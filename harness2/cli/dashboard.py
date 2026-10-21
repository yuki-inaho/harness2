# Copyright 2026 Google LLC
# Copyright 2026 yuki-inaho (fork addition: ordinary/judge dashboard adaptation)
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

"""Render a self-contained HTML dashboard from a local harness2 run directory.

Adapted from the upstream `harness2-dashboard` view (run directories of the
benchmark sweeps and the local demo). The ordinary-task run is now the primary
source: its `report.json` carries the post-selection judge outcome, and a judge
that is missing, disabled or failed is displayed as **unknown**, never as `0` and
never as a failure.

Two rules the page keeps:

* Only an independent judge score is presented as a measurement. A *judge*
  cell is coloured by the recorded judge result, and it is labelled `unknown`
  when no pairwise judge record exists.
* Where the run has no independent judge (the local demo and the benchmark
  sweeps), the page shows agreement with the reference output the run selected,
  and says in the cell, the legend and the banner that agreement is
  **unverified** agreement, not accuracy, correctness or a measured delta.

The page is one file: no JavaScript, no external request, no dependency. All
strings read from a run tree are HTML-escaped, because answers, trajectories and
judge errors are model- or task-supplied data.

Usage::

    harness2-dashboard --run runs/ordinary-1          # <run>/dashboard.html
    harness2-dashboard --run runs/ordinary-1 --open    # also opens it locally
    harness2-dashboard --compare runs/a runs/b         # <parent of first>/dashboard_compare.html
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from ._common import HelpFormatter

_TOTAL_RE = re.compile(r"Total:\s*(-?[\d,]+(?:\.\d+)?)")
_ANSWER_CHARS = 400
_TRAJ_CHARS = 1200
_LIB_CHARS = 1000
_REASON_CHARS = 400
_NEUTRAL_STATES = {"ok", "skipped"}

_STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.55 system-ui, sans-serif; margin: 0; padding: 24px;
       background: #f6f7f9; color: #1a1d21; }
main { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 28px 0 8px; border-bottom: 1px solid #d8dce1;
     padding-bottom: 6px; text-transform: uppercase; letter-spacing: .04em; }
h3 { font-size: 14px; margin: 18px 0 8px; }
.sub { color: #5f6771; margin: 0 0 18px; }
.banner { border: 1px solid #b98a00; background: #fff7e0; color: #4d3a00;
          border-radius: 10px; padding: 10px 14px; margin: 12px 0; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.chip { border-radius: 999px; padding: 3px 11px; font-size: 12.5px; font-weight: 600;
        border: 1px solid #cfd4da; background: #fff; color: #2a2f36; white-space: nowrap; }
.chip.base { background: #eceff2; }
.chip.unknown { border-style: dashed; color: #4a5560; background: #f2f4f6; font-weight: 600; }
.chip.agree, .chip.differ { background: #eef2fb; border-color: #8fa2cc; color: #24365e; }
.chip.judged { border-color: #1a7f37; background: #e6f4ea; color: #116329; }
.card { background: #fff; border: 1px solid #d8dce1; border-radius: 10px;
        padding: 14px 16px; margin: 10px 0; }
.card summary { cursor: pointer; font-weight: 600; }
pre { background: #f2f4f6; border: 1px solid #dde1e6; border-radius: 8px;
      padding: 10px 12px; overflow: auto; font-size: 12.5px; white-space: pre-wrap; }
.muted { color: #5f6771; }
table { border-collapse: collapse; margin: 10px 0; font-size: 13px; }
th, td { border: 1px solid #d8dce1; padding: 5px 10px; text-align: left; vertical-align: top; }
th { color: #5f6771; font-weight: 600; background: #eceff2; }
td.good { color: #116329; font-weight: 600; }
td.weak { color: #a5211c; }
td.unknown { color: #4a5560; font-style: italic; }
.bar { display: inline-block; width: 120px; height: 8px; background: #e3e6ea;
       border-radius: 4px; overflow: hidden; vertical-align: middle; }
.bar > span { display: block; height: 100%; background: #1a7f37; }
@media (prefers-color-scheme: dark) {
  body { background: #16181b; color: #e4e7ea; }
  .card, .chip { background: #1e2126; border-color: #33383f; color: #d7dbdf; }
  .chip.base, .chip.unknown { background: #262a30; }
  .banner { background: #2e2610; color: #f0e0b0; border-color: #7a5f10; }
  pre { background: #101216; border-color: #2c3138; }
  h2 { border-color: #33383f; }
  th { background: #262a30; border-color: #33383f; }
  td { border-color: #33383f; }
  td.good { color: #4dd287; }
  td.weak { color: #ff8a84; }
  td.unknown { color: #aeb6bf; }
  .bar { background: #2a2f35; }
  .chip.judged { background: #17351f; color: #7fd6a0; }
  .chip.agree, .chip.differ { background: #1b2437; color: #b9c8e6; }
}
"""


