# Copyright 2026 Google LLC
# Copyright 2026 yuki-inaho (fork addition: ordinary-task tests)
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

"""Regressions for the ordinary-task manifest, adapter and installed-CLI editor.

Every fixture is generated under tempfile: no network, no model, no home paths. The
native core (`run_task` -> `run_parallel` -> context/selection/reflection -> judge) runs
for real against real executor subprocesses; only the model-driven editor calls are
scripted, exactly as the upstream demo tests do.
"""

import contextlib
import dataclasses
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness2.cli import ordinary as ordinary_cli
from harness2.core import context, judge, schedule
from harness2.core.library import Library
from harness2.core.proposer import Proposer, ProposerError, Reply
from harness2.core.spec import ChannelSpec
from harness2.ordinary import (
    Command,
    InstalledOpenCodeProposer,
    ManifestAdapter,
    ManifestError,
    editor_health,
    load_manifest,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ordinary-task"

FIXED_SCRIPT = '''"""Summarize release notes: count change lines by kind."""

import sys


def main() -> int:
    adds = fixes = 0
    with open(sys.argv[1], encoding="utf-8") as source:
        for raw in source:
            line = raw.strip().lower()
            if not line or line.startswith("#"):
                continue
            if line.startswith("add:"):
                adds += 1
            elif line.startswith("fix:"):
                fixes += 1
    print(f"Adds: {adds}")
    print(f"Fixes: {fixes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


BROKEN_SCRIPT = '''import sys

count = 0
with open(sys.argv[1], encoding="utf-8") as source:
    for raw in source:
        line = raw.strip().lower()
        if line.startswith("fix:"):
            count += 1
print(f"Fixes: {count}")
'''


class ScriptedEditor(Proposer):
    """A deterministic editor/selector double; the only scripted part of these tests."""

    def call(self, mission, context_dir, label, **kwargs):
        del mission, kwargs
        root = Path(context_dir)
        workspace = None
        if label.startswith("select."):
            candidates = sorted((root / "candidates").glob("*.md"))
            chosen = next(
                (
                    path.stem
                    for path in candidates
                    if "Adds: 2" in path.read_text(encoding="utf-8")
                ),
                candidates[0].stem,
            )
            text = (
                "<why>The candidate counts both addition and fix lines.</why>\n"
                f"```choice\n{chosen}\n```"
            )
        elif label.startswith("experience."):
            text = (
                "<experience>\ncue: release notes mix additions and fixes\n"
                "lesson: count every change kind the notes use\n"
                "evidence: the adopted scripts/summarize.py prints Adds as well as Fixes\n"
                "confidence: high for this synthetic task\n</experience>"
            )
        else:
            workspace = root / "harness"
            (workspace / "scripts" / "summarize.py").write_text(FIXED_SCRIPT, encoding="utf-8")
            text = "<notes>count both add: and fix: lines</notes>"
        reply = Reply(label=label, text=text, ok=True, attempts=1, workspace=workspace)
        self._calls.append(reply)
        return reply


class TwoTaskEditor(ScriptedEditor):
    """Edits correctly for one task and leaves the base behaviour for another."""

    def call(self, mission, context_dir, label, **kwargs):
        root = Path(context_dir)
        if label.startswith(("select.", "experience.")):
            return super().call(mission, context_dir, label, **kwargs)
        source = (root / "source" / "notes.txt").read_text(encoding="utf-8")
        script = FIXED_SCRIPT if "add: csv export" in source else BROKEN_SCRIPT
        workspace = root / "harness"
        (workspace / "scripts" / "summarize.py").write_text(script, encoding="utf-8")
        reply = Reply(
            label=label, text="<notes>edited</notes>", ok=True, attempts=1, workspace=workspace
        )
        self._calls.append(reply)
        return reply


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.project = self.tmp / "ordinary-task"
        shutil.copytree(EXAMPLE, self.project)
        self.manifest_path = self.project / "manifest.json"

    # -- helpers ---------------------------------------------------------------

    def adapter(self, runs: str = "runs", manifest=None) -> ManifestAdapter:
        return ManifestAdapter(
            manifest or load_manifest(self.manifest_path), runs_root=self.tmp / runs
        )

    def write_script(self, name: str, body: str) -> Path:
        path = self.tmp / "tools" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def write_cli(self, name: str, body: str) -> Path:
        path = self.write_script(name, body)
        path.chmod(0o755)
        return path

    def run_k1(self, adapter: ManifestAdapter, manifest=None):
        manifest = manifest or adapter.manifest
        prop = ScriptedEditor(manifest.editor)
        libraries = {
            "agg": Library(
                root=self.tmp / "library" / "agg",
                domain=manifest.domain,
                surface=adapter.substrate.surface(),
            )
        }
        result = schedule.run_task(
            "notes-1",
            1,
            adapter=adapter,
            prop=prop,
            runs_root=self.tmp / "runs",
            modes=("agg",),
            libraries=libraries,
        )
        return result, prop, libraries


class NativeFlowTests(Base):
    def test_parallel_flow_through_the_real_core(self):
        manifest = load_manifest(self.manifest_path)
        adapter = self.adapter()
        prop = ScriptedEditor(manifest.editor)
        libraries = {
            "agg": Library(
                root=self.tmp / "library" / "agg",
                domain=manifest.domain,
                surface=adapter.substrate.surface(),
            )
        }
        result = schedule.run_task(
            "notes-1",
            2,
            adapter=adapter,
            prop=prop,
            runs_root=self.tmp / "runs",
            modes=("agg",),
            libraries=libraries,
        )
        self.assertEqual((result.status, result.errors), ("ok", []))

        namespace = schedule.ns(manifest.name, "oc", "agg", 2)
        work = self.tmp / "runs" / namespace / "notes-1"
        summary = json.loads((work / "summary.json").read_text())
        ledger = summary["modes"]["agg"]["ledger"]
        self.assertEqual((ledger["R"], ledger["P"]), (8, 6))
        self.assertEqual(summary["modes"]["agg"]["finals"], ["agg1", "agg2"])

        selection = summary["selections"]["agg"]
        self.assertTrue(selection["ok"])
        self.assertFalse(selection["chosen_is_base"])
        self.assertTrue((work / "selection" / "agg2" / "selection.json").is_file())
        reflection = work / "reflection" / "agg2"
        for rel in (
            "adoption.json",
            "base_harness/scripts/summarize.py",
            "harness/scripts/summarize.py",
        ):
            self.assertTrue((reflection / rel).is_file(), rel)

        executions = self.tmp / "runs" / "executions" / "notes-1"
        for label in ("base1", "base2", "inv1", "dom1", "inv2", "dom2", "agg1", "agg2"):
            run_dir = executions / f"{manifest.name}-oc-agg2__notes-1__{label}"
            for name in ("run.json", "exit.json", "answer.md", "completion.json"):
                self.assertTrue((run_dir / name).is_file(), f"{label}/{name}")

        base_answer = (executions / f"{manifest.name}-oc-agg2__notes-1__base1/answer.md").read_text()
        self.assertNotIn("Adds:", base_answer)
        chosen_answer = (Path(selection["chosen_run_dir"]) / "answer.md").read_text()
        self.assertIn("Adds: 2", chosen_answer)
        self.assertIn("Fixes: 3", chosen_answer)
        self.assertEqual(libraries["agg"].entries(), ["notes-1"])
        self.assertTrue(
            ordinary_cli._configured_match(result.base[0], selection),
            "base and adopted runs must record the same configuration, harness aside",
        )

        run_record = json.loads(
            (executions / f"{manifest.name}-oc-agg2__notes-1__base1" / "run.json").read_text()
        )
        self.assertFalse(run_record["declared_model_verified"])
        self.assertEqual(run_record["declared_executor_model"], "declared-example-label")

    def test_k1_selection_is_trivial_and_never_claims_improvement(self):
        adapter = self.adapter()
        result, _prop, _libs = self.run_k1(adapter)
        selection = result.selections["agg"]
        self.assertTrue(selection["trivial"])
        self.assertEqual(selection["chosen_label"], "agg1")
        self.assertFalse(selection["chosen_is_base"])
        self.assertEqual(selection["id_map"], {"A": "agg1"})
        self.assertNotIn("base1", selection["id_map"].values())


class JudgeTests(Base):
    def test_judge_is_gated_and_a_failure_is_unknown_not_zero(self):
        adapter = self.adapter()
        fake_run = self.tmp / "fake-run"
        fake_run.mkdir()
        (fake_run / "answer.md").write_text("Adds: 2\nFixes: 3\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as gate:
            adapter.score(fake_run, "notes-1")
        self.assertIn("judge gated", str(gate.exception))

        result, _prop, _libs = self.run_k1(adapter)
        selection = result.selections["agg"]
        base = result.base[0]
        base_row = judge.judge_one(
            adapter,
            "notes-1",
            "agg",
            1,
            base["label"],
            base["run_id"],
            Path(base["run_dir"]),
            chosen_is_base=True,
        )
        chosen_row = judge.judge_one(
            adapter,
            "notes-1",
            "agg",
            1,
            selection["chosen_label"],
            selection["chosen_run_id"],
            Path(selection["chosen_run_dir"]),
        )
        self.assertEqual((base_row.n_passed, base_row.n_criteria), (1, 2))
        self.assertEqual((chosen_row.n_passed, chosen_row.n_criteria), (2, 2))
        self.assertEqual((base_row.frac, chosen_row.frac), (0.5, 1.0))

        manifest = load_manifest(self.manifest_path)
        cases = {
            "float count": '{"n_passed": 1.5, "n_criteria": 2}',
            "boolean count": '{"n_passed": true, "n_criteria": 2}',
            "zero denominator": '{"n_passed": 0, "n_criteria": 0}',
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                script = self.write_script(
                    f"judge-{name.split()[0]}.py", f"print({payload!r})\n"
                )
                broken = dataclasses.replace(
                    manifest,
                    judge=Command(argv=(sys.executable, str(script)), timeout_s=30),
                )
                bad_adapter = ManifestAdapter(broken, runs_root=self.tmp / "runs")
                row = judge.judge_one(
                    bad_adapter,
                    "notes-1",
                    "agg",
                    1,
                    selection["chosen_label"],
                    selection["chosen_run_id"],
                    Path(selection["chosen_run_dir"]),
                )
                self.assertFalse(row.graded, name)
                self.assertIsNone(row.n_passed, name)
                self.assertTrue(row.error, name)

    def test_judge_evidence_is_retained_per_task_and_run(self):
        adapter = self.adapter()
        second = self.project / "tasks" / "notes-2"
        shutil.copytree(self.project / "tasks" / "notes-1", second)
        (second / "source" / "notes.txt").write_text("add: one\nfix: two\n", encoding="utf-8")
        manifest = adapter.manifest
        prop = ScriptedEditor(manifest.editor)
        libraries = {
            "agg": Library(
                root=self.tmp / "library" / "agg",
                domain=manifest.domain,
                surface=adapter.substrate.surface(),
            )
        }
        for task_id in ("notes-1", "notes-2"):
            result = schedule.run_task(
                task_id,
                1,
                adapter=adapter,
                prop=prop,
                runs_root=self.tmp / "runs",
                modes=("agg",),
                libraries=libraries,
            )
            self.assertEqual(result.status, "ok", task_id)
            selection = result.selections["agg"]
            records = [
                (result.base[0], True),
                (
                    {
                        "label": selection["chosen_label"],
                        "run_id": selection["chosen_run_id"],
                        "run_dir": selection["chosen_run_dir"],
                    },
                    False,
                ),
            ]
            for record, is_base in records:
                judge.judge_one(
                    adapter,
                    task_id,
                    "agg",
                    1,
                    record["label"],
                    record["run_id"],
                    Path(record["run_dir"]),
                    chosen_is_base=is_base,
                )
        judge_root = self.tmp / "runs" / "judge"
        self.assertEqual(sorted(p.name for p in judge_root.iterdir()), ["notes-1", "notes-2"])
        for task_id in ("notes-1", "notes-2"):
            runs = sorted((judge_root / task_id).iterdir())
            self.assertEqual(len(runs), 2, [p.name for p in runs])
            for run in runs:
                self.assertTrue((run / "judge.json").is_file(), run)
        first = sorted((judge_root / "notes-1").iterdir())[0] / "judge.json"
        self.assertIn("argv", json.loads(first.read_text()))

    def test_judge_rejects_an_inconsistent_all_pass_flag(self):
        adapter = self.adapter()
        result, _prop, _libs = self.run_k1(adapter)
        selection = result.selections["agg"]
        manifest = load_manifest(self.manifest_path)
        cases = {
            "counts form": '{"n_passed": 0, "n_criteria": 2, "all_pass": true}',
            "criteria form": '{"criteria": [{"name": "a", "passed": true}], '
            '"all_pass": false}',
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                script = self.write_script(
                    f"judge-allpass-{name.split()[0]}.py", f"print({payload!r})\n"
                )
                broken = dataclasses.replace(
                    manifest,
                    judge=Command(argv=(sys.executable, str(script)), timeout_s=30),
                )
                bad_adapter = ManifestAdapter(broken, runs_root=self.tmp / "runs")
                row = judge.judge_one(
                    bad_adapter,
                    "notes-1",
                    "agg",
                    1,
                    selection["chosen_label"],
                    selection["chosen_run_id"],
                    Path(selection["chosen_run_dir"]),
                )
                self.assertFalse(row.graded, name)
                self.assertIn("all_pass", row.error, name)

    def test_judge_nonzero_exit_is_an_error(self):
        adapter = self.adapter()
        result, _prop, _libs = self.run_k1(adapter)
        selection = result.selections["agg"]
        manifest = load_manifest(self.manifest_path)
        script = self.write_script(
            "judge-broken.py", "import sys\nprint('cannot measure', file=sys.stderr)\nraise SystemExit(3)\n"
        )
        broken = dataclasses.replace(
            manifest, judge=Command(argv=(sys.executable, str(script)), timeout_s=30)
        )
        bad_adapter = ManifestAdapter(broken, runs_root=self.tmp / "runs")
        row = judge.judge_one(
            bad_adapter,
            "notes-1",
            "agg",
            1,
            selection["chosen_label"],
            selection["chosen_run_id"],
            Path(selection["chosen_run_dir"]),
        )
        self.assertFalse(row.graded)
        self.assertIn("rc=3", row.error)


class ExecutorContractTests(Base):
    def test_timeout_kills_children_that_stay_in_the_process_group(self):
        manifest = load_manifest(self.manifest_path)
        marker = self.tmp / "orphan.txt"
        slow = self.write_script(
            "slow.py",
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import pathlib,sys,time; time.sleep(2); "
            "pathlib.Path(sys.argv[1]).write_text(\"orphan\")', sys.argv[1]])\n"
            "time.sleep(30)\n",
        )
        timeout_manifest = dataclasses.replace(
            manifest,
            executor=Command(argv=(sys.executable, str(slow), str(marker)), timeout_s=1),
        )
        adapter = ManifestAdapter(timeout_manifest, runs_root=self.tmp / "runs")
        with self.assertRaises(RuntimeError):
            adapter.execute("notes-1", None, "ordinary-agg1/notes-1/base1")
        run_dir = self.tmp / "runs" / "executions" / "notes-1" / "ordinary-agg1__notes-1__base1"
        record = json.loads((run_dir / "exit.json").read_text())
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["timeout_kind"], "timeout")
        self.assertFalse(record["answer_present"])
        self.assertFalse(adapter.delivered(run_dir))
        self.assertFalse((run_dir / "completion.json").exists())
        time.sleep(3.2)
        self.assertFalse(marker.exists(), "a same-group descendant survived the kill")

    def test_a_descendant_that_setsids_is_outside_the_kill_scope(self):
        """Documents the limit: only the owned process group is killed, not every session."""
        manifest = load_manifest(self.manifest_path)
        marker = self.tmp / "detached.txt"
        slow = self.write_script(
            "slow-setsid.py",
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen(['setsid', sys.executable, '-c', "
            "'import pathlib,sys,time; time.sleep(1.5); "
            "pathlib.Path(sys.argv[1]).write_text(\"detached\")', sys.argv[1]])\n"
            "time.sleep(30)\n",
        )
        timeout_manifest = dataclasses.replace(
            manifest,
            executor=Command(argv=(sys.executable, str(slow), str(marker)), timeout_s=1),
        )
        adapter = ManifestAdapter(timeout_manifest, runs_root=self.tmp / "runs")
        with self.assertRaises(RuntimeError):
            adapter.execute("notes-1", None, "ordinary-agg1/notes-1/base1")
        time.sleep(2.5)
        self.assertTrue(
            marker.exists(),
            "if this now fails, the kill got stronger: update the documented limitation",
        )

    def test_exit_zero_without_an_answer_is_a_failure(self):
        manifest = load_manifest(self.manifest_path)
        noop = dataclasses.replace(
            manifest,
            executor=Command(argv=(sys.executable, "-c", "print('nothing')"), timeout_s=30),
        )
        adapter = ManifestAdapter(noop, runs_root=self.tmp / "runs")
        with self.assertRaises(RuntimeError) as failed:
            adapter.execute("notes-1", None, "ordinary-agg1/notes-1/base1")
        self.assertIn("wrote no non-empty", str(failed.exception))
        run_dir = self.tmp / "runs" / "executions" / "notes-1" / "ordinary-agg1__notes-1__base1"
        self.assertFalse(json.loads((run_dir / "exit.json").read_text())["answer_present"])
        self.assertFalse(adapter.delivered(run_dir))

    def test_execute_is_idempotent_only_for_the_same_identity(self):
        manifest = load_manifest(self.manifest_path)
        counter = self.tmp / "counter.txt"
        mark = self.write_script(
            "mark.py",
            "import pathlib, sys\n"
            "counter = pathlib.Path(sys.argv[1])\n"
            "counter.write_text((counter.read_text() if counter.exists() else '') + 'x')\n"
            "pathlib.Path(sys.argv[2]).write_text('Adds: 2\\nFixes: 3\\n')\n",
        )
        marked = dataclasses.replace(
            manifest,
            executor=Command(
                argv=(sys.executable, str(mark), str(counter), "{run_dir}/answer.md"),
                timeout_s=30,
            ),
        )
        adapter = ManifestAdapter(marked, runs_root=self.tmp / "runs")
        first = adapter.execute("notes-1", None, "ordinary-agg1/notes-1/base1")
        again = adapter.execute("notes-1", None, "ordinary-agg1/notes-1/base1")
        self.assertEqual(first, again)
        self.assertEqual(counter.read_text(), "x")
        adapter.execute("notes-1", None, "ordinary-agg1/notes-1/base2")
        self.assertEqual(counter.read_text(), "xx")

    def test_answer_keys_never_reach_the_executor_or_evidence(self):
        task = self.project / "tasks" / "notes-1"
        (task / "source" / "expected.md").write_text("Adds: 2\nFixes: 3\n", encoding="utf-8")
        (task / "source" / "judge.json").write_text('{"criteria": []}', encoding="utf-8")
        adapter = self.adapter()
        run_dir = adapter.execute("notes-1", None, "ordinary-agg1/notes-1/base1")
        copied = {
            path.relative_to(run_dir / "input" / "task").as_posix()
            for path in (run_dir / "input" / "task").rglob("*")
            if path.is_file()
        }
        self.assertNotIn("source/expected.md", copied)
        self.assertNotIn("source/judge.json", copied)
        self.assertIn("source/notes.txt", copied)

        evidence = self.tmp / "evidence"
        context.copy_source(adapter, "notes-1", evidence)
        self.assertFalse((evidence / "source" / "expected.md").exists())
        self.assertTrue((evidence / "source" / "notes.txt").is_file())


class ManifestTests(Base):
    def minimal(self) -> dict:
        base = self.tmp / "unit" / "base"
        base.mkdir(parents=True, exist_ok=True)
        (base / "systemprompt.md").write_text("Do the work.\n", encoding="utf-8")
        task = self.tmp / "unit" / "tasks" / "t-1"
        task.mkdir(parents=True, exist_ok=True)
        (task / "task.md").write_text("Write answer.md.\n", encoding="utf-8")
        return {
            "schema": 1,
            "name": "unit",
            "base_harness": "base",
            "tasks_dir": "tasks",
            "executor": {
                "argv": ["python3", "answer.py"],
                "answer": "answer.md",
                "timeout_s": 30,
            },
            "editor": {
                "channel": "opencode",
                "model": "example/model#high",
                "steps": 5,
                "timeout_s": 60,
                "startup_timeout_s": 10,
                "launcher": "service",
            },
            "prompts": {"work": "summarize a file"},
        }

    def load(self, doc: dict):
        path = self.tmp / "unit" / "manifest.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return load_manifest(path)

    def test_valid_manifest_round_trips(self):
        manifest = self.load(self.minimal())
        self.assertEqual(manifest.answer, "answer.md")
        self.assertEqual(manifest.editor.model, "example/model#high")
        self.assertEqual(manifest.launcher, "service")
        self.assertEqual(manifest.executor.argv, ("python3", "answer.py"))
        receipt = self.adapter(manifest=manifest).receipt()
        self.assertEqual(receipt["editor"]["steps_configured"], 5)
        self.assertFalse(receipt["editor"]["steps_enforced"])
        self.assertNotIn("steps", receipt["editor"])
        self.assertFalse(receipt["executor"]["declared_model_verified"])

    def test_malformed_manifests_are_rejected_before_any_run(self):
        cases = {
            "unknown key": lambda d: d.update(extra=1),
            "schema": lambda d: d.update(schema=2),
            "string argv": lambda d: d["executor"].update(argv="python3 answer.py"),
            "unknown placeholder": lambda d: d["executor"].update(
                argv=["python3", "{nope}/answer.py"]
            ),
            "unclosed brace": lambda d: d["executor"].update(
                argv=["python3", "answer.py", "{task_dir"]
            ),
            "absolute base": lambda d: d.update(base_harness="/tmp/base"),
            "traversal base": lambda d: d.update(base_harness="../base"),
            "bool timeout": lambda d: d["executor"].update(timeout_s=True),
            "float timeout": lambda d: d["executor"].update(timeout_s=1.5),
            "zero timeout": lambda d: d["executor"].update(timeout_s=0),
            "editor model without variant": lambda d: d["editor"].update(
                model="example/model"
            ),
            "editor launcher": lambda d: d["editor"].update(launcher="auto"),
            "startup over wall": lambda d: d["editor"].update(startup_timeout_s=61),
            "measurement word": lambda d: d["prompts"].update(work="score the file"),
            "small obs_trunc": lambda d: d.update(obs_trunc=100),
            "answer traversal": lambda d: d["executor"].update(answer="../answer.md"),
            "answer reserved": lambda d: d["executor"].update(answer="run.json"),
            "uppercase name": lambda d: d.update(name="Unit"),
            "judge without argv": lambda d: d.update(judge={"timeout_s": 10}),
            "unknown judge key": lambda d: d.update(
                judge={"argv": ["python3", "j.py"], "timeout_s": 10, "shell": True}
            ),
        }
        for name, mutation in cases.items():
            with self.subTest(name=name):
                doc = self.minimal()
                mutation(doc)
                with self.assertRaises(ManifestError):
                    self.load(doc)

    def test_symlink_escape_is_rejected(self):
        doc = self.minimal()
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "systemprompt.md").write_text("not ours\n", encoding="utf-8")
        base = self.tmp / "unit" / "base"
        shutil.rmtree(base)
        base.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ManifestError) as escaped:
            self.load(doc)
        self.assertIn("symlink escape", str(escaped.exception))


class CliTests(Base):
    def test_occupied_output_is_rejected_and_dry_run_writes_nothing(self):
        occupied = self.tmp / "occupied"
        occupied.mkdir()
        (occupied / "keep.txt").write_text("mine\n", encoding="utf-8")
        code = ordinary_cli.main(
            ["--manifest", str(self.manifest_path), "--output", str(occupied)]
        )
        self.assertEqual(code, 2)
        self.assertEqual(sorted(p.name for p in occupied.iterdir()), ["keep.txt"])

        fresh = self.tmp / "fresh"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ordinary_cli.main(
                [
                    "--manifest",
                    str(self.manifest_path),
                    "--output",
                    str(fresh),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        self.assertFalse(fresh.exists())
        self.assertIn("tasks_selected", out.getvalue())
        self.assertIn("notes-1", out.getvalue())

    def test_unknown_task_is_a_configuration_error(self):
        fresh = self.tmp / "fresh"
        code = ordinary_cli.main(
            [
                "--manifest",
                str(self.manifest_path),
                "--output",
                str(fresh),
                "--only-tasks",
                "missing",
            ]
        )
        self.assertEqual(code, 2)
        self.assertFalse(fresh.exists())

    def test_invalid_manifest_writes_no_output_directory(self):
        good = json.loads(self.manifest_path.read_text())
        broken = {
            "boolean executor timeout": ("executor", "timeout_s", True),
            "missing base harness": ("", "base_harness", "missing-base"),
            "unknown executor key": ("executor", "shell", True),
        }
        for name, (section, key, value) in broken.items():
            with self.subTest(name=name):
                doc = json.loads(json.dumps(good))
                target = doc if not section else doc[section]
                target[key] = value
                path = self.project / f"bad-{key}.json"
                path.write_text(json.dumps(doc), encoding="utf-8")
                output = self.tmp / f"no-write-{key}"
                code = ordinary_cli.main(
                    ["--manifest", str(path), "--output", str(output)]
                )
                self.assertEqual(code, 2, name)
                self.assertFalse(output.exists(), name)

    def test_run_one_keys_judgments_by_task_and_retains_both(self):
        output = self.tmp / "out"
        adapter = ManifestAdapter(
            load_manifest(self.manifest_path), runs_root=output / "runs"
        )
        second = self.project / "tasks" / "notes-2"
        shutil.copytree(self.project / "tasks" / "notes-1", second)
        (second / "source" / "notes.txt").write_text("add: one\nfix: two\n", encoding="utf-8")
        prop = TwoTaskEditor(adapter.manifest.editor)
        output = self.tmp / "out"
        libraries = {
            "agg": Library(
                root=output / "library" / "agg",
                domain=adapter.manifest.domain,
                surface=adapter.substrate.surface(),
            )
        }
        judged: dict[tuple[str, str], judge.Scored] = {}
        rows: list[judge.Scored] = []
        rows_out = [
            ordinary_cli._run_one(
                adapter,
                prop,
                libraries,
                task_id,
                k=1,
                modes=("agg",),
                tag="",
                judge=True,
                judged=judged,
                rows=rows,
            )
            for task_id in ("notes-1", "notes-2")
        ]
        self.assertEqual([row["status"] for row in rows_out], ["ok", "ok"])
        first, second_row = rows_out
        first_cmp = first["modes"]["agg"]["comparison"]
        second_cmp = second_row["modes"]["agg"]["comparison"]
        self.assertEqual((first_cmp["base_frac"], first_cmp["selected_frac"]), (0.5, 1.0))
        self.assertEqual(first_cmp["delta_frac"], 0.5)
        self.assertEqual((second_cmp["base_frac"], second_cmp["selected_frac"]), (0.5, 0.5))
        self.assertEqual(second_cmp["delta_frac"], 0.0)
        self.assertTrue(first_cmp["judged_pair_available"])
        self.assertTrue(first_cmp["configured_match"])
        self.assertEqual(first_cmp["resolved_executor_model"], "opaque")

        self.assertEqual(len(rows), 4)
        self.assertEqual(sorted({row.task_id for row in rows}), ["notes-1", "notes-2"])
        self.assertTrue(all(isinstance(key, tuple) and len(key) == 2 for key in judged))
        self.assertEqual(len(judged), 4)
        judge_root = output / "runs" / "judge"
        self.assertEqual(sorted(p.name for p in judge_root.iterdir()), ["notes-1", "notes-2"])

    def test_configured_match_ignores_only_the_harness_and_run_identity(self):
        runs = self.tmp / "runs" / "executions" / "notes-1"
        identity = {
            "manifest_sha256": "manifest",
            "task_id": "notes-1",
            "run_id": "ns/notes-1/base1",
            "task_sha256": "task",
            "base_sha256": "base",
            "harness_sha256": "harness-a",
            "executor": {
                "argv_template": ["python3", "{run_dir}/answer.md"],
                "argv_normalized_sha256": "argv",
                "answer": "answer.md",
                "timeout_s": 120,
                "declared_model": "declared-example-label",
            },
            "editor": {
                "model": "example/provider#high",
                "launcher": "service",
                "steps_configured": 40,
                "steps_enforced": False,
                "timeout_s": 900,
                "startup_timeout_s": 120,
            },
            "judge": ["python3", "{manifest_dir}/tools/judge.py"],
        }
        for name, harness in (("base1", "harness-a"), ("agg1", "harness-b")):
            directory = runs / name
            directory.mkdir(parents=True, exist_ok=True)
            record = json.loads(json.dumps(identity))
            record["run_id"] = f"ns/notes-1/{name}"
            record["harness_sha256"] = harness
            (directory / "run.json").write_text(
                json.dumps({"identity": record}), encoding="utf-8"
            )
        base = {"run_dir": str(runs / "base1")}
        selection = {"chosen_run_dir": str(runs / "agg1")}
        self.assertTrue(ordinary_cli._configured_match(base, selection))
        drifted = json.loads((runs / "agg1" / "run.json").read_text())
        drifted["identity"]["executor"]["timeout_s"] = 999
        (runs / "agg1" / "run.json").write_text(json.dumps(drifted), encoding="utf-8")
        self.assertFalse(ordinary_cli._configured_match(base, selection))


class InstalledCliTests(Base):
    def test_argv_is_the_2_1_cli_shape(self):
        spec = ChannelSpec("opencode", "example/model#high", steps=40, timeout=900)
        service = InstalledOpenCodeProposer(spec, launcher="service", startup_timeout_s=30)
        argv = service.argv_for("inv1.a1")
        self.assertEqual(argv[:2], [service.cli, "run"])
        self.assertIn("--auto", argv)
        self.assertIn("--format", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "example/model#high")
        self.assertEqual(argv[argv.index("--file") + 1], "MISSION.md")
        self.assertNotIn("--dir", argv)
        self.assertNotIn("--variant", argv)
        self.assertNotIn("--standalone", argv)
        self.assertEqual(argv[-1], "Read MISSION.md and do what it asks. Reply with the requested output.")

        standalone = InstalledOpenCodeProposer(
            spec, launcher="standalone", startup_timeout_s=30
        )
        self.assertEqual(standalone.argv_for("x")[2], "--standalone")

    def test_a_real_exit_is_recorded_and_cost_is_reported(self):
        fake = self.write_cli(
            "opencode-ok",
            "#!/usr/bin/env python3\n"
            "import json, pathlib\n"
            "out = pathlib.Path('output')\n"
            "out.mkdir(exist_ok=True)\n"
            "(out / 'answer.md').write_text('Adds: 2\\nFixes: 3\\n')\n"
            "print(json.dumps({'type': 'step_finish', "
            "'part': {'type': 'step_finish', 'cost': 0.25}}))\n",
        )
        prop = InstalledOpenCodeProposer(
            ChannelSpec("opencode", "example/model#high", steps=40, timeout=60),
            audit_dir=self.tmp / "audit",
            launcher="service",
            startup_timeout_s=10,
            cli=str(fake),
        )
        reply = prop.call("Do the task.", None, "inv1.a1", max_attempts=1)
        self.assertTrue(reply.ok)
        self.assertEqual(reply.text.strip(), "Adds: 2\nFixes: 3")
        summary = prop.cost_summary()
        self.assertEqual(summary["cost_usd"], 0.25)
        self.assertEqual(summary["cost_source"], "reported")
        exit_record = json.loads(
            (self.tmp / "audit" / "sessions" / "inv1.a1" / "exit.json").read_text()
        )
        self.assertEqual(exit_record["returncode"], 0)
        self.assertFalse(exit_record["timed_out"])
        self.assertTrue(exit_record["answer_present"])
        self.assertEqual(exit_record["cost_usd"], 0.25)

    def test_startup_timeout_is_bounded_and_cost_stays_null(self):
        slow = self.write_cli(
            "opencode-slow", "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n"
        )
        prop = InstalledOpenCodeProposer(
            ChannelSpec("opencode", "example/model#high", steps=40, timeout=15),
            audit_dir=self.tmp / "audit",
            launcher="service",
            startup_timeout_s=1,
            cli=str(slow),
        )
        with self.assertRaises(ProposerError) as failed:
            prop.call("Do the task.", None, "inv1.a1", max_attempts=1, timeout=10)
        self.assertIn("startup_timeout", str(failed.exception))
        exit_record = json.loads(
            (self.tmp / "audit" / "sessions" / "inv1.a1" / "exit.json").read_text()
        )
        self.assertTrue(exit_record["timed_out"])
        self.assertIsNone(exit_record["cost_usd"])
        self.assertEqual(exit_record["cost_source"], "unmeasured")
        self.assertIsNone(prop.cost_summary()["cost_usd"])

    def test_error_event_is_a_failure_even_with_exit_zero(self):
        failing = self.write_cli(
            "opencode-error",
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({'type': 'error', 'error': {'name': 'ProviderError', "
            "'data': {'message': 'denied'}}}))\n",
        )
        prop = InstalledOpenCodeProposer(
            ChannelSpec("opencode", "example/model#high", steps=40, timeout=60),
            audit_dir=self.tmp / "audit",
            launcher="service",
            startup_timeout_s=10,
            cli=str(failing),
        )
        with self.assertRaises(ProposerError) as failed:
            prop.call("Do the task.", None, "inv1.a1", max_attempts=1)
        self.assertIn("denied", str(failed.exception))
        exit_record = json.loads(
            (self.tmp / "audit" / "sessions" / "inv1.a1" / "exit.json").read_text()
        )
        self.assertEqual(exit_record["returncode"], 0)
        self.assertIn("error event", exit_record["error"])

    def test_service_inherits_the_installed_xdg_and_standalone_isolates_it(self):
        fake = self.write_cli(
            "opencode-env",
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "label = args[args.index('--title') + 1] if '--title' in args else 'x'\n"
            "cap = pathlib.Path(os.environ['H2_TEST_ENV_CAPTURE'])\n"
            "cap.mkdir(parents=True, exist_ok=True)\n"
            "(cap / (label.replace('.', '_') + '.json')).write_text(json.dumps({\n"
            "    'XDG_DATA_HOME': os.environ.get('XDG_DATA_HOME'),\n"
            "    'XDG_STATE_HOME': os.environ.get('XDG_STATE_HOME'),\n"
            "    'PWD': os.environ.get('PWD'),\n"
            "    'OPENCODE_PERMISSION': os.environ.get('OPENCODE_PERMISSION'),\n"
            "}))\n"
            "out = pathlib.Path('output')\n"
            "out.mkdir(exist_ok=True)\n"
            "(out / 'answer.md').write_text('env captured\\n')\n",
        )
        installed = self.tmp / "installed"
        capture = self.tmp / "capture"
        env = {
            "XDG_DATA_HOME": str(installed / "data"),
            "XDG_STATE_HOME": str(installed / "state"),
            "H2_TEST_ENV_CAPTURE": str(capture),
        }
        spec = ChannelSpec("opencode", "example/model#high", steps=40, timeout=60)
        with mock.patch.dict(os.environ, env):
            service = InstalledOpenCodeProposer(
                spec,
                audit_dir=self.tmp / "audit",
                launcher="service",
                startup_timeout_s=10,
                cli=str(fake),
            )
            self.assertTrue(service.call("Do it.", None, "svc1", max_attempts=1).ok)
            standalone = InstalledOpenCodeProposer(
                spec,
                audit_dir=self.tmp / "audit",
                launcher="standalone",
                startup_timeout_s=10,
                cli=str(fake),
            )
            self.assertTrue(standalone.call("Do it.", None, "std1", max_attempts=1).ok)

        service_env = json.loads((capture / "svc1.json").read_text())
        self.assertEqual(service_env["XDG_DATA_HOME"], str(installed / "data"))
        self.assertEqual(service_env["XDG_STATE_HOME"], str(installed / "state"))
        self.assertIn("external_directory", json.loads(service_env["OPENCODE_PERMISSION"]))
        service_start = json.loads(
            (self.tmp / "audit" / "sessions" / "svc1" / "start.json").read_text()
        )
        self.assertEqual(service_start["env_keys_set"], ["PWD", "OPENCODE_PERMISSION"])
        self.assertEqual(service_start["xdg_isolation"], "inherited_shared_service")

        standalone_env = json.loads((capture / "std1.json").read_text())
        session_xdg = self.tmp / "audit" / "sessions" / "std1" / ".xdg"
        self.assertEqual(standalone_env["XDG_DATA_HOME"], str(session_xdg / "data"))
        self.assertEqual(standalone_env["XDG_STATE_HOME"], str(session_xdg / "state"))
        self.assertNotEqual(standalone_env["XDG_DATA_HOME"], service_env["XDG_DATA_HOME"])
        standalone_start = json.loads(
            (self.tmp / "audit" / "sessions" / "std1" / "start.json").read_text()
        )
        self.assertEqual(
            standalone_start["env_keys_set"],
            ["PWD", "OPENCODE_PERMISSION", "XDG_DATA_HOME", "XDG_STATE_HOME"],
        )
        self.assertEqual(standalone_start["xdg_isolation"], "per_call")

        self.assertNotEqual(service_start["label"], standalone_start["label"])
        self.assertEqual(service.health()["state"], "all_succeeded")
        self.assertEqual(standalone.health()["n_sessions"], 1)


class DegradedRunTests(Base):
    """An all-editor-fail run must not look green just because native status is `ok`."""

    def test_health_states_keep_partial_success_distinct_from_all_failed(self):
        ok = {"label": "ok1", "error": ""}
        bad = {"label": "bad1", "error": "rc=1: port in use"}
        empty = editor_health([])
        self.assertEqual((empty["state"], empty["degraded"]), ("no_calls", False))
        all_ok = editor_health([ok])
        self.assertEqual((all_ok["state"], all_ok["degraded"]), ("all_succeeded", False))
        partial = editor_health([ok, bad])
        self.assertEqual((partial["state"], partial["degraded"]), ("partial", False))
        self.assertEqual((partial["n_sessions"], partial["n_failed"]), (2, 1))
        self.assertEqual(partial["failed_labels"], ["bad1"])
        all_bad = editor_health([bad])
        self.assertEqual((all_bad["state"], all_bad["degraded"]), ("all_failed", True))

    def test_every_editor_call_failing_is_reported_degraded_and_exits_nonzero(self):
        failing = self.write_cli(
            "opencode-always-fails",
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({'type': 'error', 'error': {'name': 'ServiceError', "
            "'data': {'message': 'Managed service port is already in use'}}}))\n"
            "raise SystemExit(1)\n",
        )
        output = self.tmp / "degraded"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("shutil.which", return_value=str(failing)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ordinary_cli.main(
                    [
                        "--manifest",
                        str(self.manifest_path),
                        "--output",
                        str(output),
                        "--k",
                        "1",
                        "--modes",
                        "parallel",
                    ]
                )
        self.assertEqual(code, 1, "all-editor-fail must not exit 0")
        self.assertIn("1 degraded", out.getvalue())
        self.assertIn("degraded (editor calls failed", err.getvalue())
        report = json.loads((output / "report.json").read_text())
        row = report["tasks"][0]
        self.assertEqual(row["status"], "ok", "native status is kept as its own fact")
        self.assertEqual(row["errors"], [])
        health = row["integration"]["editor_health"]
        self.assertEqual((health["n_sessions"], health["n_failed"]), (8, 8))
        self.assertEqual(health["n_succeeded"], 0)
        self.assertEqual(health["state"], "all_failed")
        self.assertTrue(row["integration"]["degraded"])
        self.assertTrue(row["integration"]["fallback_labels"])
        self.assertEqual(report["degraded_tasks"], ["notes-1"])
        self.assertEqual(report["editor_health"]["state"], "all_failed")
        self.assertIn("degraded", "\n".join(report["notes"]))
        receipts = sorted((output / "audit" / "sessions").glob("*/exit.json"))
        self.assertEqual(len(receipts), 8)
        for receipt in receipts:
            self.assertTrue(json.loads(receipt.read_text())["error"])

    def test_ok_without_editor_receipts_is_degraded_for_real_proposer(self):
        spec = ChannelSpec("opencode", "example/model#high", steps=40, timeout=60)
        proposer = InstalledOpenCodeProposer(
            spec, audit_dir=self.tmp / "audit", launcher="service", startup_timeout_s=10
        )
        integration = ordinary_cli._integration(
            proposer, 0, ("agg",), {}, status="ok"
        )
        self.assertEqual(integration["editor_health"]["state"], "no_calls")
        self.assertTrue(integration["degraded"])
        self.assertIn("no editor calls", integration["reason"])


if __name__ == "__main__":
    unittest.main()
