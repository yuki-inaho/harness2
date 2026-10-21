# Ordinary tasks

Harness² normally adapts an agent harness for a benchmark cell. This fork adds a small,
additive path for a *plain* task you own: declare the task family in JSON, and the same
native Propose–Probe–Compose, selection, reflection and post-selection judging run
against it.

Nothing in `harness2/core`, `harness2/substrate` or `harness2/benchmarks` is modified
for this feature. `ManifestAdapter` satisfies the existing adapter seam, and
`InstalledOpenCodeProposer` changes only the editor launch.

## Quick start

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e '.[render]'

harness2-ordinary --manifest examples/ordinary-task/manifest.json \
  --output runs/ordinary-1 --k 1 --modes parallel
```

The example is synthetic: one release-notes file, a deliberately incomplete
`scripts/summarize.py`, an executor that runs it, and a judge that recomputes the
counts from the task source. Replace `editor.model` with a provider/model#variant id
your installed OpenCode can serve before a real run.

`--dry-run` prints the resolved plan and writes nothing. A real run writes the plan to
`<output>/preflight.json` before the first rollout.

## Output directory

`--output` must be **fresh**. An existing non-empty directory is rejected before
anything is written, because this CLI has no resume, cache reuse or cross-invocation
continuation. Within one run, an executor call is idempotent for the same immutable
identity (manifest, task, source, base harness, expanded argv, timeouts, editor
settings); it is never reused across runs.

Exit codes: `0` every task finished, `1` at least one task did not — including a task
whose native status is `ok` while every editor call failed or no editor receipt exists (see
[Editor health](#editor-health)) — `2` misconfiguration (manifest, arguments or output
directory).

## Manifest (schema 1)

All paths are relative to the manifest file. Absolute paths, `..` and symlinks that
resolve outside the manifest directory are rejected before any run directory is written.

```json
{
  "schema": 1,
  "name": "ordinary",
  "domain": "release-notes",
  "base_harness": "base",
  "tasks_dir": "tasks",
  "task_prompt": "task.md",
  "task_source": "source",
  "executor": {
    "argv": ["python3", "{manifest_dir}/tools/answer.py",
             "--task", "{task_dir}", "--harness", "{harness_dir}",
             "--out", "{run_dir}/answer.md"],
    "answer": "answer.md",
    "timeout_s": 120,
    "model": "declared-label-only"
  },
  "editor": {
    "channel": "opencode",
    "model": "provider/model#variant",
    "steps": 40,
    "timeout_s": 900,
    "startup_timeout_s": 120,
    "launcher": "service"
  },
  "judge": {
    "argv": ["python3", "{manifest_dir}/tools/judge.py",
             "--task", "{task_dir}", "--answer", "{answer}"],
    "timeout_s": 60
  },
  "prompts": {"work": "summarize the release notes in notes.txt",
              "deliverable_word": "summary"},
  "leak_globs": ["expected*", "judge.json"],
  "obs_trunc": 20000
}
```

| Field | Meaning |
|---|---|
| `name` | Namespace segment: lowercase `[a-z0-9_-]`. |
| `domain` | Library grouping unit; defaults to `name`. |
| `base_harness` | Directory holding the editable c1..c8 tree (OpenCode surface). |
| `tasks_dir` | One directory per task, each with `task_prompt` and optionally `task_source`. |
| `executor.argv` | Literal argv list, run directly with no implicit shell. A string command or an unknown `shell` field is rejected. An argv that explicitly names a shell is executed as written — that is the caller's responsibility, not a sandbox. |
| `executor.answer` | Relative path inside the run directory; must not collide with adapter receipts. |
| `executor.timeout_s` | Positive, bounded wall-clock ceiling. |
| `executor.model` | **Declared label only.** Never used to launch anything and never verified. |
| `editor.model` | The one model that is launched, exactly `provider/model#variant`. |
| `editor.launcher` | `service` (default) or `standalone`; no automatic fallback, no mid-run switch. |
| `judge` | Optional independent command; without it `score()` returns unknown. |
| `leak_globs` | Answer-key file globs, stripped from every copied task input and evidence tree. |
| `obs_trunc` | Per-observation render budget; must be at least 10,000. |