# -- reading helpers -------------------------------------------------------------


def _read_text(path: Path, limit: int | None = None) -> str:
    """Read a text file defensively: missing or unreadable files degrade to ''."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[:limit] if limit and len(text) > limit else text


def _read_json(path: Path) -> dict:
    try:
        got = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return got if isinstance(got, dict) else {}


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _text(value, limit: int = _REASON_CHARS) -> str:
    """One-line, truncated, escaped rendering of an untrusted value."""
    text = " ".join(str(value if value is not None else "").split())
    return _esc(text[:limit] + ("…" if len(text) > limit else ""))


def _parse_total(text: str) -> str | None:
    match = _TOTAL_RE.search(text or "")
    return match.group(1).replace(",", "") if match else None


def _num(value, lower: float = -1.0) -> float | None:
    """A finite fraction in [lower, 1], or None.

    JSON may carry `NaN`/`Infinity` (and some writers do); a non-finite or
    out-of-range value is treated exactly like a missing one so the page can never
    print `nan%` as a graded result. Judge fractions pass `lower=0.0`; only a
    delta may legitimately be negative.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not lower <= number <= 1.0:
        return None
    return number


def _pct(value: float | None) -> str:
    return "unknown" if value is None else f"{value * 100:.1f}%"


def _pt(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.1f} pt"


# -- ordinary runs (report.json) -------------------------------------------------


def _judge_cell(block: dict, judge_enabled: bool) -> dict:
    """Classify the post-selection judge record of one (task, mode) block.

    `graded` requires the pairwise record the CLI writes only after both the
    adopted and the base run were judged. Everything else is `unknown` with a
    reason; it is never rendered as a zero, a failure or an accuracy figure.
    """
    comparison = block.get("comparison")
    if not judge_enabled:
        return {"state": "unknown", "reason": "judge disabled for this run"}
    if not isinstance(comparison, dict):
        reason = (
            "selection did not complete; nothing was judged"
            if not block.get("selection_ok")
            else "no post-selection judge record"
        )
        return {"state": "unknown", "reason": reason}
    selected = _num(comparison.get("selected_frac"), lower=0.0)
    base = _num(comparison.get("base_frac"), lower=0.0)
    if not comparison.get("judged_pair_available") or selected is None or base is None:
        reason = comparison.get("selected_error") or comparison.get("base_error")
        return {
            "state": "unknown",
            "reason": f"judged pair unavailable: {reason}" if reason else "judged pair unavailable",
        }
    matched = bool(comparison.get("configured_match"))
    return {
        "state": "graded",
        "selected_frac": selected,
        "base_frac": base,
        "delta_frac": _num(comparison.get("delta_frac")) if matched else None,
        "configured_match": matched,
        "baseline_label": comparison.get("baseline_label"),
        "selected_label": comparison.get("selected_label"),
        "resolved_executor_model": comparison.get("resolved_executor_model"),
    }


