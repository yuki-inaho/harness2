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

"""Exercise LAB subprocess handling without downloading benchmark dependencies."""

import ast
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

RUNNER = (
    Path(__file__).resolve().parents[1]
    / "third_party/lab_runner/opencode/run_opencode.py"
)


@unittest.skipUnless(os.name == "posix", "LAB runners use POSIX process groups")
class LabRunnerTimeoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Import only the subprocess functions: the full runner imports the external
        # benchmark's harness, tools and document parsers.
        source = ast.parse(RUNNER.read_text(encoding="utf-8"))
        functions = [
            node
            for node in source.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"_leader_exited", "_signal_group", "_terminate_process", "invoke"}
        ]
        namespace = {
            "BENCH_ROOT": self.root,
            "Path": Path,
            "os": os,
            "signal": signal,
            "subprocess": subprocess,
            "time": time,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER), "exec"), namespace)
        self.invoke = namespace["invoke"]
        self.terminate = namespace["_terminate_process"]

    def invoke_python(self, script, *, append=False):
        return self.invoke(
            ["-c", script],
            base=[sys.executable],
            env={**os.environ, "PATH": ""},
            timeout=30,
            session_path=self.root / "session.jsonl",
            log_path=self.root / "stderr.log",
            append=append,
        )

    def start_process(self, handler):
        script = (
            "import signal, sys, time\n"
            f"signal.signal(signal.SIGTERM, {handler})\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.cleanup_process, proc)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc

    @staticmethod
    def cleanup_process(proc):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()

    def test_run_without_timeout_command_preserves_output_and_exit_status(self):
        self.assertEqual(
            self.invoke_python(
                "import sys; print('event'); print('diagnostic', file=sys.stderr); sys.exit(7)"
            ),
            7,
        )
        self.assertEqual(self.invoke_python("print('continued')", append=True), 0)
        self.assertEqual((self.root / "session.jsonl").read_text(), "event\ncontinued\n")
        self.assertEqual((self.root / "stderr.log").read_text(), "diagnostic\n")

    def test_launch_error_is_recorded_in_stderr(self):
        result = self.invoke(
            [],
            base=[str(self.root / "missing-opencode")],
            env={},
            timeout=30,
            session_path=self.root / "session.jsonl",
            log_path=self.root / "stderr.log",
            append=False,
        )
        self.assertEqual(result, 127)
        error = (self.root / "stderr.log").read_text()
        self.assertIn("Could not start OpenCode: FileNotFoundError", error)
        self.assertIn("missing-opencode", error)

    def test_timeout_remains_failure_after_graceful_shutdown(self):
        proc = self.start_process("lambda *_: sys.exit(0)")
        self.assertEqual(self.terminate(proc, grace=2), 124)
        self.assertEqual(proc.returncode, 0)

    def test_timeout_escalates_when_sigterm_is_ignored(self):
        proc = self.start_process("signal.SIG_IGN")
        self.assertEqual(self.terminate(proc, grace=0.1), 137)
        self.assertEqual(proc.returncode, -signal.SIGKILL)

    def test_group_is_stopped_when_only_a_child_ignores_sigterm(self):
        """A leader that exits on SIGTERM must not leave an owned child running."""
        marker = self.root / "child-ready"
        child = self.root / "child.py"
        child.write_text(
            "import pathlib, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"pathlib.Path({str(marker)!r}).write_text('ready')\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        parent = self.root / "parent.py"
        parent.write_text(
            "import signal, subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-u', {str(child)!r}])\n"
            "print('child', child.pid, flush=True)\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "print('ready', flush=True)\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", str(parent)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.cleanup_process, proc)
        child_line = proc.stdout.readline().strip()
        self.assertEqual(child_line.split()[0], "child", child_line)
        child_pid = int(child_line.split()[1])
        self.addCleanup(self.kill_pid_if_alive, child_pid)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists(), "the child never installed its SIGTERM handler")

        self.assertEqual(self.terminate(proc, grace=2), 124)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(self.pid_alive(child_pid, wait=3.0), "child in the runner's group survived")

    @staticmethod
    def pid_alive(pid: int, *, wait: float = 0.0) -> bool:
        """Whether `pid` is still running; a zombie reparented to init is not 'running'."""
        deadline = time.monotonic() + wait
        while True:
            alive = LabRunnerTimeoutTests._pid_running(pid)
            if not alive or time.monotonic() >= deadline:
                return alive
            time.sleep(0.05)

    @staticmethod
    def _pid_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            try:
                return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
            except (OSError, IndexError):
                return False
        return True

    @staticmethod
    def kill_pid_if_alive(pid):
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


if __name__ == "__main__":
    unittest.main()
