# Copyright 2026 Google LLC
# Copyright 2026 yuki-inaho (fork addition: ordinary-task adapter)
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

"""Ordinary tasks: drive the native Harness² method from a JSON manifest.

This is an additive fork feature. A manifest declares one task family as data --
a literal argv list for a task executor (never run through an implicit shell), an
optional independent judge command, the base harness tree, and the single editor model
to launch -- so a task that is not a benchmark cell still runs the same
Propose-Probe-Compose, selection and reflection path.

Nothing here reimplements the method: `ManifestAdapter` satisfies
`core.spec.BenchmarkAdapter`, and `InstalledOpenCodeProposer` overrides only the
editor launch of `core.proposer.Proposer`. The CLI (`harness2.cli.ordinary`) calls
`core.schedule.run_task` and, separately, `core.judge`. See docs/ORDINARY_TASKS.md.

Honest limits, repeated in that document:

* `executor.model` is a DECLARED label. The adapter never verifies which model the
  command actually used; it only records the label.
* The editor's permission fence is a prompt/tool permission policy, not an OS
  sandbox. A fresh native session is not process isolation.
* An unmeasured editor cost is `null`, never zero.
* At k=1 native selection delivers the single composed final. That is candidate
  selection, not a measured improvement over the base harness.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .core import tree as harness_tree
from .core.proposer import Proposer, _opencode_stdout, _parse_json
from .core.spec import BenchPrompts, BenchSpec, ChannelSpec
from .substrate.opencode import OpencodeSubstrate

MANIFEST_SCHEMA = 1
MAX_TIMEOUT_S = 6 * 3600
MAX_STEPS = 1000
MIN_OBS_TRUNC = 10_000

TOP_KEYS = frozenset(
    {
        "schema",
        "name",
        "domain",
        "base_harness",
        "tasks_dir",
        "task_prompt",
        "task_source",
        "executor",
        "editor",
        "judge",
        "prompts",
        "leak_globs",
        "obs_trunc",
    }
)
EXECUTOR_KEYS = frozenset({"argv", "answer", "timeout_s", "model"})
EDITOR_KEYS = frozenset(
    {"channel", "model", "steps", "timeout_s", "startup_timeout_s", "launcher"}
)
JUDGE_KEYS = frozenset({"argv", "timeout_s"})

EXECUTOR_SLOTS = frozenset(
    {
        "manifest_dir",
        "task_id",
        "task_dir",
        "source_dir",
        "prompt_file",
        "harness_dir",
        "run_dir",
    }
)
JUDGE_SLOTS = frozenset(
    {"manifest_dir", "task_id", "task_dir", "source_dir", "prompt_file", "answer"}
)

RESERVED_RUN_FILES = frozenset(
    {"run.json", "exit.json", "completion.json", "stdout.log", "stderr.log", "trajectory.md"}
)

_EDITOR_MODEL = re.compile(r"[^\s/#]+/[^\s#]+#[A-Za-z0-9._-]+\Z")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")

# Environment the adapter sets for every editor launch, and the extra keys that only a
# `standalone` launch gets. `service` deliberately inherits the invoking environment --
# including the installed CLI's `XDG_DATA_HOME`/`XDG_STATE_HOME` -- because the client
# has to resolve the already-running shared service. Overriding those per call made each
# client try to start its own managed service on the one fixed global port, so parallel
# native calls collided with each other and with the running service.
EDITOR_ENV_BASE = ("PWD", "OPENCODE_PERMISSION")
EDITOR_ENV_XDG = ("XDG_DATA_HOME", "XDG_STATE_HOME")


class ManifestError(ValueError):
    """The manifest is not usable. Raised before any run directory is written."""


@dataclass(frozen=True)
class Command:
    """One argv list and the wall-clock ceiling it runs under. Executed directly."""

    argv: tuple[str, ...]
    timeout_s: int


@dataclass(frozen=True)
class Manifest:
    """A validated manifest. Every path in it is absolute and inside the manifest dir."""

    path: Path
    sha256: str
    name: str
    domain: str
    base_harness: Path
    tasks_dir: Path
    task_prompt: str
    task_source: str
    executor: Command
    answer: str
    executor_model: str
    editor: ChannelSpec
    launcher: str
    startup_timeout_s: int
    judge: Command | None
    prompts: BenchPrompts
    leak_globs: tuple[str, ...]
    obs_trunc: int


def _no_unknown(mapping: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ManifestError(
            f"{where} has unknown key(s) {unknown}; allowed: {sorted(allowed)} "
            f"(a typo must not silently disable a fence)"
        )


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where} must be a non-empty string, got {value!r}")
    if "\x00" in value:
        raise ManifestError(f"{where} must not contain NUL bytes")
    return value


def _int(value: Any, where: str, *, minimum: int = 1, maximum: int = MAX_TIMEOUT_S) -> int:
    """An int in range. `True` is an int in Python, so bool is rejected explicitly."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{where} must be an integer, got {type(value).__name__}")
    if not minimum <= value <= maximum:
        raise ManifestError(f"{where} must be between {minimum} and {maximum}, got {value}")
    return int(value)


