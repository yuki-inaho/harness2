# ADR 0001: Adopt selected upstream fixes, pin the dev environment with uv, adapt the dashboard

- Status: Accepted
- Date: 2026-10-21
- Context: fork of `google-research/harness2` at upstream
  `1cfe254e8d25b15f37048c3730134686abe60335`

## Context

The fork runs the same recursive method as upstream plus an ordinary-task path
(`harness2-ordinary`) that drives the installed OpenCode CLI. Upstream has since
fixed a set of failure-visibility, cache-integrity, packaging and runner-defect
issues. Without those fixes this fork can report a failed executor as a clean
run, lose the evidence of a killed rollout, and cannot be installed from a
source distribution with its benchmark assets.

At the same time the repository had no pinned development environment: the
documented flow was `uv venv` + `uv pip install -e`, which resolves fresh
versions on every machine and leaves no lock file to reproduce a test run.

Three upstream changes needed a decision rather than a merge:

* `#93` adds a dashboard for benchmark run trees. Its chips colour agreement
  with a "reference total" green/red, which reads as correctness even though no
  judge graded anything.
* `#116` adds OpenRouter model support, but as a broad rewrite of benchmark
  configuration, credential plumbing and install paths, together with
  developer-local agent settings that are not part of this project.
* `#112` isolates JobBench rollout installs and temp files with environment
  variables and a permission fence, which is **not** an OS sandbox.

## Decision

1. **Adopt the selected upstream fixes, one scoped hunk at a time.** The PRs,
   their head commits and the files they touch are listed in
   [docs/UPSTREAM_FIXES.md](../UPSTREAM_FIXES.md). Each adopted area carries a
   regression test that fails on the unpatched tree.
2. **Pin the combined vendor file, not the per-PR hash.** `#23` and `#78` both
   edit `third_party/lab_runner/opencode/run_opencode.py`. The shipped file is
   the combination, so `dependencies/ASSETS.json` records the combined SHA-256
   and `scripts/check_provenance.py` verifies it. Upstream licenses and
   `PROVENANCE.md` stay in place.
3. **Drive the development environment with uv and a lock file.** Add
   `.python-version` (`3.12`), a `dev` dependency group (`pytest`, `ruff`,
   `setuptools>=77` for the source-distribution test) and `uv.lock`.
   The documented flow becomes `uv sync --locked --extra render` followed by
   `uv run --locked pytest tests -q` / `uv run --locked ruff check .`.
4. **Adapt the dashboard instead of vendoring it.** `harness2/cli/dashboard.py`
   keeps the upstream single-file, dependency-free page, the demo/sweep view,
   `--compare` and `--open`, and adds the ordinary-task view where the judge is
   the only measurement: a disabled, missing or failed judge is `unknown`, and
   agreement with a reference output is shown as *unverified agreement*, never
   as accuracy or correctness.
5. **Do not apply `#116` wholesale.** The fork's ordinary path already launches
   the installed OpenCode CLI with an explicit `provider/model#variant` from the
   manifest, so provider selection does not need an OpenRouter-specific rewrite.
   The reasons, and what is *not* claimed, are recorded below.

### How provider selection works in this fork

* **Benchmark path (upstream, unchanged).** `harness2/config.py` reads
  `HARNESS2_SOLVER_MODEL` in OpenCode's `provider/model` syntax and
  `validate_models()` rejects a value without a `/`. The benchmark launchers pass
  that string to the pinned OpenCode CLI as `--model`. Setup-time route
  validation for WorkBuddy still requires a `google-vertex/gemini…` executor and
  a bare `gemini…` judge, as documented in `docs/BENCHMARKS.md`.
* **Ordinary path.** `manifest.json` carries `editor.model`, validated as
  `provider/model#variant` by `harness2-ordinary`, and
  `InstalledOpenCodeProposer` passes exactly that string to
  `opencode run --model …`. There is no fallback provider and no mid-run model
  switch. The executor's *declared* model label is metadata only: the adapter
  never verifies which model the opaque executor command used
  (`declared_model_verified: false`, `resolved_executor_model: "opaque"`).
* **What this does not claim.** Choosing `openrouter/…` for an ordinary run is a
  manifest edit, not a code change, and the run only works if the installed
  OpenCode CLI can serve that provider in that environment. This fork makes **no
  benchmark-level OpenRouter support claim**, ships no provider credentials, and
  does not change the benchmark route validation.

## Consequences

* A failing LAB rollout now propagates a non-zero exit status, and a timeout is
  handled in Python instead of by the external GNU `timeout`, which is absent on
  a default macOS install. `SIGTERM` goes to the process group, and whatever is
  still in the group after the leader exits is `SIGKILL`ed, so a supervisor that
  exits promptly cannot leave a working child behind a `124`. The recorded
  statuses (`124`, `137`, `127`) match the previous contract.
