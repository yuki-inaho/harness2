"""Example task executor: run the harness summarizer and write its stdout as the answer.

Copyright 2026 yuki-inaho (fork example).

No implicit shell: the manifest's argv list is executed directly, this script
receives absolute paths, and nothing here reads a judge file or an expected answer.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    script = args.harness / "scripts" / "summarize.py"
    notes = args.task / "source" / "notes.txt"
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(script), str(notes)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(proc.stdout, encoding="utf-8")
    (args.out.parent / "trajectory.md").write_text(
        "[ACTION] run scripts/summarize.py on source/notes.txt\n"
        f"[OBSERVATION] {proc.stdout.strip() or '(no output)'}\n"
        + (f"[ERROR] {proc.stderr.strip()}\n" if proc.stderr.strip() else "")
        + f"[ACTION] wrote {args.out.name} after {time.time() - started:.2f}s\n",
        encoding="utf-8",
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