def _relative_name(value: Any, where: str) -> str:
    """A relative path that cannot leave the directory it is joined to."""
    raw = _text(value, where).replace("\\", "/")
    parts = [part for part in raw.split("/") if part]
    if raw.startswith("/") or not parts or any(part in (".", "..") for part in parts):
        raise ManifestError(f"{where} must be a relative path inside its directory, got {value!r}")
    return "/".join(parts)


def _under(root: Path, value: Any, where: str) -> Path:
    """Resolve a declared relative path and refuse a symlink that escapes `root`."""
    base = Path(root).resolve()
    resolved = (base / _relative_name(value, where)).resolve()
    if resolved != base and base not in resolved.parents:
        raise ManifestError(f"{where} resolves outside {base} (symlink escape): {resolved}")
    return resolved


def _argv(value: Any, where: str, slots: frozenset[str]) -> tuple[str, ...]:
    """A literal argv list: no implicit shell, no string command, no unknown placeholders.

    An argv that explicitly names a shell (e.g. `["bash", "-c", ...]`) is accepted and run
    as written; that is the caller's choice and not an OS security boundary.
    """
    if not isinstance(value, list) or not value:
        raise ManifestError(
            f"{where}.argv must be a non-empty LIST of strings (a string command is never "
            f"executed), got {type(value).__name__}"
        )
    out: list[str] = []
    for index, element in enumerate(value):
        item = _text(element, f"{where}.argv[{index}]")
        for name in _PLACEHOLDER.findall(item):
            if name not in slots:
                raise ManifestError(
                    f"{where}.argv[{index}] uses unknown placeholder {{{name}}}; "
                    f"available: {sorted(slots)}"
                )
        remainder = _PLACEHOLDER.sub("", item)
        if "{" in remainder or "}" in remainder:
            raise ManifestError(
                f"{where}.argv[{index}] has a brace that is not a known placeholder: {item!r}"
            )
        out.append(item)
    return tuple(out)


def expand_argv(argv: Sequence[str], slots: Mapping[str, str], where: str) -> list[str]:
    """Substitute the declared placeholders; an element that becomes empty is an error."""
    out: list[str] = []
    for element in argv:
        value = _PLACEHOLDER.sub(lambda m: slots[m.group(1)], element)
        if not value:
            raise ManifestError(f"{where} element {element!r} expands to an empty string")
        if "\x00" in value:
            raise ManifestError(f"{where} element {element!r} expands to a NUL byte")
        out.append(value)
    return out


