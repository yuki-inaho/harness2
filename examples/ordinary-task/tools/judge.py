"""Example independent judge: recompute the expected counts from the task source.

Copyright 2026 yuki-inaho (fork example).

It reads only the task material and a copy of the answer file, prints one JSON object,
and never writes into the evidence tree. A missing or malformed answer is a failed
criterion here, not a crash, so the harness never confuses "unreadable" with a zero.
"""

import argparse
import json
import re
from pathlib import Path

_ADD = re.compile(r"^Adds:\s*(\d+)\s*$", re.M)
_FIX = re.compile(r"^Fixes:\s*(\d+)\s*$", re.M)


def expected(notes: Path) -> tuple[int, int]:
    adds = fixes = 0
    for raw in notes.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lower()
        if not line or line.startswith("#"):
            continue
        if line.startswith("add:"):
            adds += 1
        elif line.startswith("fix:"):
            fixes += 1
    return adds, fixes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--answer", type=Path, required=True)
    args = parser.parse_args()

    want_adds, want_fixes = expected(args.task / "source" / "notes.txt")
    try:
        text = args.answer.read_text(encoding="utf-8")
    except OSError:
        text = ""
    got_adds = _ADD.search(text)
    got_fixes = _FIX.search(text)
    criteria = [
        {
            "name": "adds",
            "passed": bool(got_adds) and int(got_adds.group(1)) == want_adds,
        },
        {
            "name": "fixes",
            "passed": bool(got_fixes) and int(got_fixes.group(1)) == want_fixes,
        },
    ]
    print(json.dumps({"criteria": criteria}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
