# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 yuki-inaho (fork addition: ordinary-task subprocess E2E)
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

"""End-to-end `harness2.cli.ordinary` runs against a synthetic OpenCode executable.

Every case here launches the real CLI as a subprocess, with a fake `opencode` first on
PATH. The fake speaks the installed 2.1 JSONL protocol; no service, network, or model is
contacted. The assertions check the process exit code *and* the native status and editor
health recorded in `report.json`, because fallbacks can make an outage look successful.

The fake is driven by `HARNESS2_FAKE_MODE`:

* `healthy` -- edit the copied harness and print one text event plus a step cost
* `all_failed` -- print an error event and exit 1 on every session
* `single_failed` -- fail only the `dom1.a1` session, so other editor calls succeed
* `answer_then_error` -- write `output/answer.md` first, then print an error event
* `timeout` -- never produce an event, so the adapter's wall bound kills it
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ordinary-task"

FAKE_OPENCODE = '''#!/usr/bin/env python3
"""Synthetic OpenCode 2.1 CLI: JSONL events only, no model."""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def emit(*events):
    for event in events:
        print(json.dumps(event))
    sys.stdout.flush()


def step_finish(cost=0.25):
    return {"type": "step_finish", "part": {"type": "step-finish", "cost": cost}}


def error_event(message):
    return {"type": "error", "error": {"name": "SyntheticError", "data": {"message": message}}}


def edit_harness():
    target = Path.cwd() / "context" / "harness" / "scripts" / "summarize.py"
    healthy = HERE / "summarize_healthy.py"
    if target.is_file() and healthy.is_file():
        target.write_text(healthy.read_text(encoding="utf-8"), encoding="utf-8")


def label_of(argv):
    if "--title" in argv:
        return argv[argv.index("--title") + 1]
    return ""


def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print("synthetic-opencode 2.1.0")
        return 0

    mode = os.environ.get("HARNESS2_FAKE_MODE", "healthy")
    label = label_of(argv)

    if mode == "timeout":
        time.sleep(60)
        return 1
    if mode == "all_failed":
        emit(error_event("synthetic outage"))
        return 1
    if mode == "single_failed" and label.startswith("dom1.a1"):
        emit(error_event("synthetic single-stage outage"))
        return 1
    if mode == "answer_then_error":
        edit_harness()
        output = Path.cwd() / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "answer.md").write_text("partial answer from a failed session\\n", encoding="utf-8")
        emit(
            {"type": "text", "part": {"type": "text", "text": "wrote an answer"}},
            step_finish(),
            error_event("session failed after writing output"),
        )
        return 0

    edit_harness()
    emit(
        {"type": "text", "part": {"type": "text", "text": "edited the harness"}},
        step_finish(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

HEALTHY_SUMMARIZE = '''"""Synthetic healthy harness: count additions as well as fixes."""

import sys


def main() -> int:
    lines = []
    with open(sys.argv[1], encoding="utf-8") as source:
        for raw in source:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line.lower())
    adds = sum(1 for line in lines if line.startswith("add:"))
    fixes = sum(1 for line in lines if line.startswith("fix:"))
    print(f"Adds: {adds}")
    print(f"Fixes: {fixes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

BAD_JUDGE = '''"""Synthetic judge that is not valid JSON: measurement is unknown, never zero."""

print("this judge produced no JSON object")
'''


class OrdinaryE2E(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.project = self.tmp / "ordinary-task"
        shutil.copytree(EXAMPLE, self.project)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self._write_executable(self.bin / "opencode", FAKE_OPENCODE)
        (self.bin / "summarize_healthy.py").write_text(HEALTHY_SUMMARIZE, encoding="utf-8")

    def _write_executable(self, path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def manifest(self, **editor) -> Path:
        path = self.project / "manifest.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["editor"].update(editor)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return path

    def use_bad_judge(self) -> None:
        judge = self.project / "tools" / "judge_unknown.py"
        judge.write_text(BAD_JUDGE, encoding="utf-8")
        path = self.project / "manifest.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["judge"]["argv"] = [
            sys.executable,
            "{manifest_dir}/tools/judge_unknown.py",
        ]
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    def run_cli(self, mode: str, output: Path, *, k: int = 1, extra=()) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["HARNESS2_FAKE_MODE"] = mode
        argv = [
            sys.executable,
            "-m",
            "harness2.cli.ordinary",
            "--manifest",
            str(self.project / "manifest.json"),
            "--output",
            str(output),
            "--k",
            str(k),
            "--modes",
            "parallel",
            *extra,
        ]
        return subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def report(self, output: Path) -> dict:
        return json.loads((output / "report.json").read_text(encoding="utf-8"))

    def receipts(self, output: Path) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((output / "audit" / "sessions").glob("*/exit.json"))
        ]

    def test_healthy_run_exits_zero_with_truthful_native_and_editor_health(self):
        proc = self.run_cli("healthy", self.tmp / "healthy")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.report(self.tmp / "healthy")
        row = report["tasks"][0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["errors"], [])
        health = row["integration"]["editor_health"]
        self.assertEqual(health["state"], "all_succeeded")
        self.assertEqual((health["n_sessions"], health["n_failed"]), (4, 0))
        self.assertFalse(row["integration"]["degraded"])
        self.assertEqual(report["editor_health"]["state"], "all_succeeded")
        self.assertEqual(report["degraded_tasks"], [])
        self.assertTrue(all(not item["error"] for item in self.receipts(self.tmp / "healthy")))
        judgment = json.loads((self.tmp / "healthy" / "judgment.json").read_text())
        self.assertTrue(judgment)
        self.assertTrue(all(item["graded"] for item in judgment))

    def test_all_editor_calls_failing_is_partial_not_ok(self):
        proc = self.run_cli("all_failed", self.tmp / "failed")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = self.report(self.tmp / "failed")
        row = report["tasks"][0]
        self.assertEqual(row["status"], "partial", "a failed stage must not be reported as ok")
        # k=1 runs three proposal stages (inv1, dom1, agg1); the experience reflection
        # call after selection also fails but is not a `stages[]` entry of the mode.
        self.assertEqual(len(row["errors"]), 3)
        self.assertTrue(all("proposer execution failed" in e for e in row["errors"]))
        health = row["integration"]["editor_health"]
        self.assertEqual(health["state"], "all_failed")
        self.assertEqual((health["n_sessions"], health["n_failed"]), (8, 8))
        self.assertTrue(row["integration"]["degraded"])
        self.assertEqual(report["degraded_tasks"], ["notes-1"])
        receipts = self.receipts(self.tmp / "failed")
        self.assertEqual(len(receipts), 8)
        self.assertTrue(all("synthetic outage" in item["error"] for item in receipts))

    def test_one_failed_stage_leaves_other_editor_successes_visible(self):
        proc = self.run_cli("single_failed", self.tmp / "one")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = self.report(self.tmp / "one")
        row = report["tasks"][0]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(len(row["errors"]), 1)
        self.assertIn("dom1: proposer execution failed", row["errors"][0])
        health = row["integration"]["editor_health"]
        self.assertEqual(health["state"], "partial")
        self.assertGreater(health["n_succeeded"], 0, "other editor calls did succeed")
        self.assertEqual(health["n_failed"], 2, "the failed stage retried once")
        self.assertFalse(row["integration"]["degraded"])
        self.assertTrue(row["modes"]["agg"]["selection_ok"])
        self.assertEqual(report["degraded_tasks"], [])

    def test_answer_then_error_rejects_the_output_and_is_not_cached(self):
        proc = self.run_cli("answer_then_error", self.tmp / "answer-error")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = self.report(self.tmp / "answer-error")
        row = report["tasks"][0]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["integration"]["editor_health"]["state"], "all_failed")
        receipts = self.receipts(self.tmp / "answer-error")
        self.assertEqual(len(receipts), 8)
        self.assertTrue(any(item["answer_present"] for item in receipts))
        self.assertTrue(all("error event" in item["error"] for item in receipts))
        cached_success = [
            path
            for path in (self.tmp / "answer-error" / "audit").glob("*.meta.json")
            if json.loads(path.read_text(encoding="utf-8")).get("ok") is True
        ]
        self.assertEqual(cached_success, [], "a failed session must not leave a reusable reply")

    def test_a_silent_editor_is_killed_at_the_wall_bound(self):
        self.manifest(timeout_s=1, startup_timeout_s=1)
        proc = self.run_cli("timeout", self.tmp / "timeout")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = self.report(self.tmp / "timeout")
        row = report["tasks"][0]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["integration"]["editor_health"]["state"], "all_failed")
        receipts = self.receipts(self.tmp / "timeout")
        self.assertTrue(receipts)
        self.assertTrue(all(item["timed_out"] for item in receipts))
        self.assertTrue(all("timeout" in item["error"] for item in receipts))

    def test_a_malformed_judge_is_unknown_not_zero(self):
        self.use_bad_judge()
        proc = self.run_cli("healthy", self.tmp / "bad-judge")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.report(self.tmp / "bad-judge")
        row = report["tasks"][0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["integration"]["editor_health"]["state"], "all_succeeded")
        judgment = json.loads((self.tmp / "bad-judge" / "judgment.json").read_text())
        self.assertTrue(judgment)
        self.assertTrue(all(not item["graded"] for item in judgment))
        self.assertTrue(all(item["frac"] is None for item in judgment))
        self.assertTrue(all("judge" in item["error"].lower() for item in judgment))
        comparison = row["modes"]["agg"]["comparison"]
        self.assertFalse(comparison["judged_pair_available"])
        self.assertIsNone(comparison["delta_frac"])

    def test_occupied_output_is_rejected_before_any_run(self):
        output = self.tmp / "occupied"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("existing\n", encoding="utf-8")
        proc = self.run_cli("healthy", output)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("occupied", proc.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing\n")
        self.assertFalse((output / "report.json").exists())
        self.assertFalse((output / "runs").exists())
        self.assertFalse((output / "audit").exists())


if __name__ == "__main__":
    unittest.main()