def load_manifest(path: Path | str) -> Manifest:
    """Read, validate and normalise one manifest. Nothing is written."""
    manifest_path = Path(path).expanduser()
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {manifest_path}: {exc}") from exc
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest {manifest_path} is not UTF-8 JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestError(f"manifest {manifest_path} must be a JSON object")
    _no_unknown(doc, TOP_KEYS, "manifest")

    schema = doc.get("schema")
    if isinstance(schema, bool) or schema != MANIFEST_SCHEMA:
        raise ManifestError(f"manifest.schema must be {MANIFEST_SCHEMA}, got {schema!r}")

    root = manifest_path.resolve().parent
    name = _text(doc.get("name"), "manifest.name")
    if not _NAME.fullmatch(name):
        raise ManifestError(
            f"manifest.name {name!r} must be lowercase [a-z0-9_-] and is a namespace segment"
        )
    domain = _text(doc.get("domain", name), "manifest.domain")

    base = _under(root, doc.get("base_harness"), "manifest.base_harness")
    if not base.is_dir():
        raise ManifestError(f"manifest.base_harness is not a directory: {base}")
    tasks = _under(root, doc.get("tasks_dir"), "manifest.tasks_dir")
    if not tasks.is_dir():
        raise ManifestError(f"manifest.tasks_dir is not a directory: {tasks}")

    task_prompt = _relative_name(doc.get("task_prompt", "task.md"), "manifest.task_prompt")
    task_source = _relative_name(doc.get("task_source", "source"), "manifest.task_source")

    executor = doc.get("executor")
    if not isinstance(executor, dict):
        raise ManifestError("manifest.executor must be an object")
    _no_unknown(executor, EXECUTOR_KEYS, "manifest.executor")
    executor_argv = _argv(executor.get("argv"), "manifest.executor", EXECUTOR_SLOTS)
    answer = _relative_name(executor.get("answer"), "manifest.executor.answer")
    if answer.rsplit("/", 1)[-1] in RESERVED_RUN_FILES:
        raise ManifestError(
            f"manifest.executor.answer {answer!r} collides with an adapter receipt; "
            f"pick another name"
        )
    executor_timeout = _int(executor.get("timeout_s"), "manifest.executor.timeout_s")
    executor_model = executor.get("model", "")
    if not isinstance(executor_model, str):
        raise ManifestError("manifest.executor.model must be a string label or omitted")

    editor = doc.get("editor")
    if not isinstance(editor, dict):
        raise ManifestError("manifest.editor must be an object")
    _no_unknown(editor, EDITOR_KEYS, "manifest.editor")
    channel = _text(editor.get("channel"), "manifest.editor.channel")
    if channel != "opencode":
        raise ManifestError(
            f"manifest.editor.channel must be 'opencode' (the only installed launcher this "
            f"fork supports), got {channel!r}"
        )
    model = _text(editor.get("model"), "manifest.editor.model")
    if not _EDITOR_MODEL.fullmatch(model):
        raise ManifestError(
            f"manifest.editor.model must be an exact provider/model#variant id "
            f"(e.g. 'example/provider#high'), got {model!r}"
        )
    steps = _int(editor.get("steps"), "manifest.editor.steps", minimum=1, maximum=MAX_STEPS)
    editor_timeout = _int(editor.get("timeout_s"), "manifest.editor.timeout_s")
    startup_timeout = _int(editor.get("startup_timeout_s"), "manifest.editor.startup_timeout_s")
    if startup_timeout > editor_timeout:
        raise ManifestError(
            f"manifest.editor.startup_timeout_s ({startup_timeout}) must not exceed "
            f"timeout_s ({editor_timeout})"
        )
    launcher = _text(editor.get("launcher"), "manifest.editor.launcher")
    if launcher not in ("service", "standalone"):
        raise ManifestError(
            f"manifest.editor.launcher must be 'service' or 'standalone', got {launcher!r} "
            f"(there is no automatic fallback between them)"
        )

    judge = doc.get("judge")
    judge_cmd: Command | None = None
    if judge is not None:
        if not isinstance(judge, dict):
            raise ManifestError("manifest.judge must be an object when present")
        _no_unknown(judge, JUDGE_KEYS, "manifest.judge")
        judge_cmd = Command(
            argv=_argv(judge.get("argv"), "manifest.judge", JUDGE_SLOTS),
            timeout_s=_int(judge.get("timeout_s"), "manifest.judge.timeout_s"),
        )

    prompts = doc.get("prompts")
    if not isinstance(prompts, dict):
        raise ManifestError("manifest.prompts must be an object")
    try:
        bench_prompts = BenchPrompts(**prompts)
    except TypeError as exc:
        raise ManifestError(f"manifest.prompts has an unknown field: {exc}") from exc
    except ValueError as exc:
        raise ManifestError(f"manifest.prompts: {exc}") from exc

    leak_globs = doc.get("leak_globs", [])
    if not isinstance(leak_globs, list) or not all(
        isinstance(g, str) and g.strip() for g in leak_globs
    ):
        raise ManifestError("manifest.leak_globs must be a list of non-empty glob strings")
    for glob in leak_globs:
        if Path(glob).is_absolute() or ".." in Path(glob).parts:
            raise ManifestError(
                f"manifest.leak_globs entry {glob!r} must be a relative glob inside a task dir"
            )

    obs_trunc = _int(
        doc.get("obs_trunc", 20_000),
        "manifest.obs_trunc",
        minimum=MIN_OBS_TRUNC,
        maximum=1_000_000,
    )

    tasks_found = _task_ids(tasks, task_prompt)
    if not tasks_found:
        raise ManifestError(
            f"manifest.tasks_dir {tasks} has no task directory containing {task_prompt!r}"
        )

    return Manifest(
        path=manifest_path.resolve(),
        sha256=hashlib.sha256(raw).hexdigest(),
        name=name,
        domain=domain,
        base_harness=base,
        tasks_dir=tasks,
        task_prompt=task_prompt,
        task_source=task_source,
        executor=Command(executor_argv, executor_timeout),
        answer=answer,
        executor_model=executor_model,
        editor=ChannelSpec(
            "opencode", model, effort="", steps=steps, timeout=editor_timeout
        ),
        launcher=launcher,
        startup_timeout_s=startup_timeout,
        judge=judge_cmd,
        prompts=bench_prompts,
        leak_globs=tuple(leak_globs),
        obs_trunc=obs_trunc,
    )