Placeholders available to the executor: `{manifest_dir}`, `{task_id}`, `{task_dir}`,
`{source_dir}`, `{prompt_file}`, `{harness_dir}`, `{run_dir}`. The judge gets the same
minus the harness, plus `{answer}` (a copy of the answer only).

Unknown keys are errors, so a typo cannot silently disable a fence.

## Executor contract

Each rollout copies the task directory (minus `leak_globs`) and the harness into
`<runs>/executions/<task>/<run>/`, expands the argv and runs it with `cwd` set to that
run directory. The adapter never inserts an implicit shell. Receipts written next to the
answer:

- `run.json` — expanded argv, template, timeout, declared executor model and
  `declared_model_verified: false`, plus the identity hashes.
- `stdout.log`, `stderr.log` — the command's real output.
- `exit.json` — return code, signal, `timed_out`, timeout kind, wall time, answer hash.
- `completion.json` — written only after a non-empty answer exists.

The call counts as failed when the wall clock expires (the whole owned process group is
killed), when the return code is non-zero, or when the command exits 0 without a
non-empty answer. A failure raises, which core records as an error-bearing rollout; it
is never turned into an empty success. A descendant that detaches into its own session
(`setsid`, a daemon) is outside that group and can survive the kill; the adapter does
not claim otherwise.

## Editor contract

The editor is the installed OpenCode CLI (verified against 2.1): `opencode run --auto
--format json --model provider/model#variant --title <label> --file MISSION.md <message>`,
with no `--dir` and no `--variant`, and the workspace as `cwd`. Each call gets a fresh
native session, its own workspace, its own `--title` label and its own receipts
(`start.json`, `process.json`, `events.jsonl`, `stderr.log`, `exit.json`) under
`<output>/audit/sessions/<label>/`.

Environment isolation follows the launcher, and `start.json` records it as
`env_keys_set` plus `xdg_isolation`:

| Launcher | XDG data/state | Why |
|---|---|---|
| `service` (default) | Inherited from the invoking environment (`inherited_shared_service`) | The client must resolve the already-running shared OpenCode service. Overriding `XDG_DATA_HOME`/`XDG_STATE_HOME` per call made every client try to start its own managed service on the one fixed global port, so parallel native calls failed against each other and the running service. |
| `standalone` | Per call, under `<output>/audit/sessions/<label>/.xdg/` (`per_call`) | A standalone run starts its own local process, so its database and state are kept out of the shared install. |

In both modes the adapter sets only `PWD` and `OPENCODE_PERMISSION`, which limits
external directory reads to that workspace. It never changes the global OpenCode
configuration, the service port, credentials or the provider, and it never switches
launcher or model on its own.

A startup deadline is enforced separately from the wall-clock deadline, so "no event
yet" is distinguishable from "slow but working". A timeout, a non-zero exit, or an
`error` event is a failure. The launcher is never switched automatically. The adapter
performs no retries of its own and never retries after a run; the native stage loop may
still call the editor up to its transport attempts (2 in recursion), including after a
quota or auth error, and each attempt writes its own receipts.

`editor.steps` is a **configured** limit recorded in the receipts (`steps_configured`,
`steps_enforced: false`). The installed 2.1 CLI exposes no step-limit flag, so this
adapter does not claim the limit was enforced.

Cost is summed from `step_finish.part.cost` when the CLI reports it. If any session did
not, `report.json` reports `cost_usd: null` with `cost_source: "unmeasured"`; an
unmeasured session is never counted as zero.

## Editor health

Native task status and editor health are separate facts. The native stage loop can
finish — selection and reflection included — by falling back to the base harness while
no editor call succeeded, so a green `status` alone does not mean the editor worked.