* A JobBench rollout that exits non-zero, or that is killed, now closes its
  trajectory with an `error` event and a log line; a retried attempt keeps its
  own note. Output acceptance is unchanged: files already in the output
  directory are still kept as the deliverable.
* JobBench rollouts no longer share `npm`/`pip --user` installs or `TMPDIR`; a
  retry starts from a clean sandbox. This is **environment and permission
  isolation inside the rollout**, and it is not an OS sandbox: the agent still
  runs as the invoking user and the fence is a tool-permission policy.
* Source distributions now contain `third_party/` and
  `dependencies/ASSETS.json`, so `harness2-setup` and
  `scripts/check_provenance.py` work from an extracted sdist. Wheels still ship
  only the Python package, which `third_party/README.md` states.
* The development interpreter is pinned to Python 3.12 by `.python-version`;
  `pyproject.toml` still declares `>=3.12,<3.14`, so 3.13 remains installable
  even though the lock is tested on 3.12.
* `uv.lock` is now a reviewable artifact: a dependency change means a lock diff.
* Every combined vendor edit requires recomputing the `ASSETS.json` entry and
  keeping `PROVENANCE.md` accurate; the packaging test fails loudly if the
  archive and the pins disagree.
* The dashboard never presents a majority of totals as a score. The cost is that
  runs without a judge look less "finished": they show `unknown` and an
  unverified agreement banner. That is the intended trade.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Cherry-pick every PR branch wholesale | `#23` and `#78` edit the same vendor file and each head is tested against the upstream base only; a wholesale pick either conflicts or imports changes nobody re-tested against this fork. |
| Copy the per-PR `ASSETS.json` hashes | They describe each PR head, not the combined file; provenance would silently fail or, worse, pass on a file nobody tested. |
| Keep `uv venv` + `uv pip install -e` and add a `requirements.txt` | Still unpinned transitively, and two dependency descriptions drift. A lock plus a dev group is one source of truth. |
| Vendor `#93` unchanged and add a disclaimer | Colour and wording would still imply "correct" versus "incorrect" without a judge, and the page would not read `report.json` at all. |
| Build the ordinary dashboard in a separate tool/repository | A second install path for one HTML page; the page needs no dependencies, so it belongs next to the CLIs it describes. |
| Apply `#116` wholesale | It rewrites benchmark configuration and install paths that this fork does not need, and it carries developer-local agent settings that must not be published. |
| Cherry-pick only `#116`'s provider config | The benchmark config is not where provider selection happens in the ordinary path; a partial pick would imply support that is neither tested nor needed. |
| Describe `#112` as sandboxing | It is not an OS sandbox; claiming so would misstate the isolation guarantee. |

## Validation

Run from the repository root on this checkout (Linux, CPython 3.12.14 from
`uv`):

```bash
uv sync --locked --extra render
uv run --locked pytest tests -q
uv run --locked ruff check .
uv run --locked python scripts/check_provenance.py
```

Observed for the tooling change set:

* `uv sync --locked --extra render` resolved the lock and installed the project
  plus the `dev` group and the `render` extra; `uv lock --check` reports the lock
  up to date.
* `uv run --locked python scripts/check_provenance.py` → `PASS: 47 release asset
  checksums`, with the combined runner hash recomputed after the group-stop
  change.
* Each adopted area carries a regression test that fails on the unpatched tree
  and passes after the scoped patch: a wrong exit status, a missing
  `_terminate_process`/`_isolation_env`/`_note_exit` symbol, a missing
  `.env.example`, an sdist without `third_party/`, and — for the dashboard — a
  module that does not exist yet. The captured before/after output is kept with
  the integration report.
* `tests/test_dashboard.py` drives the dashboard CLI as a subprocess over
  synthetic run directories (judged success, failed task, unknown judge,
  non-finite numbers, escaping, demo agreement view, `--compare`, usage errors).
* `harness2-demo` (deterministic editors, real Python executions) produced a run
  directory that `harness2-dashboard --run …` rendered with the
  unverified-agreement banner.
* The ordinary CLI was also driven end to end by a fixture `opencode` executable
  and the example judge: the real `report.json` carried a graded pair
  (base 50%, adopted 100%, Δ +50 pt), a failing judge produced `unknown`, and a
  failing executor produced a task with a failed native status (`base-failed`)
  and no judge record. The dashboard rendered all three from those files. A
  failed proposer stage now also sets the native task status to `partial` with
  one `errors` entry per stage, so a stage outage is visible even when the
  fallbacks still deliver an output.

Limits of this validation: no model-backed run was performed for this change
set, so "the tests pass" means the subprocess and report contracts hold, not
that any model improved. The macOS-specific `#78` motive (GNU `timeout` absent)
was verified by driving the Python timeout path with an empty `PATH` on Linux,
not on a Mac. Wheels still do not ship `third_party/`; only source
distributions do.