def _task_ids(tasks_dir: Path, prompt_name: str) -> list[str]:
    """Sorted task directory names that carry a prompt file; malformed names raise."""
    found: list[str] = []
    for child in sorted(Path(tasks_dir).iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        if not (child / prompt_name).is_file():
            continue
        if not _TASK_ID.fullmatch(child.name):
            raise ManifestError(
                f"task directory {child.name!r} must match [A-Za-z0-9][A-Za-z0-9._-]*"
            )
        found.append(child.name)
    return found


def _tree_digest(root: Path) -> str:
    """Content digest of a directory tree, ignoring byte-compiled byproducts."""
    base = Path(root)
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        digest.update(path.relative_to(base).as_posix().encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _copy_tree(src: Path, dest: Path) -> None:
    """Copy regular files only; symlinks are skipped rather than followed."""
    for path in sorted(Path(src).rglob("*")):
        if path.is_symlink() or not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix == ".pyc":
            continue
        target = Path(dest) / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _is_leaked(rel: str, leak_globs: Sequence[str]) -> bool:
    """Whether a task-relative path matches a declared answer-key glob, bare or nested."""
    for glob in leak_globs:
        pattern = str(glob).replace("\\", "/").lstrip("./")
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, f"*/{pattern}"):
            return True
    return False


def _copy_input(src: Path, dest: Path, leak_globs: Sequence[str]) -> int:
    """Copy a task input tree minus declared answer-key globs; return the file count."""
    copied = 0
    for path in sorted(Path(src).rglob("*")):
        if path.is_symlink() or not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix == ".pyc":
            continue
        rel = path.relative_to(src).as_posix()
        if _is_leaked(rel, leak_globs):
            continue
        target = Path(dest) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied


def _kill_group(process: subprocess.Popen) -> None:
    """Kill the process group this call owns, so same-group children are not orphaned."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: int,
    stdout_path: Path,
    stderr_path: Path,
    startup_timeout_s: int | None = None,
) -> dict:
    """Run one argv without an implicit shell, streaming output to files, and kill the group on timeout.

    A separate startup deadline answers "the process came up but no event arrived", which a
    single wall-clock timeout cannot distinguish from a slow, working session.

    The kill covers the process group this call owns. A descendant that detaches into its
    own session (setsid/daemonize) is outside that group and can survive; that limit is
    documented rather than papered over.
    """
    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    started_unix = time.time()
    with (
        Path(stdout_path).open("wb") as out,
        Path(stderr_path).open("wb") as err,
    ):
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        first_event_s: float | None = None
        timed_out = ""
        while process.poll() is None:
            now = time.monotonic()
            if first_event_s is None and Path(stdout_path).stat().st_size > 0:
                first_event_s = now - started
            if (
                startup_timeout_s is not None
                and first_event_s is None
                and now - started > startup_timeout_s
            ):
                timed_out = "startup_timeout"
                break
            if now - started > timeout_s:
                timed_out = "timeout"
                break
            time.sleep(0.05)
        if timed_out:
            _kill_group(process)
        returncode = process.poll()
    wall_s = round(time.monotonic() - started, 3)
    return {
        "pid": process.pid,
        "started_unix": started_unix,
        "returncode": returncode,
        "signal": -returncode if returncode is not None and returncode < 0 else None,
        "timed_out": bool(timed_out),
        "timeout_kind": timed_out,
        "first_event_s": None if first_event_s is None else round(first_event_s, 3),
        "wall_s": wall_s,
    }


def _step_cost(stdout: str) -> float | None:
    """Sum `part.cost` over step_finish events; None when the stream never reported one."""
    total = 0.0
    seen = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if (part.get("type") or event.get("type")) not in ("step-finish", "step_finish"):
            continue
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
            seen = True
    return round(total, 6) if seen else None


class ManifestAdapter:
    """A `BenchmarkAdapter` whose task family is declared by a manifest, not by code."""

    def __init__(self, manifest: Manifest, *, runs_root: Path):
        self.manifest = manifest
        self.runs_root = Path(runs_root)
        self.substrate = OpencodeSubstrate()
        self.spec = BenchSpec(
            name=manifest.name,
            runs_root=self.runs_root,
            install_mode="workspace_config",
            prompts=manifest.prompts,
            channels={"proposer": manifest.editor, "selector": manifest.editor},
            domain_word=manifest.prompts.domain_word,
            leak_globs=manifest.leak_globs,
            obs_trunc=manifest.obs_trunc,
        )

    # -- benchmark contract -------------------------------------------------------

    def tasks(self, split: str = "all") -> list[str]:
        del split
        return _task_ids(self.manifest.tasks_dir, self.manifest.task_prompt)

    def domain_of(self, task_id: str) -> str:
        del task_id
        return self.manifest.domain

    def task_prompt(self, task_id: str) -> str:
        path = self._task_dir(task_id) / self.manifest.task_prompt
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            raise ManifestError(f"{path} is empty; the task has no prompt")
        return text

    def task_files(self, task_id: str) -> Path | None:
        source = self._task_dir(task_id) / self.manifest.task_source
        return source if source.is_dir() else None

    def base_harness(self) -> Path:
        return self.manifest.base_harness

    def validate_tree(self, harness: harness_tree.Tree) -> None:
        if not harness:
            raise ValueError("The harness tree is empty; keep at least one editable file.")
        always_on = self.substrate.surface().always_on
        if not any(rel in harness for rel in always_on):
            raise ValueError(
                "The harness has no always-on file (" + ", ".join(always_on) + ")."
            )

    def execute(
        self,
        task_id: str,
        harness_dir: Path | None,
        run_id: str,
        *,
        timeout: int | None = None,
    ) -> Path:
        """Run the executor argv once. Succeeds only with a non-empty declared answer file."""
        task_id = self._checked_task(task_id)
        harness_src = Path(harness_dir) if harness_dir else self.manifest.base_harness
        if not harness_src.is_dir():
            raise ManifestError(f"harness directory is missing: {harness_src}")

        run_dir = self.runs_root / "executions" / task_id / _slug(run_id)
        wall = int(timeout or self.manifest.executor.timeout_s)
        slots = {
            "manifest_dir": str(self.manifest.path.parent),
            "task_id": task_id,
            "task_dir": str(run_dir / "input" / "task"),
            "source_dir": str(run_dir / "input" / "task" / self.manifest.task_source),
            "prompt_file": str(run_dir / "input" / "task" / self.manifest.task_prompt),
            "harness_dir": str(run_dir / "harness"),
            "run_dir": str(run_dir),
        }
        argv = expand_argv(self.manifest.executor.argv, slots, "manifest.executor.argv")
        identity = self._identity(task_id, harness_src, run_id, wall, argv, run_dir)
        recorded = _read_json(run_dir / "completion.json")
        if recorded and recorded.get("identity") == identity:
            if self._answer(run_dir).is_file():
                return run_dir

        shutil.rmtree(run_dir, ignore_errors=True)
        task_copy = run_dir / "input" / "task"
        task_copy.mkdir(parents=True, exist_ok=True)
        _copy_input(self._task_dir(task_id), task_copy, self.manifest.leak_globs)
        harness_copy = run_dir / "harness"
        _copy_tree(harness_src, harness_copy)

        env = dict(os.environ)
        env["PWD"] = str(run_dir)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "run_id": run_id,
                    "task_id": task_id,
                    "argv": argv,
                    "argv_template": list(self.manifest.executor.argv),
                    "cwd": str(run_dir),
                    "timeout_s": wall,
                    "declared_executor_model": self.manifest.executor_model or None,
                    "declared_model_verified": False,
                    "identity": identity,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        result = run_bounded(
            argv,
            cwd=run_dir,
            env=env,
            timeout_s=wall,
            stdout_path=run_dir / "stdout.log",
            stderr_path=run_dir / "stderr.log",
        )
        answer = self._answer(run_dir)
        answer_ok = answer.is_file() and bool(answer.read_bytes().strip())
        stderr_tail = _tail(run_dir / "stderr.log", 600)
        error = ""
        if result["timed_out"]:
            error = f"{result['timeout_kind']} after {wall}s"
        elif result["returncode"] not in (0, None):
            error = f"rc={result['returncode']}: {stderr_tail}"
        elif not answer_ok:
            error = f"the command exited 0 but wrote no non-empty {self.manifest.answer!r}"
        (run_dir / "exit.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "run_id": run_id,
                    **result,
                    "answer": self.manifest.answer,
                    "answer_present": answer_ok,
                    "answer_sha256": (
                        hashlib.sha256(answer.read_bytes()).hexdigest() if answer_ok else None
                    ),
                    "stderr_tail": stderr_tail,
                    "error": error,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if error:
            raise RuntimeError(f"[{run_id}] executor failed: {error}; inspect {run_dir / 'exit.json'}")
        (run_dir / "completion.json").write_text(
            json.dumps({"schema": 1, "identity": identity, "run_id": run_id}, indent=2) + "\n",
            encoding="utf-8",
        )
        return run_dir

    def delivered(self, run_dir: Path) -> bool:
        answer = self._answer(Path(run_dir))
        return answer.is_file() and bool(answer.read_bytes().strip())

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        run_dir = Path(run_dir)
        own = run_dir / "trajectory.md"
        if own.is_file():
            text = own.read_text(encoding="utf-8", errors="replace")
        else:
            argv = _read_json(run_dir / "run.json").get("argv") or []
            exit_rec = _read_json(run_dir / "exit.json")
            text = (
                f"[ACTION] execute {' '.join(str(a) for a in argv)}\n"
                f"[OBSERVATION] {_clip(_read_text(run_dir / 'stdout.log'), obs_trunc)}\n"
                f"[ERROR] {_clip(_read_text(run_dir / 'stderr.log'), obs_trunc)}\n"
                f"[ACTION] declared exit code {exit_rec.get('returncode')!r}, "
                f"timed_out={exit_rec.get('timed_out')!r}\n"
            )
        return text[:400_000]

    def deliverable_text(self, run_dir: Path) -> str:
        return _read_text(self._answer(Path(run_dir)))

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        return {self.manifest.answer: self.deliverable_text(run_dir)}

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        """Run the independent judge, and only once an adoption record exists for this task.

        A missing judge block returns None (core records "judge returned nothing"); a
        malformed judge output raises, so it is excluded from the denominator, never 0/n.
        """
        judge = self.manifest.judge
        if judge is None:
            return None
        task_id = self._checked_task(task_id)
        selection = sorted(
            self.runs_root.glob(f"*/{task_id}/selection/*/selection.json")
        )
        if not selection:
            raise RuntimeError(
                f"judge gated: no selection.json under {self.runs_root}/*/{task_id}/selection; "
                f"score() is only reachable after the method adopted a candidate"
            )

        answer_src = self._answer(Path(run_dir))
        if not (answer_src.is_file() and answer_src.read_bytes().strip()):
            raise RuntimeError(f"nothing to judge: {answer_src} is missing or empty")

        judge_dir = self.runs_root / "judge" / task_id / _slug(Path(run_dir).name)
        shutil.rmtree(judge_dir, ignore_errors=True)
        task_copy = judge_dir / "input" / "task"
        task_copy.mkdir(parents=True, exist_ok=True)
        _copy_input(self._task_dir(task_id), task_copy, self.manifest.leak_globs)
        answer_copy = judge_dir / "input" / "answer" / answer_src.name
        answer_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(answer_src, answer_copy)

        slots = {
            "manifest_dir": str(self.manifest.path.parent),
            "task_id": task_id,
            "task_dir": str(task_copy),
            "source_dir": str(task_copy / self.manifest.task_source),
            "prompt_file": str(task_copy / self.manifest.task_prompt),
            "answer": str(answer_copy),
        }
        argv = expand_argv(judge.argv, slots, "manifest.judge.argv")
        env = dict(os.environ)
        env["PWD"] = str(judge_dir)
        result = run_bounded(
            argv,
            cwd=judge_dir,
            env=env,
            timeout_s=judge.timeout_s,
            stdout_path=judge_dir / "stdout.log",
            stderr_path=judge_dir / "stderr.log",
        )
        (judge_dir / "judge.json").write_text(
            json.dumps({"argv": argv, **result}, indent=2) + "\n", encoding="utf-8"
        )
        if result["timed_out"]:
            raise RuntimeError(f"judge {result['timeout_kind']} after {judge.timeout_s}s")
        if result["returncode"] not in (0, None):
            raise RuntimeError(
                f"judge rc={result['returncode']}: {_tail(judge_dir / 'stderr.log', 400)}"
            )
        return _parse_judgment(_parse_json(_read_text(judge_dir / "stdout.log")), judge_dir)

    # -- receipts -----------------------------------------------------------------

    def receipt(self) -> dict:
        """What this run is configured to do, as data. Nothing is launched here."""
        return {
            "schema": 1,
            "manifest": str(self.manifest.path),
            "manifest_sha256": self.manifest.sha256,
            "name": self.manifest.name,
            "domain": self.manifest.domain,
            "tasks": self.tasks(),
            "base_harness_sha256": _tree_digest(self.manifest.base_harness),
            "executor": {
                "argv_template": list(self.manifest.executor.argv),
                "answer": self.manifest.answer,
                "timeout_s": self.manifest.executor.timeout_s,
                "declared_model": self.manifest.executor_model or None,
                "declared_model_verified": False,
            },
            "editor": {
                "channel": self.manifest.editor.kind,
                "model": self.manifest.editor.model,
                "steps_configured": self.manifest.editor.steps,
                "steps_enforced": False,
                "timeout_s": self.manifest.editor.timeout,
                "startup_timeout_s": self.manifest.startup_timeout_s,
                "launcher": self.manifest.launcher,
            },
            "judge": (
                {
                    "argv_template": list(self.manifest.judge.argv),
                    "timeout_s": self.manifest.judge.timeout_s,
                }
                if self.manifest.judge
                else None
            ),
            "leak_globs": list(self.manifest.leak_globs),
            "obs_trunc": self.manifest.obs_trunc,
        }

    # -- internals ----------------------------------------------------------------

    def _task_dir(self, task_id: str) -> Path:
        return self.manifest.tasks_dir / self._checked_task(task_id)

    def _checked_task(self, task_id: str) -> str:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ManifestError(f"task id {task_id!r} is not a safe directory name")
        return task_id

    def _answer(self, run_dir: Path) -> Path:
        return Path(run_dir) / self.manifest.answer

    def _identity(
        self,
        task_id: str,
        harness_src: Path,
        run_id: str,
        wall: int,
        argv: Sequence[str],
        run_dir: Path,
    ) -> dict:
        """Everything that may change a rollout's meaning, hashed or recorded verbatim."""
        judge = self.manifest.judge
        return {
            "manifest_sha256": self.manifest.sha256,
            "task_id": task_id,
            "run_id": run_id,
            "task_sha256": _tree_digest(self._task_dir(task_id)),
            "base_sha256": _tree_digest(self.manifest.base_harness),
            "harness_sha256": _tree_digest(harness_src),
            "executor": {
                "argv_template": list(self.manifest.executor.argv),
                "argv_normalized_sha256": _normalized_argv_digest(argv, run_dir),
                "answer": self.manifest.answer,
                "timeout_s": wall,
                "declared_model": self.manifest.executor_model or None,
            },
            "editor": {
                "model": self.manifest.editor.model,
                "launcher": self.manifest.launcher,
                "steps_configured": self.manifest.editor.steps,
                "steps_enforced": False,
                "timeout_s": self.manifest.editor.timeout,
                "startup_timeout_s": self.manifest.startup_timeout_s,
            },
            "judge": list(judge.argv) if judge else None,
        }


def _parse_judgment(doc: Any, where: Path) -> dict:
    """Validate the judge's JSON. Counts must be integers, bools must be bools."""
    if not isinstance(doc, dict):
        raise ValueError(f"judge output at {where / 'stdout.log'} is not a JSON object")
    criteria = doc.get("criteria")
    if criteria is not None:
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("judge criteria must be a non-empty list")
        names: list[str] = []
        passed = 0
        for index, item in enumerate(criteria):
            if not isinstance(item, dict):
                raise ValueError(f"judge criteria[{index}] must be an object")
            if not isinstance(item.get("passed"), bool):
                raise ValueError(f"judge criteria[{index}].passed must be a boolean")
            names.append(str(item.get("name") or f"criterion-{index + 1}"))
            passed += int(item["passed"])
        all_pass = passed == len(criteria)
        if "all_pass" in doc and doc["all_pass"] is not all_pass:
            raise ValueError("judge all_pass disagrees with its criteria list")
        return {
            "n_passed": passed,
            "n_criteria": len(criteria),
            "all_pass": all_pass,
            "criteria": names,
        }

    n_passed = doc.get("n_passed")
    n_criteria = doc.get("n_criteria")
    for label, value in (("n_passed", n_passed), ("n_criteria", n_criteria)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"judge {label} must be an integer, got {type(value).__name__}")
        if value < 0:
            raise ValueError(f"judge {label} must not be negative")
    if n_criteria <= 0:
        raise ValueError("judge n_criteria must be a positive denominator")
    if n_passed > n_criteria:
        raise ValueError(f"judge n_passed ({n_passed}) exceeds n_criteria ({n_criteria})")
    all_pass = n_passed == n_criteria
    if "all_pass" in doc and doc["all_pass"] is not all_pass:
        raise ValueError(
            f"judge all_pass={doc['all_pass']!r} disagrees with "
            f"n_passed={n_passed}/n_criteria={n_criteria}"
        )
    return {
        "n_passed": n_passed,
        "n_criteria": n_criteria,
        "all_pass": all_pass,
        "criteria": [],
    }


def editor_env(
    cwd: Path, session: Path, launcher: str, base: Mapping[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """The exact environment for one editor launch, and the keys the adapter itself set.

    `service` inherits the invoking environment so the CLI talks to the installed shared
    background service through its own XDG data/state; the adapter only pins `PWD` and
    the permission fence. `standalone` starts its own local process and gets a per-call
    XDG root, which keeps its database and state out of the shared install.
    """
    env = dict(os.environ if base is None else base)
    env["PWD"] = str(cwd)
    env["OPENCODE_PERMISSION"] = json.dumps(
        {
            "external_directory": {
                "*": "deny",
                str(cwd): "allow",
                f"{cwd}/**": "allow",
            },
            "webfetch": "deny",
            "websearch": "deny",
        }
    )
    keys = list(EDITOR_ENV_BASE)
    if launcher == "standalone":
        env["XDG_DATA_HOME"] = str(session / ".xdg" / "data")
        env["XDG_STATE_HOME"] = str(session / ".xdg" / "state")
        keys += list(EDITOR_ENV_XDG)
    return env, keys


def editor_health(receipts: Sequence[Mapping[str, Any]]) -> dict:
    """Summarise actual editor exit receipts; native task status is a separate fact.

    A session counts as failed when its `exit.json` carries a non-empty `error` (timeout,
    non-zero exit, `error` event, or an empty reply). "Every call failed" is the state
    that a green native status must not hide: selection/reflection can still complete by
    falling back to the base harness while no editor work succeeded.
    """
    items = [dict(item) for item in receipts]
    failed = [item for item in items if str(item.get("error") or "").strip()]
    succeeded = [item for item in items if not str(item.get("error") or "").strip()]
    total = len(items)
    if total == 0:
        state = "no_calls"
    elif not succeeded:
        state = "all_failed"
    elif failed:
        state = "partial"
    else:
        state = "all_succeeded"
    return {
        "n_sessions": total,
        "n_failed": len(failed),
        "n_succeeded": len(succeeded),
        "state": state,
        "degraded": state == "all_failed",
        "failed_labels": [str(item.get("label") or "") for item in failed],
        "succeeded_labels": [str(item.get("label") or "") for item in succeeded],
    }


class InstalledOpenCodeProposer(Proposer):
    """Native `Proposer` with only the editor launch replaced by the installed OpenCode CLI.

    Retries, audit files, call fingerprints and `harvest="context/harness"` stay native.
    One session per call: its own workspace, its own native session label and its own
    receipts (`start.json`, `process.json`, `exit.json`, `events.jsonl`). Environment
    isolation follows the launcher: `service` inherits the installed shared service's
    XDG data/state, `standalone` gets a per-call XDG root. A failed launch is reported as
    a failure; it never silently switches launcher or model.
    """

    def __init__(
        self,
        spec: ChannelSpec,
        *,
        audit_dir: Path | None = None,
        attempts: int = 2,
        launcher: str = "service",
        startup_timeout_s: int = 120,
        cli: str | None = None,
    ):
        super().__init__(spec, audit_dir=audit_dir, attempts=attempts)
        if launcher not in ("service", "standalone"):
            raise ValueError(f"launcher must be 'service' or 'standalone', got {launcher!r}")
        self.launcher = launcher
        self.startup_timeout_s = int(startup_timeout_s)
        self.cli = cli or shutil.which("opencode") or "opencode"
        self._tls = threading.local()
        self._costs: list[float | None] = []
        self._costs_lock = threading.Lock()
        self._sessions: list[dict] = []
        self._sessions_lock = threading.Lock()

    def call(self, mission, context_dir, label, **kwargs):  # type: ignore[override]
        self._tls.label = label
        try:
            return super().call(mission, context_dir, label, **kwargs)
        finally:
            self._tls.label = ""

    def cost_summary(self) -> dict:
        """Total editor cost, or null as soon as one session did not report a cost."""
        with self._costs_lock:
            costs = list(self._costs)
        measured = [c for c in costs if c is not None]
        return {
            "n_sessions": len(costs),
            "n_measured": len(measured),
            "cost_usd": (
                round(sum(measured), 6) if costs and len(measured) == len(costs) else None
            ),
            "cost_source": (
                "reported" if costs and len(measured) == len(costs) else "unmeasured"
            ),
        }

    def argv_for(self, label: str) -> list[str]:
        """The exact 2.1 CLI argv. No implicit shell, no `--dir`, no `--variant`."""
        argv = [self.cli, "run"]
        if self.launcher == "standalone":
            argv.append("--standalone")
        argv += [
            "--auto",
            "--format",
            "json",
            "--model",
            self.spec.model,
            "--title",
            label or "ordinary",
            "--file",
            "MISSION.md",
            "Read MISSION.md and do what it asks. Reply with the requested output.",
        ]
        return argv

    def _opencode(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        cwd = Path(cwd)
        (cwd / "output").mkdir(parents=True, exist_ok=True)
        (cwd / "MISSION.md").write_text(
            (f"{system}\n\n---\n\n" if system else "") + mission, encoding="utf-8"
        )
        label = getattr(self._tls, "label", "") or f"session-{time.time_ns()}"
        session = self._session_dir(label, cwd)
        session.mkdir(parents=True, exist_ok=True)

        argv = self.argv_for(label)
        env, env_keys = editor_env(cwd, session, self.launcher)
        (session / "start.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "label": label,
                    "argv": argv,
                    "cwd": str(cwd),
                    "model": self.spec.model,
                    "steps_configured": self.spec.steps,
                    "steps_enforced": False,
                    "launcher": self.launcher,
                    "startup_timeout_s": self.startup_timeout_s,
                    "wall_timeout_s": wall,
                    "env_keys_set": env_keys,
                    "xdg_isolation": (
                        "per_call" if self.launcher == "standalone" else "inherited_shared_service"
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        result = run_bounded(
            argv,
            cwd=cwd,
            env=env,
            timeout_s=wall,
            stdout_path=session / "events.jsonl",
            stderr_path=session / "stderr.log",
            startup_timeout_s=self.startup_timeout_s,
        )
        (session / "process.json").write_text(
            json.dumps(
                {
                    "pid": result["pid"],
                    "started_unix": result["started_unix"],
                    "first_event_s": result["first_event_s"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        raw = _read_text(session / "events.jsonl")
        text, event_error = _opencode_stdout(raw)
        cost = _step_cost(raw)
        answer_path = cwd / "output" / "answer.md"
        answer_text = _read_text(answer_path) if answer_path.is_file() else ""
        chosen = answer_text if answer_text.strip() else text
        error = ""
        if result["timed_out"]:
            error = f"{result['timeout_kind']} after {wall}s (startup bound {self.startup_timeout_s}s)"
        elif event_error:
            error = f"error event: {event_error}"
        elif result["returncode"] not in (0, None):
            error = f"rc={result['returncode']}: {_tail(session / 'stderr.log', 300)}"
        elif not chosen.strip():
            error = "empty reply: no text event and no non-empty output/answer.md"

        (session / "exit.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "label": label,
                    **result,
                    "answer_file": "output/answer.md",
                    "answer_present": bool(answer_text.strip()),
                    "text_chars": len(chosen),
                    "error": error,
                    "cost_usd": cost,
                    "cost_source": "reported" if cost is not None else "unmeasured",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        with self._costs_lock:
            self._costs.append(cost)
        receipt = str(session / "exit.json")
        if self.audit_dir:
            try:
                receipt = str((session / "exit.json").relative_to(self.audit_dir))
            except (TypeError, ValueError):
                pass
        with self._sessions_lock:
            self._sessions.append(
                {
                    "label": label,
                    "receipt": receipt,
                    "error": error,
                    "timed_out": bool(result["timed_out"]),
                    "returncode": result["returncode"],
                    "answered": bool(chosen.strip()),
                }
            )
        if error:
            return "", cost or 0.0, error
        return chosen, cost or 0.0, ""

    def session_receipts(self) -> list[dict]:
        """One entry per editor call, mirroring the `exit.json` each call wrote."""
        with self._sessions_lock:
            return [dict(item) for item in self._sessions]

    def health(self) -> dict:
        """Editor health for every call this proposer made, from its own receipts."""
        return editor_health(self.session_receipts())

    def _session_dir(self, label: str, cwd: Path) -> Path:
        """A per-call receipt directory: a retry of the same label must not erase the first."""
        safe = _SAFE_LABEL.sub("_", label).strip("._") or f"session-{time.time_ns()}"
        root = Path(self.audit_dir) / "sessions" if self.audit_dir else Path(cwd) / ".session"
        candidate = root / safe
        index = 1
        while candidate.exists() and index < 1000:
            index += 1
            candidate = root / f"{safe}.{index}"
        return candidate


def _normalized_argv_digest(argv: Sequence[str], run_dir: Path) -> str:
    """Digest an expanded argv with the run-specific root replaced by its placeholder.

    Two executions of the same configuration in different run directories must hash equal
    or a base/adopted comparison could never show a matched configuration.
    """
    text = json.dumps(list(argv), ensure_ascii=False)
    text = text.replace(str(run_dir), "{run_dir}").replace(str(run_dir.resolve()), "{run_dir}")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    """One path segment from a run id / label; no separators, no dot segments."""
    text = str(value).replace("\\", "/").strip("/")
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ManifestError(f"unsafe path segment from {value!r}")
    safe = _SAFE_LABEL.sub("__", "__".join(parts)).strip("._")
    if not safe:
        raise ManifestError(f"unsafe path segment from {value!r}")
    return safe


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json(path: Path) -> dict:
    try:
        got = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return got if isinstance(got, dict) else {}


def _tail(path: Path, limit: int) -> str:
    return _read_text(path)[-limit:].strip()


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n…(+{len(text) - limit} chars — adapter truncation)"


__all__ = [
    "Command",
    "InstalledOpenCodeProposer",
    "Manifest",
    "ManifestAdapter",
    "ManifestError",
    "expand_argv",
    "load_manifest",
    "run_bounded",
]