def collect_ordinary(root: Path, report: dict) -> dict:
    """The plain-data model of an ordinary-task run, from its own report.json."""
    judge_enabled = bool(report.get("judge_enabled"))
    editor = report.get("editor") if isinstance(report.get("editor"), dict) else {}
    cost = report.get("cost") if isinstance(report.get("cost"), dict) else {}
    tasks = []
    judged = unknown = 0
    for row in report.get("tasks") or []:
        if not isinstance(row, dict):
            continue
        integration = row.get("integration") if isinstance(row.get("integration"), dict) else {}
        health = (
            integration.get("editor_health")
            if isinstance(integration.get("editor_health"), dict)
            else {}
        )
        modes = []
        for mode, block in sorted((row.get("modes") or {}).items()):
            if not isinstance(block, dict):
                continue
            cell = _judge_cell(block, judge_enabled)
            if cell["state"] == "graded":
                judged += 1
            else:
                unknown += 1
            modes.append({"name": str(mode), "selection": block, "judge": cell})
        if not modes:
            unknown += 1
        tasks.append(
            {
                "task_id": str(row.get("task_id") or ""),
                "domain": str(row.get("domain") or ""),
                "status": str(row.get("status") or "unknown"),
                "errors": [str(e) for e in row.get("errors") or []],
                "error": str(row.get("error") or ""),
                "degraded": bool(integration.get("degraded")),
                "health": health,
                "fallback_labels": [str(x) for x in integration.get("fallback_labels") or []],
                "modes": modes,
            }
        )
    run_health = report.get("editor_health") if isinstance(report.get("editor_health"), dict) else {}
    return {
        "kind": "ordinary",
        "run_root": root.name,
        "name": str(report.get("name") or root.name),
        "k": report.get("k"),
        "modes": [str(m) for m in report.get("modes") or []],
        "tag": str(report.get("tag") or ""),
        "judge_enabled": judge_enabled,
        "editor": editor,
        "cost": cost,
        "editor_health": run_health,
        "degraded_tasks": [str(t) for t in report.get("degraded_tasks") or []],
        "tasks": tasks,
        "judged": judged,
        "unknown": unknown,
        "notes": [str(n) for n in report.get("notes") or []],
        "finished_at": str(report.get("finished_at") or ""),
    }


def _chips(items: Sequence[str]) -> str:
    return "".join(f"<span class='chip base'>{_esc(item)}</span>" for item in items)


def _judge_text(cell: dict) -> str:
    if cell["state"] == "graded":
        text = (
            f"judged: selected {_pct(cell['selected_frac'])} · "
            f"base {_pct(cell['base_frac'])} · Δ {_pt(cell['delta_frac'])}"
        )
        if not cell["configured_match"]:
            text += " (Δ withheld: configuration not matched)"
        return text
    return f"unknown — {cell['reason']}"


def _judge_class(cell: dict) -> str:
    return "good" if cell["state"] == "graded" else "unknown"


def _status_class(status: str) -> str:
    return "good" if status in _NEUTRAL_STATES else "weak"


def _health_chips(health: dict, degraded: bool) -> str:
    if not health:
        parts = ["editor health: n/a"]
    else:
        parts = [
            f"editor calls: {health.get('n_succeeded', 0)}/{health.get('n_sessions', 0)} ok",
            f"state: {health.get('state', 'unknown')}",
        ]
    if degraded:
        parts.append("degraded")
    return _chips(parts)