`report.json` therefore carries `editor_health` (whole run) and, per task,
`integration.editor_health` plus `integration.fallback_labels`, computed from the
`exit.json` receipts of that task's own calls:

```json
{"n_sessions": 8, "n_failed": 8, "n_succeeded": 0, "state": "all_failed",
 "degraded": true, "failed_labels": ["inv1.a1"], "succeeded_labels": []}
```

`state` is `no_calls`, `all_failed`, `partial` or `all_succeeded`. `all_failed` marks
the task degraded: the adapter reports it, lists it under `degraded_tasks`, prints it on
stderr, and the CLI exits `1` even though the native `status` is `ok`. A `partial`
state stays distinct and is not called degraded. For the real editor proposer, an `ok`
native status with `no_calls` is also degraded because it has no editor evidence. The native `status` field is left
untouched, and a judged delta measured while `all_failed` is not a tested model effect
— no editor work produced it.

## Judge contract

The judge is a separate command run after adoption, in its own directory. Staged there
are only a copy of the task prompt, the task source (minus `leak_globs`) and a copy of
the answer — not the harness, the workspace, or the evidence tree. That staging is a
convention, not a sandbox: the judge is an ordinary local process with the invoking
user's filesystem access, and a script that reads absolute paths (including
`{manifest_dir}`) can read anything the user can. It prints one JSON object:

```json
{"criteria": [{"name": "adds", "passed": true}, {"name": "fixes", "passed": false}]}
```

or `{"n_passed": 1, "n_criteria": 2, "all_pass": false}`. Counts must be integers and
booleans must be booleans; `true` as a count, a float count, a non-positive denominator
and a non-zero exit are all *unknown*, never `0/n`: core records the judge as ungraded
and excludes it from the denominator.

`score()` raises unless an adoption record already exists under the task's
`selection/` directory, so judging cannot run before selection. Judge files and expected
answers are never copied into editor or solver evidence.

## What a report may and may not claim

- `executor.model` is a declared label. A report may say the *configuration was
  matched*; it may not claim which model the opaque command actually used. The report
  calls this `configured_match` and records `resolved_executor_model: "opaque"`.
- At `k=1`, native selection delivers the single composed final. That is candidate
  selection among the finals, not a measured improvement over the base harness.
  `report.json` states this and marks `selection_is_a_measurement: false`.
- `judged_pair_available` only means the base and adopted runs were both graded. A
  `delta_frac` is reported only when the pair was judged *and* both runs recorded the
  same manifest, task, source, base harness, normalized argv, timeouts, judge argv and
  editor settings (`configured_match`); otherwise it is `null`. This is not a claim
  about real-world comparability, and the executor's internals remain opaque.
- Failure, a tie, `KEEP_BASE` and "no observed effect" are results and are preserved.
- `editor_health` is derived from the editor's own `exit.json` receipts. A task in state
  `all_failed` is reported as degraded and exits `1`; its native `status` and fallback
  labels are never presented as editor success.

## Limits

- A fresh native session plus a permission fence is **not** an OS sandbox. The fence is
  a tool permission policy; it is not proof that the editor could not read outside its
  workspace.
- In `service` mode the CLI talks to the installed background service and inherits that
  install's XDG data/state, so editor sessions share its database. Isolation between
  calls is a fresh native session, a separate workspace, label and receipt directory —
  not a separate database, and no stronger isolation is claimed. `standalone` keeps its
  per-call XDG root but, like any local OpenCode process, is still not an OS sandbox.
- The judge gate checks that an adoption record exists; it is not a lock against a
  separately written file, and its receipts live under `<runs>/judge/<task>/<run>/` so
  one task's evidence is never overwritten by another's.
- A timeout kills only children that remain in the process group the adapter owns; a
  descendant that calls `setsid` or daemonizes is outside it and can survive.
- The example models and labels are synthetic placeholders, not a recommendation.
