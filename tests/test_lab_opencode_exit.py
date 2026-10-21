# SPDX-License-Identifier: Apache-2.0
"""Exercise the bundled LAB CLI with offline task/render and process fixtures.

The CLI is run in a child interpreter whose ``harness``/``opencode`` imports are
replaced by fixtures. The fixture executor is a real executable (a Python script
named ``bun``), so the exit status crosses a real process boundary instead of a
mocked ``subprocess`` call.
"""

import importlib.util
import json
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "third_party/lab_runner/opencode/run_opencode.py"

FAKE_BUN = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

argv = sys.argv
workspace = Path(argv[argv.index("--dir") + 1])
code = int(os.environ["FIXTURE_RC"])
if code == 0:
    (workspace / "output" / "ledger.txt").write_text("The requested ledger.")
print(json.dumps({"type": "step_finish", "part": {"type": "step-finish",
                   "tokens": {"input": 1, "output": 1}, "cost": 0}}))
sys.stdout.flush()
raise SystemExit(code)
"""


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    return result


def run_fixture(returncode, root):
    docs = root / "documents"
    docs.mkdir()
    checkout = root / "checkout"
    (checkout / "packages/opencode").mkdir(parents=True)
    fake_bun = root / "bin" / "bun"
    fake_bun.parent.mkdir(parents=True)
    fake_bun.write_text(FAKE_BUN)
    fake_bun.chmod(0o755)
    spec = importlib.util.spec_from_file_location(
        "fixture_session", ROOT / "third_party/lab_runner/opencode/session.py"
    )
    session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(session)
    rendered = types.SimpleNamespace(
        prompt="Fixture task",
        agent=None,
        mode="fixture",
        base_skills=[],
        harness_skills=[],
        subagents=[],
        scripts=[],
        plugins=[],
        plugin_tools=[],
        opencode_only=False,
    )
    modules = {
        "harness": module("harness"),
        "harness.run": module(
            "harness.run",
            BENCH_ROOT=root,
            DEFAULT_SKILLS=[],
            _load_env=lambda: None,
            load_task=lambda **kwargs: {
                "docs_dir": docs,
                "instructions": "Write the requested ledger.",
            },
        ),
        "harness.tools": module("harness.tools", get_all_tool_definitions=lambda: []),
        "opencode": module("opencode", session=session),
        "opencode.render": module(
            "opencode.render",
            RenderResult=types.SimpleNamespace,
            render_tree=lambda *args, **kwargs: rendered,
        ),
        "utils": module("utils"),
        "utils.stdio": module("utils.stdio", force_utf8_stdio=lambda: None),
    }

    argv = [
        str(RUNNER),
        "--task",
        "accounting/task1",
        "--run-id",
        "fixture",
        "--model",
        "fixture",
        "--results-root",
        str(root / "results"),
        "--opencode-dir",
        str(checkout),
        "--bun",
        str(fake_bun),
        "--max-continue",
        "0",
    ]
    with (
        patch.dict(sys.modules, modules),
        patch.object(sys, "argv", argv),
        patch.dict("os.environ", {"FIXTURE_RC": str(returncode)}),
    ):
        runpy.run_path(str(RUNNER), run_name="__main__")


class LabOpencodeExitTests(unittest.TestCase):
    def test_cli_propagates_executor_failure_and_preserves_success(self):
        for returncode in (0, 42):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), str(returncode), str(root)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
                metrics = json.loads((root / "results/fixture/metrics.json").read_text())
                self.assertEqual(metrics["exit_code"], returncode)
                self.assertEqual(metrics["finished_cleanly"], returncode == 0)
                graded = root / "results/fixture/output"
                self.assertEqual(
                    [p.name for p in graded.iterdir()] if graded.is_dir() else [],
                    ["ledger.txt"] if returncode == 0 else [],
                )


if __name__ == "__main__":
    run_fixture(int(sys.argv[1]), Path(sys.argv[2]))
