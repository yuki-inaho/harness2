# Upstream fixes adopted in this fork

This fork starts from upstream `google-research/harness2` at commit
`1cfe254e8d25b15f37048c3730134686abe60335`. The table records which upstream
pull requests were adopted, at which head commit, and what changed locally.
Upstream text (titles, bodies, patches) is treated as data: every change was
reviewed, applied scoped to the files named here, and re-tested on this
checkout. Nothing is cherry-picked wholesale.

All upstream code keeps its Apache-2.0 license. Patches under `third_party/`
keep their own license copies and `PROVENANCE.md`, and the checksums in
[`dependencies/ASSETS.json`](../dependencies/ASSETS.json) pin the shipped files.

## Adopted

| PR | Title | Head SHA | Scope in this fork |
|---|---|---|---|
| [#110](https://github.com/google-research/harness2/pull/110) | Retry runs after improvement proposer failures | `6656e60c9b85eaa7ea844ebfe6eadfc512deaeae` | `harness2/core/recursion.py`, `harness2/core/schedule.py`: a failed stage is recorded as a partial result instead of being retried into a misleading success |
| [#79](https://github.com/google-research/harness2/pull/79) | Compare reflection diffs against the original base | `a8588ee5c7bad6f0b1b63d6f9fe1ee229e203c49` | `harness2/core/context.py`, `harness2/core/selection.py` |
| [#101](https://github.com/google-research/harness2/pull/101) | A failed mode keeps its paid stage records | `9ae0bd69118fee83f6a96d3a70a0d3554d2f4839` | `harness2/core/schedule.py`: stage records survive the exception path |
| [#87](https://github.com/google-research/harness2/pull/87) | Do not raise on a non-object event field | `785ab54e5b984c8d1364acc1b24a40894eb47e92` | `harness2/substrate/opencode.py`: a string payload no longer raises `AttributeError` |
| [#13](https://github.com/google-research/harness2/pull/13) | Reject OpenCode session errors before caching output | `148b792af95ff7265f90f3678ba2955b045c63b9` | `harness2/core/proposer.py` |
| [#69](https://github.com/google-research/harness2/pull/69) | Reject incomplete audit cache entries | `2b2f3add77279340374eca38f2fe2f22b507c4b9` | `harness2/core/proposer.py` |
| [#48](https://github.com/google-research/harness2/pull/48) | Isolate cached replies and harnesses by channel configuration | `459ced547f10618aca01c35e4056502e15a03941` | `harness2/core/proposer.py` |
| [#104](https://github.com/google-research/harness2/pull/104) | A probe that cannot start its command names what is missing | `e6558df232e07a5e999cc7476269b04f4aaa1643` | `harness2/preflight.py` |
| [#102](https://github.com/google-research/harness2/pull/102) | An OpenCode executor asked twice for the OpenCode checkout | `98ed19bb1854b77cc8a67788b028c2c9b1509a68` | `harness2/preflight.py` |
| [#23](https://github.com/google-research/harness2/pull/23) | Propagate OpenCode executor exit status | `20872e5f989721aa9bb52dd7cec04346d9e88d0a` | `third_party/lab_runner/opencode/run_opencode.py`, `third_party/lab_runner/PROVENANCE.md`, `dependencies/ASSETS.json` |
| [#78](https://github.com/google-research/harness2/pull/78) | Remove the GNU `timeout` dependency from the OpenCode runner | `5ad053ca8a163040cab4151feaa3e05e9d14754f` | `third_party/lab_runner/opencode/run_opencode.py` |
| [#122](https://github.com/google-research/harness2/pull/122) | Note in the trajectory when a rollout exits non-zero | `0d0866e83c4246110289878a4aab022780de74df` | `harness2/benchmarks/jb_opencode_runner.py` |
| [#112](https://github.com/google-research/harness2/pull/112) | Isolate rollouts, temp files and workspaces | `2c12ecd5c0b3c093c728183f12033a46ea738deb` | `harness2/benchmarks/jb_opencode_runner.py` |
| [#7](https://github.com/google-research/harness2/pull/7) | Ship the referenced environment example | `7cd1ea6ea1a250c1794f7878e1552e8867570d16` | `.env.example` (the file `README.md` already told you to copy) |
| [#25](https://github.com/google-research/harness2/pull/25) | Include benchmark assets in source distributions | `5a6f3e8aa5158886915a380e2d648ff0326b1bae` | `MANIFEST.in`, `third_party/README.md` |
| [#93](https://github.com/google-research/harness2/pull/93) | Self-contained HTML view of run directories | `7a2ca03608e753e5dc02b7879b72407e44e0d2bb` | `harness2/cli/dashboard.py` — **adapted**, see below |

Every row above also carries a regression test on this checkout:

| PR | Tests |
|---|---|
| `#110` | `tests/test_proposer_failures.py` |
| `#79` | `tests/test_context_diffs.py` |
| `#101` | `tests/test_workflow.py` |
| `#87` | `tests/test_opencode_events.py` |
| `#13` | `tests/test_proposer_session_errors.py` |
| `#69`, `#48` | `tests/test_proposer_cache.py` |
| `#104` | `tests/test_preflight_probe_messages.py` |
| `#102` | `tests/test_preflight_substrate.py` |
| `#23` | `tests/test_lab_opencode_exit.py` |
| `#78` | `tests/test_lab_runner_timeout.py` |
| `#122` | `tests/test_jobbench_killed_rollout.py` |
| `#112` | `tests/test_jobbench_rollout_isolation.py` |
| `#7` | `tests/test_env_example.py` |
| `#25` | `tests/test_source_distribution.py` |
| `#93` (adaptation) | `tests/test_dashboard.py` |

The `#23`/`#78` tests execute a real child process (a fixture executable named
`bun`), so the exit status crosses a process boundary; they do not mock
`subprocess.run`. `#112` and `#122` drive the real JobBench launcher with a
fixture `bun` on `PATH`. The `#93` tests run the dashboard CLI as a subprocess
over synthetic `report.json` and execution trees, including the ordinary
success, failed-task and unknown-judge outcomes and the escaping of untrusted
strings. None of these tests needs a model, an account or a network connection.

## Combined vendor checksum for #23 + #78

`#23` and `#78` both edit `third_party/lab_runner/opencode/run_opencode.py`, so
the per-PR head hashes cannot be reused: the shipped file is the combination of
both hunks. `dependencies/ASSETS.json` therefore records the combined file, and
`#23` also changed `PROVENANCE.md`:

| Asset | SHA-256 in this fork | Notes |
|---|---|---|
| `third_party/lab_runner/opencode/run_opencode.py` | `d070235a9ff20132b7f54196c9487bfbb6d8516b464fb9215d916b448e101fde` | `#23` returning the executor status, `#78` Python-side timeout handling, and the local group-stop hardening below |
| `third_party/lab_runner/PROVENANCE.md` | `ceeac726e21fdadae555515b1c1e7c4a4ddd5d7e5fdc94c7c10fc843189e76ed` | `#23`'s exit-status note plus the timeout/process-group provenance for `#78` and the fork hardening, so it deliberately differs from the `#23` head |

On the `#23` head alone, `run_opencode.py` hashed to
`6d426bf02c1ca19d3911357137a599a98762a765e213eb8c2ce6b34d29fc957d`; on the
`#78` head alone it is a different file again. The combined file matches neither,
which is exactly why the pin is recomputed here rather than copied from either
PR. Verify with:

```bash
uv run --locked python scripts/check_provenance.py
```

The combined behaviour: `invoke()` starts OpenCode in its own process group with
no external `timeout` command (missing on a default macOS install), returns
`127` when the process cannot start, `124` after a graceful group shutdown and
`137` after an escalated kill, and `main()` returns the executor status so
`python -m opencode.run_opencode` (and therefore the sweep) no longer exits `0`
after a failed run. Session output, stderr capture, metrics and the graded
output copy are unchanged, so a failed attempt still leaves its receipt.

### Local hardening on top of `#78`: the whole group stops

`#78` waits for the leader process only. If the leader exits on `SIGTERM` while a
child in the same group ignores it, that child survives the timeout: the sweep
records `124` and a stray process keeps working on the task. This fork closes
that hole:

* `SIGTERM` is sent to the process group, not to the leader alone.
* The leader's exit is observed *without reaping it* (`waitid(..., WNOWAIT)`),
  so its pid — which is the group id this call owns — cannot be recycled before
  the group is signalled.
* Anything still in the group is then `SIGKILL`ed. The exit status stays `124`
  when the leader exited gracefully and `137` when the leader itself had to be
  killed; a timeout is a failure either way.
* A descendant that leaves the group (`setsid`, a daemon) is out of reach, and
  the docstring says so instead of promising a wider kill.

`tests/test_lab_runner_timeout.py::test_group_is_stopped_when_only_a_child_ignores_sigterm`
reproduces the hole with a synthetic leader and a `SIGTERM`-ignoring child: it
failed before this change (the child survived `124`) and passes after it.

## #93 dashboard: adapted, not vendored

The upstream dashboard reads the benchmark sweep tree (`runs/`, `executions/`)
and colours chips by agreement with a "reference total". That is useful for the
local demo, but on its own it would let a majority of totals look like a score.
The fork keeps the self-contained single-file HTML page, the demo/sweep view,
`--compare` and `--open`, and adds the ordinary-task view:

* `report.json` is the primary input. The **judge** column shows the recorded
  post-selection judge outcome (`selected`, `base`, and `Δ` only when
  `configured_match` is true). `Δ` is withheld otherwise.
* A judge that is disabled, missing, not reached or failed renders as
  **`unknown`** with the reason, styled neutrally. It is never `0`, never a
  failure colour, and never part of an accuracy figure.
* Runs without an independent judge (the demo and the sweeps) show agreement
  with the reference the run itself selected. Every chip, the banner and the
  legend say that agreement is **unverified** and is not accuracy, correctness
  or a measured improvement.
* The reference is the total in the candidate file the selector adopted. The
  upstream fallback "majority among non-base rollouts" is not used, because a
  majority of outputs is not a reference.
* All strings read from a run tree are HTML-escaped (task ids, labels, answers,
  trajectories, judge errors). The page contains no JavaScript, no external
  stylesheet, no image and no network request; `--open` only opens the local
  file with the default browser.

`harness2-dashboard --run <ordinary output>` renders the scored view;
`harness2-dashboard --run <demo output>` renders the unverified agreement view;
`--compare A B` writes a side-by-side page (judge coverage per run, and the
per-task/mode judge cells when every compared run is an ordinary run).

## Considered and not adopted wholesale

| PR | Decision |
|---|---|
| [#116](https://github.com/google-research/harness2/pull/116) | **Not applied wholesale.** See [ADR 0001](adr/0001-upstream-fixes-and-uv.md). The ordinary path already launches the installed OpenCode CLI with an explicit `provider/model#variant`, so an OpenRouter-specific rewrite of the benchmark config, credential plumbing and developer hook files is not needed here. Nothing in this fork claims benchmark-level OpenRouter support. |

## Reproducing the tooling checks

```bash
uv sync --locked --extra render
uv run --locked pytest tests -q
uv run --locked ruff check .
uv run --locked python scripts/check_provenance.py
```

The suite is offline: fixtures are synthetic, and a "passing run" in these tests
means a subprocess contract held, not that a model produced good work.
