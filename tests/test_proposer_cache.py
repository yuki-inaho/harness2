# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Proposer caches must contain a complete reply and any harvested workspace."""

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from harness2.core.proposer import Proposer, ProposerError
from harness2.core.spec import ChannelSpec


class InterruptedAudit(BaseException):
    """Simulate process interruption without entering the normal retry path."""


class ProposerCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.spec = ChannelSpec(kind="opencode", model="offline-test")
        self.text = "<selection>agg1</selection>\n"
        transport = patch.object(Proposer, "_opencode", side_effect=self.respond)
        self.transport = transport.start()
        self.addCleanup(transport.stop)

    def respond(self, cwd, mission, system, wall):
        workspace = cwd / "edited"
        (workspace / "empty").mkdir(parents=True)
        (workspace / "runner.py").write_text("print('complete')\n", encoding="utf-8")
        (workspace / "data.json").write_text('{"complete": true}\n', encoding="utf-8")
        return self.text, 0.25, ""

    def call(self, label="proposal", harvest=None):
        # A fresh instance models resuming in another process.
        return Proposer(self.spec, audit_dir=self.root, attempts=1).call(
            "Improve the harness", None, label, harvest=harvest
        )

    def metadata_path(self, reply):
        return reply.audit.with_name(reply.audit.name.removesuffix(".response.md") + ".meta.json")

    def test_complete_reply_is_reused_without_new_cost(self):
        first = self.call()
        cached = self.call()
        self.assertFalse(first.cached)
        self.assertEqual(first.cost_usd, 0.25)
        self.assertTrue(cached.cached)
        self.assertTrue(cached.ok)
        self.assertEqual(cached.text, self.text)
        self.assertEqual(cached.cost_usd, 0.0)
        self.assertEqual(cached.audit, first.audit)
        self.assertIsNone(cached.workspace)
        self.assertEqual(self.transport.call_count, 1)

    def test_interruption_before_metadata_requires_another_session(self):
        # Interrupt only the completion-marker publication. Patching json.dumps globally
        # would also hit the channel fingerprint (#48), which is written before any audit
        # file and is a different failure than "response published, metadata missing".
        from harness2.core import proposer as proposer_module

        real_write = proposer_module._atomic_write_text

        def interrupted_write(path, text):
            if str(path).endswith(".meta.json"):
                raise InterruptedAudit
            real_write(path, text)

        with patch("harness2.core.proposer._atomic_write_text", side_effect=interrupted_write):
            with self.assertRaises(InterruptedAudit):
                self.call()
        self.assertTrue(list(self.root.glob("*.response.md")))
        self.assertFalse(list(self.root.glob("*.meta.json")))
        resumed = self.call()
        self.assertFalse(resumed.cached)
        self.assertEqual(resumed.text, self.text)
        self.assertEqual(self.transport.call_count, 2)

    def test_interrupted_publication_leaves_no_complete_entry(self):
        replace = os.replace
        for suffix in ("response.md", "meta.json"):
            with self.subTest(suffix=suffix):
                first = self.call(suffix)
                first.audit.write_text("damaged")

                def interrupted_replace(src, dst):
                    if Path(dst).name.endswith(suffix):
                        raise InterruptedAudit
                    return replace(src, dst)

                with patch("harness2.core.proposer.os.replace", side_effect=interrupted_replace):
                    with self.assertRaises(InterruptedAudit):
                        self.call(suffix)
                self.assertFalse(self.metadata_path(first).exists())
                self.assertFalse(list(self.root.glob(".*")))
                if suffix == "response.md":
                    self.assertEqual(first.audit.read_text(), "damaged")
                calls = self.transport.call_count
                resumed = self.call(suffix)
                self.assertFalse(resumed.cached)
                self.assertEqual(resumed.text, self.text)
                self.assertEqual(self.transport.call_count, calls + 1)

    def test_missing_invalid_or_legacy_metadata_is_a_cache_miss(self):
        for damage in ("missing", "truncated", "wrong-type", "failed", "legacy", "wrong-hash"):
            with self.subTest(damage=damage):
                first = self.call(damage)
                meta_path = self.metadata_path(first)
                meta = json.loads(meta_path.read_text())
                if damage == "missing":
                    meta_path.unlink()
                elif damage == "truncated":
                    meta_path.write_text('{"ok":')
                elif damage == "wrong-type":
                    meta_path.write_text("[]")
                else:
                    if damage == "failed":
                        meta["ok"] = False
                    elif damage == "legacy":
                        meta.pop("response_sha256", None)
                        meta.pop("workspace_sha256", None)
                    else:
                        meta["response_sha256"] = "incorrect"
                    meta_path.write_text(json.dumps(meta))
                calls = self.transport.call_count
                resumed = self.call(damage)
                self.assertFalse(resumed.cached)
                self.assertEqual(resumed.text, self.text)
                self.assertEqual(self.transport.call_count, calls + 1)
                self.assertTrue(self.call(damage).cached)

    def test_damaged_response_is_a_cache_miss(self):
        for damage in (b"<selection>", b"", b"\xff", None):
            with self.subTest(damage=damage):
                label = f"response-{self.transport.call_count}"
                first = self.call(label)
                if damage is None:
                    first.audit.unlink()
                else:
                    first.audit.write_bytes(damage)
                calls = self.transport.call_count
                resumed = self.call(label)
                self.assertFalse(resumed.cached)
                self.assertEqual(resumed.text, self.text)
                self.assertEqual(self.transport.call_count, calls + 1)

    def test_complete_harvest_is_preserved_on_cache_hit(self):
        first = self.call(harvest="edited")
        cached = self.call(harvest="edited")
        self.assertTrue(cached.cached)
        self.assertEqual(cached.workspace, first.workspace)
        self.assertEqual((cached.workspace / "runner.py").read_text(), "print('complete')\n")
        self.assertTrue((cached.workspace / "empty").is_dir())
        self.assertEqual(self.transport.call_count, 1)

    def test_damaged_harvest_is_a_cache_miss(self):
        for damage in ("missing", "changed-file", "missing-file", "extra-file", "missing-directory"):
            with self.subTest(damage=damage):
                first = self.call(damage, harvest="edited")
                if damage == "missing":
                    shutil.rmtree(first.workspace)
                elif damage == "changed-file":
                    (first.workspace / "runner.py").write_text("partial")
                elif damage == "missing-file":
                    (first.workspace / "data.json").unlink()
                elif damage == "extra-file":
                    (first.workspace / "unexpected.py").write_text("extra")
                else:
                    (first.workspace / "empty").rmdir()
                calls = self.transport.call_count
                resumed = self.call(damage, harvest="edited")
                self.assertFalse(resumed.cached)
                self.assertEqual(self.transport.call_count, calls + 1)
                self.assertEqual((resumed.workspace / "runner.py").read_text(), "print('complete')\n")
                self.assertTrue((resumed.workspace / "empty").is_dir())
                self.assertTrue(self.call(damage, harvest="edited").cached)

    def test_interrupted_harvest_cannot_reuse_previous_reply(self):
        first = self.call(harvest="edited")
        shutil.rmtree(first.workspace)
        copytree = shutil.copytree

        def interrupted_copy(src, dst, *args, **kwargs):
            if Path(dst).suffix == ".workspace":
                Path(dst).mkdir()
                (Path(dst) / "runner.py").write_text("partial")
                raise InterruptedAudit
            return copytree(src, dst, *args, **kwargs)

        with patch("harness2.core.proposer.shutil.copytree", side_effect=interrupted_copy):
            with self.assertRaises(InterruptedAudit):
                self.call(harvest="edited")
        resumed = self.call(harvest="edited")
        self.assertFalse(resumed.cached)
        self.assertEqual(self.transport.call_count, 3)
        self.assertTrue((resumed.workspace / "data.json").is_file())
        self.assertEqual((resumed.workspace / "runner.py").read_text(), "print('complete')\n")

    def test_unrecorded_workspace_does_not_satisfy_harvest_request(self):
        first = self.call()
        stale = first.audit.with_name(first.audit.name.removesuffix(".response.md") + ".workspace")
        stale.mkdir()
        (stale / "runner.py").write_text("stale")
        cached = self.call()
        self.assertTrue(cached.cached)
        self.assertIsNone(cached.workspace)
        harvested = self.call(harvest="edited")
        self.assertFalse(harvested.cached)
        self.assertEqual((harvested.workspace / "runner.py").read_text(), "print('complete')\n")
        self.assertEqual(self.transport.call_count, 2)

    def test_failed_reply_is_not_reused(self):
        self.transport.side_effect = None
        self.transport.return_value = "", 0.25, "model failed"
        with self.assertRaises(ProposerError):
            self.call()
        self.transport.side_effect = self.respond
        resumed = self.call()
        self.assertFalse(resumed.cached)
        self.assertEqual(resumed.text, self.text)
        self.assertEqual(self.transport.call_count, 2)

    def test_calls_without_audit_directory_are_not_cached(self):
        proposer = Proposer(self.spec, attempts=1)
        for _ in range(2):
            reply = proposer.call("Improve the harness", None, "proposal")
            self.assertFalse(reply.cached)
            self.assertIsNone(reply.audit)
        self.assertEqual(self.transport.call_count, 2)


class ProposerCacheChannelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audit = self.root / "audit"
        self.spec = ChannelSpec("opencode", "provider/model-a")
        self.context = self.root / "context"
        (self.context / "harness").mkdir(parents=True)
        (self.context / "harness" / "script.py").write_text("print('base')\n", encoding="utf-8")

    def test_cache_is_scoped_to_channel_configuration(self):
        specs = (
            self.spec,
            replace(self.spec, model="provider/model-b"),
            replace(self.spec, kind="claude_cli"),
            replace(self.spec, effort="low"),
            replace(self.spec, steps=10),
        )
        for index, spec in enumerate(specs):
            with self.subTest(spec=spec):
                expected = f"reply {index}"
                for cached in (False, True):
                    # A new instance must still reuse a matching on-disk cache entry.
                    proposer = Proposer(spec, audit_dir=self.audit)
                    transport = "_claude_cli" if spec.kind == "claude_cli" else "_opencode"
                    with patch.object(
                        proposer, transport, return_value=(expected, 0.25, "")
                    ) as call:
                        reply = proposer.call("Edit the harness", self.context, "inv1.a1")
                    self.assertEqual(reply.text, expected)
                    self.assertEqual(reply.cached, cached)
                    self.assertEqual(reply.cost_usd, 0 if cached else 0.25)
                    self.assertEqual(call.call_count, 0 if cached else 1)

        proposer = Proposer(self.spec, audit_dir=self.audit)
        with patch.object(proposer, "_opencode") as call:
            reply = proposer.call("Edit the harness", self.context, "inv1.a1")
        call.assert_not_called()
        self.assertTrue(reply.cached)
        self.assertEqual(reply.text, "reply 0")

    def test_timeout_only_change_reuses_cached_reply(self):
        """A timeout decides whether a call finishes, not what it says: a reply that
        completed under one timeout is still valid under another."""
        proposer = Proposer(self.spec, audit_dir=self.audit)
        with patch.object(proposer, "_opencode", return_value=("reply", 0.25, "")) as call:
            reply = proposer.call("Edit the harness", self.context, "inv1.a1")
        call.assert_called_once()
        self.assertFalse(reply.cached)

        proposer = Proposer(replace(self.spec, timeout=7200), audit_dir=self.audit)
        with patch.object(proposer, "_opencode", return_value=("other", 0.25, "")) as call:
            reply = proposer.call("Edit the harness", self.context, "inv1.a1")
        call.assert_not_called()
        self.assertTrue(reply.cached)
        self.assertEqual(reply.text, "reply")

    def test_changed_model_does_not_reuse_harvested_harness(self):
        workspaces = []
        for model in ("provider/model-a", "provider/model-b"):
            with self.subTest(model=model):
                expected = f"print({model!r})\n"

                def edit(cwd, mission, system, wall):
                    (cwd / "context/harness/script.py").write_text(expected, encoding="utf-8")
                    return "Edited the script", 0.25, ""

                proposer = Proposer(replace(self.spec, model=model), audit_dir=self.audit)
                with patch.object(proposer, "_opencode", side_effect=edit) as call:
                    reply = proposer.call(
                        "Edit the harness", self.context, "inv1.a1", harvest="context/harness"
                    )
                call.assert_called_once()
                self.assertFalse(reply.cached)
                self.assertEqual((reply.workspace / "script.py").read_text(), expected)
                workspaces.append(reply.workspace)

                with patch.object(proposer, "_opencode") as call:
                    cached = proposer.call(
                        "Edit the harness", self.context, "inv1.a1", harvest="context/harness"
                    )
                call.assert_not_called()
                self.assertTrue(cached.cached)
                self.assertEqual(cached.workspace, reply.workspace)
                self.assertEqual((cached.workspace / "script.py").read_text(), expected)

        self.assertEqual(len(workspaces), 2)
        self.assertNotEqual(workspaces[0], workspaces[1])
        self.assertEqual((self.context / "harness/script.py").read_text(), "print('base')\n")

if __name__ == "__main__":
    unittest.main()