def render_ordinary(data: dict) -> str:
    health = data["editor_health"]
    parts = [
        "<h1>harness2 dashboard — " + _esc(data["name"]) + "</h1>",
        "<p class='sub'>ordinary-task run · generated by <code>harness2-dashboard</code> · "
        "self-contained, no JavaScript, no network.</p>",
        "<div class='chips'>"
        + _chips(
            [
                f"tasks: {len(data['tasks'])}",
                f"k={data['k']}",
                f"modes: {', '.join(data['modes']) or 'unknown'}",
                "judge: enabled" if data["judge_enabled"] else "judge: disabled (scores unknown)",
                f"judged pairs: {data['judged']}",
                f"unknown: {data['unknown']}",
            ]
        )
        + "</div>",
    ]
    if data["degraded_tasks"]:
        parts.append(
            "<div class='banner'>Degraded: every editor call failed, or no editor receipt "
            "exists, for "
            + _esc(", ".join(data["degraded_tasks"]))
            + ". A judged delta measured in this state is not a tested model effect.</div>"
        )
    cost = data["cost"]
    cost_line = (
        f"editor cost: {cost.get('cost_usd')} USD ({cost.get('cost_source', 'unknown')}; "
        f"{cost.get('n_measured', 0)}/{cost.get('n_sessions', 0)} sessions measured)"
        if cost
        else "editor cost: unknown"
    )
    editor = data["editor"]
    parts.append(
        "<div class='chips'>"
        + _chips(
            [
                cost_line,
                f"editor: {editor.get('channel', 'unknown')} {editor.get('model', '')}".strip(),
                f"launcher: {editor.get('launcher', 'unknown')}",
                "steps enforced: "
                + ("yes" if editor.get("steps_enforced") else "no (configured only)"),
                f"editor health: {health.get('state', 'unknown')}",
            ]
        )
        + "</div>"
    )

    parts.append(
        "<h2>tasks</h2><table><tr><th>task</th><th>mode</th><th>native status</th>"
        "<th>selection</th><th>judge (post-selection)</th><th>editor</th></tr>"
    )
    for task in data["tasks"]:
        modes = task["modes"] or [None]
        for index, mode in enumerate(modes):
            first = index == 0
            task_cell = (
                f"<td rowspan='{len(modes)}'>{_esc(task['task_id'])}</td>" if first else ""
            )
            status_cell = (
                f"<td rowspan='{len(modes)}' class='{_status_class(task['status'])}'>"
                f"{_esc(task['status'])}</td>"
                if first
                else ""
            )
            editor_cell = (
                f"<td rowspan='{len(modes)}'>{_health_chips(task['health'], task['degraded'])}</td>"
                if first
                else ""
            )
            if mode is None:
                parts.append(
                    f"<tr>{task_cell}<td>—</td>{status_cell}<td>—</td>"
                    f"<td class='unknown'>unknown — task did not produce a mode record</td>"
                    f"{editor_cell}</tr>"
                )
                continue
            selection = mode["selection"]
            chosen = selection.get("chosen_label") or "—"
            picked = "base" if selection.get("chosen_is_base") else "candidate"
            sel_text = (
                f"{_esc(chosen)} ({picked}, {_esc(selection.get('candidates', '?'))} candidates)"
                if selection.get("selection_ok")
                else f"failed — {_text(selection.get('error') or 'selection did not complete')}"
            )
            ledger = selection.get("ledger") if isinstance(selection.get("ledger"), dict) else {}
            detail = [
                f"finals: {_esc(len(selection.get('finals') or []))}",
                f"kept base: {_esc(len(selection.get('kept_base') or []))}",
                f"fell back: {_esc(len(selection.get('fell_back') or []))}",
                f"ledger R{_esc(ledger.get('R'))}/P{_esc(ledger.get('P'))}",
            ]
            if selection.get("trivial_selection"):
                detail.append("trivial selection (candidate choice, not a measured improvement)")
            sel_text += f"<br><span class='muted'>{' · '.join(detail)}</span>"
            judge = mode["judge"]
            parts.append(
                f"<tr>{task_cell}<td>{_esc(mode['name'])}</td>{status_cell}"
                f"<td>{sel_text}</td>"
                f"<td class='{_judge_class(judge)}'>{_esc(_judge_text(judge))}</td>"
                f"{editor_cell}</tr>"
            )
    parts.append("</table>")
    if data["unknown"]:
        parts.append(
            "<p class='muted'>`unknown` means no pairwise judge record exists for that task and "
            "mode: the judge is disabled, the selection did not complete, or the judge failed. "
            "It is excluded from any denominator and is not a zero.</p>"
        )

    errors = [
        (task["task_id"], [*task["errors"], task["error"]])
        for task in data["tasks"]
        if task["errors"] or task["error"]
    ]
    if errors:
        parts.append("<h2>errors</h2>")
        for task_id, messages in errors:
            body = "".join(f"<pre>{_text(m, 2000)}</pre>" for m in messages if m)
            parts.append(
                f"<details class='card'><summary>{_esc(task_id)}</summary>{body}</details>"
            )
    if data["notes"]:
        parts.append("<h2>report notes</h2>")
        parts.append(
            "<ul>" + "".join(f"<li>{_esc(n)}</li>" for n in data["notes"]) + "</ul>"
        )
    if data["finished_at"]:
        parts.append(f"<p class='muted'>finished: {_esc(data['finished_at'])}</p>")
    return _page(f"harness2 dashboard — {data['name']}", parts)


