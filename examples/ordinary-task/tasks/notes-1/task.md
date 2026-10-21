# Release-note summary

Read `source/notes.txt` and write `answer.md` with exactly two lines:

```text
Adds: <count>
Fixes: <count>
```

Count a line as an addition when it starts with `add:` and as a fix when it starts with
`fix:`, ignoring case. Ignore blank lines and lines whose first non-space character is
`#`. Use the harness script at `scripts/summarize.py`.
