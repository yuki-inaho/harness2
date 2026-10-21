# Copyright 2026 Google LLC
# Copyright 2026 yuki-inaho (fork addition: ordinary-task CLI)
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

"""Run a plain task through the native Harness² method from a JSON manifest.

Thin argparse over `core.schedule.run_task` (plan) and `core.judge` (measure): the
CLI builds the manifest adapter and the installed-CLI editor, runs the same
Propose-Probe-Compose / selection / reflection path the benchmarks use, and only
afterwards judges the base and adopted runs. Judging here is a separate, optional step
and never touches the evidence tree.

There is no resume: every invocation needs a FRESH output directory, and an occupied
one is rejected before anything is written.

Exit codes: 0 every task finished, 1 at least one task did not (including a task whose
editor calls all failed, or a real editor task with `ok` native status but no receipts), 2 misconfiguration
(manifest, arguments, or output directory).

    python3 -m harness2.cli.ordinary --manifest examples/ordinary-task/manifest.json \\
        --output runs/ordinary-1 --k 1 --modes parallel
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..core import judge as core_judge
from ..core import schedule
from ..core.library import Library
from ..ordinary import (
    InstalledOpenCodeProposer,
    ManifestAdapter,
    ManifestError,
    editor_health,
    load_manifest,
)
from . import _common as C

NOTE_DECLARED_MODEL = (
    "executor.model is a declared label only: the adapter does not verify which model "
    "the executor command actually used, and no report may claim a resolved model from it."
)
NOTE_EXECUTOR_OPAQUE = (
    "The executor command is opaque: its resolved model, tools and network access are "
    "outside this adapter. The adapter controls only the argv, the copied inputs and the "
    "declared answer file."
)
NOTE_SELECTION = (
    "At k=1 native selection delivers the single composed final; that is candidate "
    "selection among the finals, not a measured improvement over the base harness."
)
NOTE_COST = (
    "Editor cost is null when the installed CLI reported no step cost; an unmeasured "
    "session is never counted as zero. Core keeps 0.0 internally for a reply, so only "
    "this report's cost block is the public cost statement."
)
NOTE_SANDBOX = (
    "A fresh native session plus a permission fence is not an OS sandbox, and the fence "
    "is not proof that the editor could not read outside its workspace."
)
NOTE_STEPS = (
    "editor.steps is a configured limit recorded in the receipts; the installed 2.1 CLI "
    "exposes no step-limit flag, so the adapter does not claim it was enforced."
)
NOTE_RETRY = (
    "The adapter itself never retries or switches model or launcher; the native stage "
    "loop may call the editor up to its transport attempts (2 in recursion), including "
    "after a quota or auth error, and each attempt keeps its own receipts."
)
NOTE_JUDGE = (
    "The judge is an ordinary local command. Only the files staged into its working "
    "directory are controlled; it is not sandboxed and can read any path the invoking "
    "user can."
)
NOTE_EDITOR_HEALTH = (
    "`integration.editor_health` reads the editor exit receipts, not the native task "
    "status. Native selection/reflection can complete by falling back to the base harness "
    "while every editor call failed; a task in state `all_failed`, or an `ok` task with "
    "no editor receipts, is degraded and the run "
    "exits 1. A judged delta measured in that state is not a tested model effect."
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harness2.cli.ordinary", description=__doc__, formatter_class=C.HelpFormatter
    )
    p.add_argument("--manifest", required=True, type=Path, help="path to manifest.json")
    p.add_argument(
        "--output",
        required=True,
        type=Path,
        help="fresh output directory; an occupied one is rejected (no resume)",
    )
    p.add_argument("--k", type=int, default=1, help="task-level recursion budget")
    p.add_argument("--modes", default="parallel", help=C.MODES_HELP)
    p.add_argument("--tag", default="", help="optional namespace tag")
    p.add_argument(
        "--only-tasks", default="", help="comma list restricting the run to these task ids"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved plan as JSON and write nothing",
    )
    p.add_argument(
        "--no-judge",
        action="store_true",
        help="skip the optional post-selection judge even when the manifest declares one",
    )
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        modes = C.parse_modes(args.modes)
        if args.k < 1:
            raise ManifestError(f"--k must be positive, got {args.k}")
        schedule.validate_tag(args.tag)
        output = _checked_output(args.output)
        adapter = ManifestAdapter(manifest, runs_root=output / "runs")
        tasks = adapter.tasks()
        wanted = _csv(args.only_tasks)
        unknown = sorted(set(wanted) - set(tasks))
        if unknown:
            raise ManifestError(
                f"--only-tasks names unknown task(s) {unknown}; the manifest has {tasks}"
            )
        selected = [t for t in tasks if t in wanted] if wanted else tasks
    except (ManifestError, ValueError) as exc:
        print(f"ordinary: {exc}", file=sys.stderr)
        return 2

    plan = adapter.receipt() | {
        "k": args.k,
        "modes": list(modes),
        "tag": args.tag,
        "tasks_selected": selected,
        "output": str(output),
        "judge_enabled": bool(manifest.judge) and not args.no_judge,
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0

    output.mkdir(parents=True, exist_ok=True)
    (output / "runs").mkdir(parents=True, exist_ok=True)
    (output / "audit").mkdir(parents=True, exist_ok=True)
    plan["opencode"] = _probe_opencode()
    plan["python"] = sys.version.split()[0]
    plan["started_at"] = datetime.now(timezone.utc).isoformat()
    (output / "preflight.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

    prop = InstalledOpenCodeProposer(
        manifest.editor,
        audit_dir=output / "audit",
        launcher=manifest.launcher,
        startup_timeout_s=manifest.startup_timeout_s,
    )
    libraries = {
        mode: Library(
            root=output / "library" / mode,
            domain=manifest.domain,
            surface=adapter.substrate.surface(),
        )
        for mode in modes
    }
    judged: dict[tuple[str, str], core_judge.Scored] = {}
    rows: list[core_judge.Scored] = []
    task_rows: list[dict] = []
    try:
        for task_id in selected:
            task_rows.append(
                _run_one(
                    adapter,
                    prop,
                    libraries,
                    task_id,
                    k=args.k,
                    modes=modes,
                    tag=args.tag,
                    judge=bool(manifest.judge) and not args.no_judge,
                    judged=judged,
                    rows=rows,
                )
            )
    except Exception as exc:  # a bug in the adapter/CLI, not a task outcome
        print(f"ordinary: unexpected failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if rows:
        core_judge.write(rows, output / "judgment.json")
    degraded_tasks = [
        str(row["task_id"]) for row in task_rows if (row.get("integration") or {}).get("degraded")
    ]
    run_health = _run_health(prop, task_rows)
    report = {
        "schema": 1,
        "name": manifest.name,
        "manifest_sha256": manifest.sha256,
        "k": args.k,
        "modes": list(modes),
        "tag": args.tag,
        "output": str(output),
        "executor": {
            "declared_model": manifest.executor_model or None,
            "declared_model_verified": False,
        },
        "editor": plan["editor"],
        "judge_enabled": bool(manifest.judge) and not args.no_judge,
        "cost": prop.cost_summary(),
        "editor_health": run_health,
        "degraded_tasks": degraded_tasks,
        "tasks": task_rows,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "notes": [
            NOTE_DECLARED_MODEL,
            NOTE_EXECUTOR_OPAQUE,
            NOTE_SELECTION,
            NOTE_COST,
            NOTE_SANDBOX,
            NOTE_STEPS,
            NOTE_RETRY,
            NOTE_JUDGE,
            NOTE_EDITOR_HEALTH,
        ],
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    failed = [row["task_id"] for row in task_rows if row["status"] not in ("ok", "skipped")]
    print(
        f"ordinary {manifest.name}: {len(task_rows) - len(failed)} task(s) finished, "
        f"{len(failed)} did not, {len(degraded_tasks)} degraded. "
        f"report: {output / 'report.json'}"
    )
    if failed:
        print(f"  unfinished: {', '.join(failed)}", file=sys.stderr)
    if degraded_tasks:
        print(
            f"  degraded (editor calls failed or no editor evidence; native status is not editor success): "
            f"{', '.join(degraded_tasks)}",
            file=sys.stderr,
        )
    return 1 if (failed or degraded_tasks) else 0


def _run_one(
    adapter: ManifestAdapter,
    prop: InstalledOpenCodeProposer,
    libraries: dict[str, Library],
    task_id: str,
    *,
    k: int,
    modes: tuple[str, ...],
    tag: str,
    judge: bool,
    judged: dict[tuple[str, str], core_judge.Scored],
    rows: list[core_judge.Scored],
) -> dict:
    """Run one task natively, then optionally measure base and adopted runs."""
    before = len(_receipts(prop))
    try:
        result = schedule.run_task(
            task_id,
            k,
            adapter=adapter,
            prop=prop,
            runs_root=adapter.runs_root,
            modes=modes,
            tag=tag,
            libraries=libraries,
        )
    except Exception as exc:
        return {
            "task_id": task_id,
            "status": "exception",
            "error": f"{type(exc).__name__}: {exc}",
            "integration": _integration(prop, before, modes, {}, status="exception"),
        }

    row = {
        "task_id": task_id,
        "domain": result.domain,
        "status": result.status,
        "errors": list(result.errors),
        "modes": {},
        "integration": _integration(prop, before, modes, result.modes, status=result.status),
    }
    base = result.base[0] if result.base else None
    for mode in modes:
        mode_rec = result.modes.get(mode) or {}
        sel = result.selections.get(mode) or {}
        ledger = mode_rec.get("ledger") or {}
        block = {
            "finals": mode_rec.get("finals", []),
            "kept_base": mode_rec.get("kept_base", []),
            "fell_back": mode_rec.get("fell_back", []),
            "ledger": {"R": ledger.get("R"), "P": ledger.get("P")},
            "chosen_label": sel.get("chosen_label", ""),
            "chosen_is_base": bool(sel.get("chosen_is_base")),
            "trivial_selection": bool(sel.get("trivial")),
            "candidates": sel.get("n_candidates"),
            "selection_ok": bool(sel.get("ok")),
        }
        if judge and sel.get("ok") and sel.get("chosen_run_dir"):
            chosen_record = {
                "label": sel.get("chosen_label", ""),
                "run_id": sel.get("chosen_run_id", ""),
                "run_dir": sel.get("chosen_run_dir", ""),
            }
            chosen = _scored(
                adapter, result.task_id, mode, k, chosen_record, judged, rows, is_base=False
            )
            base_row = (
                _scored(adapter, result.task_id, mode, k, base, judged, rows, is_base=True)
                if base
                else None
            )
            both = bool(chosen.graded and base_row and base_row.graded)
            matched = _configured_match(base, sel)
            block["comparison"] = {
                "baseline_label": (base or {}).get("label"),
                "selected_label": sel.get("chosen_label"),
                "selection_is_a_measurement": False,
                "judged_pair_available": both,
                "configured_match": matched,
                "resolved_executor_model": "opaque",
                "base_frac": base_row.frac if base_row else None,
                "selected_frac": chosen.frac,
                "delta_frac": (
                    (chosen.frac - base_row.frac) if both and matched and base_row else None
                ),
                "base_error": (base_row.error if base_row else "no base run"),
                "selected_error": chosen.error,
            }
        row["modes"][mode] = block
    return row


def _receipts(prop: object) -> list[dict]:
    """Editor receipts if this proposer records them; a scripted double records none."""
    getter = getattr(prop, "session_receipts", None)
    return list(getter()) if callable(getter) else []


def _run_health(prop: object, task_rows: list[dict]) -> dict:
    """Keep the run-level degraded flag consistent with its task rows."""
    health = editor_health(_receipts(prop))
    health["degraded"] = bool(
        health["degraded"]
        or any((row.get("integration") or {}).get("degraded") for row in task_rows)
    )
    return health


def _integration(
    prop: object, before: int, modes: tuple[str, ...], records: dict, *, status: str
) -> dict:
    """Editor health for this task, from the receipts its own calls wrote.

    `before` is the receipt count on entry, so a later task cannot inherit an earlier
    task's sessions. A proposer without receipts (a scripted double) reports `no_calls`
    rather than an implied success.
    """
    health = editor_health(_receipts(prop)[before:])
    fallback: set[str] = set()
    for mode in modes:
        record = records.get(mode) or {}
        fallback.update(str(label) for label in record.get("fell_back") or [])
    missing_editor_evidence = (
        status == "ok"
        and isinstance(prop, InstalledOpenCodeProposer)
        and health["state"] == "no_calls"
    )
    degraded = bool(health["degraded"] or missing_editor_evidence)
    return {
        "editor_health": health,
        "fallback_labels": sorted(fallback),
        "degraded": degraded,
        "reason": (
            "no editor calls were recorded despite native status ok"
            if missing_editor_evidence
            else "every editor call failed (see the session exit.json receipts); native "
            "status and fallback labels are not editor success"
            if degraded
            else ""
        ),
    }


def _scored(
    adapter: ManifestAdapter,
    task_id: str,
    mode: str,
    k: int,
    record: dict,
    judged: dict[tuple[str, str], core_judge.Scored],
    rows: list[core_judge.Scored],
    *,
    is_base: bool,
) -> core_judge.Scored:
    """Judge a run once per (task, run directory), so the shared base is not measured twice.

    The key carries the task id as well as the run: a run id alone is unique in this
    adapter, but the cache must not depend on that to stay task-correct.
    """
    run_id = str(record.get("run_id") or "")
    run_dir = str(record.get("run_dir") or "")
    key = (task_id, run_dir)
    if run_dir and key in judged:
        return judged[key]
    scored = core_judge.judge_one(
        adapter,
        task_id,
        mode,
        k,
        str(record.get("label") or ""),
        run_id,
        Path(run_dir),
        chosen_is_base=is_base,
    )
    if run_dir:
        judged[key] = scored
    rows.append(scored)
    return scored


def _configured_match(base: dict | None, selection: dict) -> bool:
    """Whether the base and adopted runs recorded the same configuration, harness aside.

    This compares recorded configuration only: it is not a check of the opaque command's
    internals, and it says nothing about which model the executor resolved.
    """
    if not base or not selection.get("chosen_run_dir"):
        return False
    left = _run_identity(base.get("run_dir"))
    right = _run_identity(selection.get("chosen_run_dir"))
    if not left or not right:
        return False
    for identity in (left, right):
        identity.pop("run_id", None)
        identity.pop("harness_sha256", None)
    return left == right


def _run_identity(run_dir) -> dict:
    if not run_dir:
        return {}
    record = _read_json(Path(str(run_dir)) / "run.json")
    identity = record.get("identity")
    return dict(identity) if isinstance(identity, dict) else {}


def _read_json(path: Path) -> dict:
    try:
        got = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return got if isinstance(got, dict) else {}


def _checked_output(path: Path) -> Path:
    """Validate the output root without creating it. An occupied one is rejected."""
    output = Path(path).expanduser()
    if output.is_symlink():
        raise ManifestError(f"--output must not be a symlink: {output}")
    if output.exists():
        if not output.is_dir():
            raise ManifestError(f"--output exists and is not a directory: {output}")
        if any(output.iterdir()):
            raise ManifestError(
                f"--output is occupied: {output}; this CLI has no resume or reuse, so choose "
                f"a fresh directory"
            )
    return output.resolve()


def _probe_opencode() -> dict:
    """Record the installed CLI version. A failure here is data, not a retry."""
    cli = shutil.which("opencode") or "opencode"
    try:
        proc = subprocess.run(
            [cli, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"path": cli, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "path": cli,
        "ok": proc.returncode == 0,
        "version": ((proc.stdout or "") + (proc.stderr or "")).strip()[:200],
        "returncode": proc.returncode,
    }


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