# -- agreement view: demo and sweep runs, where no judge exists -------------------


def _stage_of(rollout: str) -> str:
    match = re.match(r"[a-z]+", rollout)
    return match.group(0) if match else rollout


def _collect_rollouts(root: Path, mode: str, task: str) -> list[dict]:
    base = root / "executions" / mode / task
    if not base.is_dir():
        return []
    out = []
    for name in sorted(p.name for p in base.iterdir() if p.is_dir()):
        answer = _read_text(base / name / "answer.txt", _ANSWER_CHARS)
        trajectory = _read_text(base / name / "trajectory.md", _TRAJ_CHARS)
        out.append(
            {
                "name": name,
                "stage": _stage_of(name),
                "total": _parse_total(answer) or _parse_total(trajectory),
                "answer": answer,
                "trajectory": trajectory,
            }
        )
    return out


def _selection(root: Path, mode: str, task: str) -> dict:
    """The stage selections of one (mode, task), with the reference they adopted.

    The reference is the total in the candidate file the selector adopted. It is
    a reference output, not a graded truth: a run without an independent judge
    has no accuracy figure at all.
    """
    out = []
    base = root / "runs" / mode / task / "selection"
    if not base.is_dir():
        return {"stages": [], "reference": None, "reference_from": ""}
    reference, reference_from = None, ""
    for stage_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        record = _read_json(stage_dir / "selection.json")
        chosen_label = str(record.get("chosen_label") or "")
        chosen_id = str(record.get("chosen_id") or "")
        total = None
        candidate = stage_dir / "candidates" / f"{chosen_id}.md"
        if chosen_id and candidate.is_file():
            total = _parse_total(_read_text(candidate, 20_000))
        out.append(
            {
                "stage": stage_dir.name,
                "chosen_label": chosen_label,
                "chosen_is_base": bool(record.get("chosen_is_base")),
                "ok": bool(record.get("ok")),
                "error": str(record.get("error") or ""),
                "total": total,
            }
        )
        if reference is None and total is not None:
            reference, reference_from = total, f"selection {stage_dir.name} → {chosen_label}"
    return {"stages": out, "reference": reference, "reference_from": reference_from}


def _agreement(rollout: dict, reference: str | None) -> str:
    if reference is None or rollout["total"] is None:
        return "unknown"
    return "agree" if rollout["total"] == reference else "differ"


def _run_summary(root: Path, executions: int) -> dict:
    """The run-level counters a demo writes (demo.json); sweeps may have none."""
    data = _read_json(root / "demo.json")
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else None
    if tasks is None:
        return {}
    done = sum(
        1 for t in tasks if isinstance(t, dict) and str(t.get("status")) in _NEUTRAL_STATES
    )
    return {
        "file": "demo.json",
        "done": done,
        "failed": len(tasks) - done,
        "executions": int(data.get("execution_count") or executions),
        "editor_calls": int(data.get("editor_calls") or 0),
    }


