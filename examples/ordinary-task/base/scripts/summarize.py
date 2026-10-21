"""Summarize release notes: count the change lines in one notes file.

The base harness is deliberately incomplete: it counts fixes only. An ordinary run
lets the editor propose and probe a harness that counts additions too.
"""

import sys


def main() -> int:
    lines = []
    with open(sys.argv[1], encoding="utf-8") as source:
        for raw in source:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line.lower())
    fixes = sum(1 for line in lines if line.startswith("fix:"))
    print(f"Fixes: {fixes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
