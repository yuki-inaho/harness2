# harvey-labs runner assets

These runners derive from the harvey-labs integration and retain its
[upstream license](LICENSE). See the [pinned revision and patches](../../dependencies/PINNED.md).

The integration adds editable harness installation and configurable models,
paths, and endpoints. Local model-provider names, environment variables, and
log paths use the `harness2` namespace. Setup installs these runners into the LAB checkout as overlays.

[ASSETS.json](../../dependencies/ASSETS.json) records the shipped file checksums.
Verify them with `python scripts/check_provenance.py`.

## Adopted upstream changes

`opencode/run_opencode.py` carries two upstream fixes and one local hardening:

- **Executor exit status.** Upstream PR #23
  (head `20872e5f989721aa9bb52dd7cec04346d9e88d0a`): the CLI propagates the executor
  exit status to its caller, so a failed run can no longer be reported as clean; the
  captured session, its stderr log and the diagnostic metrics are retained for the
  failed attempt.
- **No GNU `timeout` dependency.** Upstream PR #78
  (head `5ad053ca8a163040cab4151feaa3e05e9d14754f`): the wall-clock timeout is enforced
  in Python, in a process group the runner owns, instead of by an external `timeout`
  command that a default macOS installation does not provide. `124` means the executor
  exited after `SIGTERM`, `137` means it had to be killed, `127` means it could not be
  started.
- **Group stop includes children (fork addition).** #78 waited for the leader only, so a
  child that ignored `SIGTERM` survived the timeout. The runner now observes the
  leader's exit without reaping it, then `SIGKILL`s whatever remains in its process
  group; a descendant that leaves the group (`setsid`, a daemon) is still out of reach.
  `tests/test_lab_runner_timeout.py` pins both the statuses and the survivor case.

Because these three changes are combined in one file, the checksum recorded for
`opencode/run_opencode.py` in [ASSETS.json](../../dependencies/ASSETS.json) describes the
combined file, not either PR head.