def collect_agreement(root: Path) -> dict:
    """The demo/sweep model: per (mode, task) rollouts, totals and the selected reference."""
    runs = root / "runs"
    modes = []
    if runs.is_dir():
        for mode_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
            tasks = []
            for task_dir in sorted(p for p in mode_dir.iterdir() if p.is_dir()):
                if not (root / "executions" / mode_dir.name / task_dir.name).is_dir():
                    continue
                rollouts = _collect_rollouts(root, mode_dir.name, task_dir.name)
                selection = _selection(root, mode_dir.name, task_dir.name)
                for rollout in rollouts:
                    rollout["agreement"] = _agreement(rollout, selection["reference"])
                summary = _read_json(task_dir / "summary.json")
                tasks.append(
                    {
                        "name": task_dir.name,
                        "status": str(summary.get("status") or ""),
                        "rollouts": rollouts,
                        "stages": selection["stages"],
                        "reference": selection["reference"],
                        "reference_from": selection["reference_from"],
                    }
                )
            if tasks:
                modes.append({"name": mode_dir.name, "tasks": tasks})
    executions = sum(len(t["rollouts"]) for m in modes for t in m["tasks"])
    library = root / "library"
    entries = [
        {"file": p.relative_to(root).as_posix(), "text": _read_text(p, _LIB_CHARS)}
        for p in sorted(library.rglob("EXPERIENCE.md"))
    ] if library.is_dir() else []
    return {
        "kind": "agreement",
        "run_root": root.name,
        "modes": modes,
        "executions": executions,
        "summary": _run_summary(root, executions),
        "library": entries,
    }


def render_agreement(data: dict) -> str:
    summary = data["summary"]
    chips = (
        [
            f"tasks ok: {summary['done']}/{summary['done'] + summary['failed']}",
            f"executions: {summary['executions']}",
            f"editor calls: {summary['editor_calls']}",
        ]
        if summary
        else [f"executions: {data['executions']}"]
    )
    chips.append(f"library entries: {len(data['library'])}")
    parts = [
        f"<h1>harness2 dashboard — {_esc(data['run_root'])}</h1>",
        "<p class='sub'>demo / sweep run · generated by <code>harness2-dashboard</code> · "
        "self-contained, no JavaScript, no network.</p>",
        f"<div class='chips'>{_chips(chips)}</div>",
        "<div class='banner'>This run has no independent judge. The chips below show "
        "<strong>agreement with the reference output the run selected — unverified</strong>. "
        "That is not accuracy, not correctness, and not a measured improvement: no judge "
        "graded any output here. Use an ordinary-task run with a judge for a scored view.</div>",
    ]
    for mode in data["modes"]:
        parts.append(f"<h2>mode: {_esc(mode['name'])}</h2>")
        for task in mode["tasks"]:
            reference = task["reference"]
            ref_label = (
                f"{_esc(reference)} ({_esc(task['reference_from'])})"
                if reference is not None
                else "unknown (no selection record)"
            )
            status = (
                f" · status: {_esc(task['status'])}" if task["status"] else ""
            )
            parts.append(
                f"<h3>{_esc(task['name'])} "
                f"<span class='muted'>(reference total: {ref_label}{status})</span></h3>"
            )
            chips = []
            for rollout in task["rollouts"]:
                label = rollout["name"]
                if rollout["total"] is not None:
                    label += f": {rollout['total']}"
                state = rollout["agreement"]
                note = {
                    "agree": "matches reference (unverified)",
                    "differ": "differs from reference (unverified)",
                    "unknown": "total unknown",
                }[state]
                chips.append(
                    f"<span class='chip {state}'>{_esc(label)} — {_esc(note)}</span>"
                )
            parts.append(f"<div class='chips'>{''.join(chips)}</div>")
            for rollout in task["rollouts"]:
                if not rollout["trajectory"]:
                    continue
                parts.append(
                    f"<details><summary>{_esc(rollout['name'])} — trajectory</summary>"
                    f"<pre>{_esc(rollout['trajectory'])}</pre></details>"
                )
            if task["stages"]:
                rows = "".join(
                    f"<li><code>{_esc(s['stage'])}</code> — chosen "
                    f"{_esc(s['chosen_label'] or '—')}"
                    + (" (base)" if s["chosen_is_base"] else "")
                    + (f", total {_esc(s['total'])}" if s["total"] else "")
                    + ("" if s["ok"] else f" — failed: {_text(s['error'])}")
                    + "</li>"
                    for s in task["stages"]
                )
                parts.append(f"<p class='muted'>Selection stages:</p><ul>{rows}</ul>")
    if data["library"]:
        parts.append("<h2>adopted experience library</h2>")
        for entry in data["library"]:
            parts.append(
                f"<details><summary><code>{_esc(entry['file'])}</code></summary>"
                f"<pre>{_esc(entry['text'])}</pre></details>"
            )
    return _page(f"harness2 dashboard — {data['run_root']}", parts)


# -- comparison ------------------------------------------------------------------


def compare(roots: Sequence[Path]) -> dict:
    """Collect one row per run; identical task/mode keys line up across runs."""
    rows = []
    for root in roots:
        report = _read_json(root / "report.json") if (root / "report.json").is_file() else {}
        if report.get("tasks") is not None and "judge_enabled" in report:
            data = collect_ordinary(root, report)
            cells = {}
            for task in data["tasks"]:
                for mode in task["modes"]:
                    cells[f"{task['task_id']} / {mode['name']}"] = mode["judge"]
            rows.append(
                {
                    "name": data["run_root"],
                    "kind": "ordinary",
                    "tasks": len(data["tasks"]),
                    "judged": data["judged"],
                    "unknown": data["unknown"],
                    "degraded": len(data["degraded_tasks"]),
                    "executions": None,
                    "editor_calls": None,
                    "cells": cells,
                }
            )
            continue
        data = collect_agreement(root)
        rows.append(
            {
                "name": data["run_root"],
                "kind": "agreement",
                "tasks": sum(len(m["tasks"]) for m in data["modes"]),
                "judged": 0,
                "unknown": 0,
                "degraded": 0,
                "executions": data["executions"],
                "editor_calls": data["summary"].get("editor_calls"),
                "cells": {},
            }
        )
    return {"runs": rows}


def render_compare(data: dict) -> str:
    runs = data["runs"]
    parts = [
        f"<h1>harness2 run comparison — {len(runs)} runs</h1>",
        "<p class='sub'>generated by <code>harness2-dashboard --compare</code> · "
        "self-contained, no JavaScript, no network.</p>",
        "<table><tr><th>run</th><th>kind</th><th>tasks</th><th>judged pairs</th>"
        "<th>judge unknown</th><th>degraded</th><th>executions</th><th>editor calls</th>"
        "<th>graded share</th></tr>",
    ]
    for row in runs:
        total = row["judged"] + row["unknown"]
        share = (row["judged"] / total) if total else None
        bar = (
            f"<span class='bar'><span style='width:{round(share * 100)}%'></span></span> "
            f"{round(share * 100)}%"
            if share is not None
            else "<span class='muted'>not applicable</span>"
        )
        parts.append(
            f"<tr><td>{_esc(row['name'])}</td><td>{_esc(row['kind'])}</td>"
            f"<td>{row['tasks']}</td><td>{row['judged']}</td><td>{row['unknown']}</td>"
            f"<td>{row['degraded']}</td>"
            f"<td>{'n/a' if row['executions'] is None else row['executions']}</td>"
            f"<td>{'n/a' if row['editor_calls'] is None else row['editor_calls']}</td>"
            f"<td>{bar}</td></tr>"
        )
    parts.append("</table>")
    parts.append(
        "<p class='muted'>`judged pairs` counts (task, mode) pairs with a pairwise judge "
        "record; `judge unknown` counts pairs whose judge is disabled, missing or failed. "
        "`graded share` describes judge coverage only — it says nothing about output quality."
        "</p>"
    )
    keys = sorted({key for row in runs for key in row["cells"]})
    if keys:
        parts.append("<h2>post-selection judge per task / mode</h2>")
        parts.append(
            "<table><tr><th>task / mode</th>"
            + "".join(f"<th>{_esc(r['name'])}</th>" for r in runs)
            + "</tr>"
        )
        for key in keys:
            cells = []
            for row in runs:
                cell = row["cells"].get(key)
                if cell is None:
                    cells.append("<td class='unknown'>not in this run</td>")
                    continue
                cells.append(
                    f"<td class='{_judge_class(cell)}'>{_esc(_judge_text(cell))}</td>"
                )
            parts.append(f"<tr><td>{_esc(key)}</td>{''.join(cells)}</tr>")
        parts.append("</table>")
        parts.append(
            "<p class='muted'>Each cell is an independent judge record for the adopted run and "
            "the base run; `Δ` is withheld unless the two runs recorded the same configuration. "
            "Agreement between runs is not a correctness claim.</p>"
        )
    agreement_runs = [r["name"] for r in runs if r["kind"] == "agreement"]
    if agreement_runs:
        parts.append(
            "<div class='banner'>"
            + _esc(", ".join(agreement_runs))
            + " have no independent judge; their reference-agreement view is unverified "
            "agreement, not accuracy.</div>"
        )
    return _page("harness2 run comparison", parts)


def _page(title: str, parts: Sequence[str]) -> str:
    return "\n".join(
        [
            "<!doctype html><html><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width,initial-scale=1'>",
            f"<title>{_esc(title)}</title><style>{_STYLE}</style></head><body><main>",
            *parts,
            "</main></body></html>",
        ]
    )


# -- CLI -------------------------------------------------------------------------


def _ordinary_report(root: Path) -> dict:
    report = _read_json(root / "report.json")
    if report.get("tasks") is not None and "judge_enabled" in report:
        return report
    return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=HelpFormatter
    )
    parser.add_argument(
        "--run", type=Path, default=Path("runs/demo"),
        help="Run directory to visualize (ordinary report.json, or a demo/sweep tree).",
    )
    parser.add_argument(
        "--compare", type=Path, nargs="+", default=None, metavar="RUN",
        help="Compare two or more run directories side by side (overrides --run).",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output HTML path (default: <run>/dashboard.html, or "
             "<parent of the first run>/dashboard_compare.html when comparing).",
    )
    parser.add_argument(
        "--open", action="store_true",
        help="Open the written file with the local default browser (no network request).",
    )
    args = parser.parse_args(argv)

    if args.compare is not None:
        roots = [path.expanduser().resolve() for path in args.compare]
        if len(roots) < 2:
            print("error: --compare needs at least two run directories", file=sys.stderr)
            return 2
        invalid = [str(root) for root in roots if not _is_run(root)]
        if invalid:
            print(
                "error: not run directories (no report.json or runs/): " + ", ".join(invalid),
                file=sys.stderr,
            )
            return 2
        data = compare(roots)
        output = (args.output or roots[0].parent / "dashboard_compare.html").expanduser()
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_compare(data), encoding="utf-8")
        print(f"comparison: {output} ({len(roots)} runs)")
        if args.open:
            webbrowser.open(output.as_uri())
        return 0

    root = args.run.expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    report = _ordinary_report(root)
    if report:
        data = collect_ordinary(root, report)
        if not data["tasks"]:
            print(f"error: {root / 'report.json'} records no tasks", file=sys.stderr)
            return 2
        page = render_ordinary(data)
        summary = (
            f"{len(data['tasks'])} tasks, {data['judged']} judged pairs, "
            f"{data['unknown']} unknown"
        )
    elif (root / "runs").is_dir() or (root / "executions").is_dir():
        data = collect_agreement(root)
        if not data["modes"]:
            print(f"error: no executed tasks found under {root}", file=sys.stderr)
            return 2
        page = render_agreement(data)
        summary = (
            f"{len(data['modes'])} modes, {data['executions']} executions, "
            f"{len(data['library'])} library entries (agreement view, unverified)"
        )
    else:
        print(
            f"error: {root} has neither an ordinary report.json nor a demo/sweep runs/ tree",
            file=sys.stderr,
        )
        return 2
    output = (args.output or root / "dashboard.html").expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    print(f"dashboard: {output} ({summary})")
    if args.open:
        webbrowser.open(output.as_uri())
    return 0


def _is_run(root: Path) -> bool:
    return root.is_dir() and (
        _ordinary_report(root) or (root / "runs").is_dir() or (root / "executions").is_dir()
    )


if __name__ == "__main__":
    raise SystemExit(main())
