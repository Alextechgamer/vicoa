"""What Vicoa knows about the Antigravity CLI (``agy``) as a launchable binary.

Everything here was verified live against ``agy`` 1.2.2 on 2026-09-16; the
facts that shape the command line are recorded next to the code that depends
on them because none of them are obvious from ``agy --help``:

* ``--input-format stream-json`` is the driver mode (added in 1.1.15: one
  NDJSON prompt per line on stdin, one turn each, one conversation). It must
  NOT be combined with ``--print``: on 1.2.x ``--print`` takes the next argv
  token as its prompt, so ``--print --input-format …`` fails at startup.
* ``--add-dir <cwd>`` is load-bearing. Without it every tool runs in
  ``~/.gemini/antigravity-cli/scratch/`` — ``init.cwd`` still reports the
  real cwd, so the only visible symptom is a coding agent that edits the
  wrong directory.
* ``--disable-slash-commands`` is load-bearing too. A CLI-answered command
  such as ``/usage`` on stdin ends the whole session with an ``ERROR``
  result (exit 2); with the flag it is passed to the model as plain text.
* ``--print-timeout`` is per *turn* (a 15s timeout survived a 25s idle gap
  between turns); on expiry the CLI returns partial output and exits 0, which
  in driver mode means the session is gone — hence the large value.
* ``--effort`` is not a free knob: every id ``agy models`` lists already
  carries its effort (``gemini-3.8-flash-high``, ``gpt-oss-120b-medium``),
  a mismatched ``--effort`` is a hard startup error, and Claude ids reject
  the flag outright. So the model id is the only model-side setting Vicoa
  passes; there is no thinking-effort picker for this agent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


CATALOG_ID = "antigravity"
DISPLAY_NAME = "Antigravity"
BINARIES: Tuple[str, ...] = ("agy",)
#: The installer drops the binary in ``~/.local/bin``, which a launchd/systemd
#: daemon's PATH does not include.
EXTRA_DIRS: Tuple[str, ...] = ("~/.local/bin",)
#: First release with ``--input-format stream-json``.
MIN_VERSION = "1.1.15"
INSTALL_HINT = (
    "Install the Antigravity CLI: curl -fsSL "
    "https://antigravity.google/cli/install.sh | bash — then run `agy` once "
    "and sign in with your Google account."
)

#: Generous per-turn ceiling; see the module docstring for why it is per turn.
PRINT_TIMEOUT = "12h"

#: Vicoa permission mode -> agy flags. ``default`` is agy's ``request-review``:
#: workspace reads are auto-allowed, everything else is auto-denied because
#: headless mode has no one to ask. ``plan`` writes a plan artifact first and
#: then behaves like ``default``. Verified per mode on 1.2.2.
PERMISSION_FLAGS: Dict[str, Tuple[str, ...]] = {
    "default": (),
    "acceptEdits": ("--mode", "accept-edits"),
    "plan": ("--mode", "plan"),
    "bypassPermissions": ("--dangerously-skip-permissions",),
}

#: Model ids clients treat as "keep the agent's own default" and never send.
DEFAULT_MODEL_SENTINELS = frozenset({"default", "auto"})


def resolve_agy_binary(
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Locate ``agy``: ``which`` (PATH / npm locations) first, then EXTRA_DIRS.

    Same contract as ``generic_acp.resolve_agent_binary`` so spawn resolution
    and the daemon's install detection never disagree.
    """
    if which is None:
        which = shutil.which
    for candidate in BINARIES:
        found = which(candidate)
        if found:
            return found
    for directory in EXTRA_DIRS:
        base = os.path.expanduser(directory)
        for candidate in BINARIES:
            full = os.path.join(base, candidate)
            if os.path.isfile(full) and os.access(full, os.X_OK):
                return full
    return None


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """First ``major.minor.patch`` triple in ``text`` (``agy --version`` prints a bare ``1.2.2``)."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def version_at_least(actual: Optional[str], minimum: str) -> bool:
    """Whether ``actual`` satisfies ``minimum``. An unparseable ``actual`` passes.

    Failing open on purpose, as ``pi_family.spec`` does: a version string we
    can't read is far more likely an upstream format change than a genuinely
    old install, and the startup path still catches a build without driver
    mode (it rejects ``--input-format`` before emitting ``init``).
    """
    want = parse_version(minimum)
    have = parse_version(actual)
    if want is None or have is None:
        return True
    return have >= want


def _run_capture(command: List[str], *, timeout: float = 10.0) -> Optional[str]:
    """Run ``command`` and return its combined output, or ``None`` if it fails to run."""
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{proc.stdout}\n{proc.stderr}".strip() or None


def check_runtime_requirements(
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
    run_version: Optional[Callable[[List[str]], Optional[str]]] = None,
) -> Optional[str]:
    """Return an error string when Antigravity can't be driven on this machine.

    Two failure modes, both worded for the chat/install surfaces: the binary
    is absent, or it predates driver mode (1.1.15). Returns ``None`` when the
    agent is launchable. ``run_version`` resolves at call time (not bound as
    a default) so tests can monkeypatch ``spec._run_capture``.
    """
    if run_version is None:
        run_version = _run_capture
    binary = resolve_agy_binary(which=which)
    if binary is None:
        return (
            f"{DISPLAY_NAME} CLI ('agy') is not installed or not on PATH. "
            f"{INSTALL_HINT}"
        )
    version = run_version([binary, "--version"])
    if not version_at_least(version, MIN_VERSION):
        found = (version or "unknown").strip().splitlines()[0]
        return (
            f"{DISPLAY_NAME} CLI {found} is too old: Vicoa needs agy >= "
            f"{MIN_VERSION} (stream-json driver mode). Run `agy update` and retry."
        )
    return None


def build_command(
    *,
    binary: str,
    cwd: str,
    model: Optional[str] = None,
    permission_mode: Optional[str] = None,
    conversation_id: Optional[str] = None,
    extra_dirs: Tuple[str, ...] = (),
    print_timeout: str = PRINT_TIMEOUT,
) -> List[str]:
    """The full argv for one driver-mode ``agy`` process.

    ``extra_dirs`` are further ``--add-dir`` entries (the attachments folder,
    so a pasted image is readable under ``request-review``). A permission mode
    outside PERMISSION_FLAGS is treated as ``default`` rather than rejected —
    the daemon already validated it against the catalog enum.
    """
    command = [
        binary,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--disable-slash-commands",
        "--print-timeout",
        print_timeout,
        "--add-dir",
        cwd,
    ]
    for directory in extra_dirs:
        command.extend(["--add-dir", directory])
    normalized_model = (model or "").strip()
    if normalized_model and normalized_model not in DEFAULT_MODEL_SENTINELS:
        command.extend(["--model", normalized_model])
    command.extend(PERMISSION_FLAGS.get(permission_mode or "default", ()))
    if conversation_id:
        command.extend(["--conversation", conversation_id])
    return command


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

#: ``agy models`` is a network call, so its result is cached on disk and
#: reused across sessions on the same machine for this long.
MODELS_CACHE_TTL_SECONDS = 6 * 60 * 60


def parse_models_tsv(text: Optional[str]) -> List[Dict[str, str]]:
    """``agy models`` prints one ``<id>\\t<label>`` per line; parse that.

    The same TSV is what ``agy --output-format json models`` wraps in its
    ``response`` field, so this is the one stable contract across versions.
    Lines without a tab (spinner residue, warnings) are skipped.
    """
    models: List[Dict[str, str]] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip("\r\n")
        if "\t" not in line:
            continue
        model_id, _, label = line.partition("\t")
        model_id = model_id.strip()
        label = label.strip() or model_id
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append({"id": model_id, "label": label})
    return models


def parse_current_model(text: Optional[str]) -> Optional[Dict[str, str]]:
    """The agent's configured default model, from ``agy -p /model --output-format json``.

    The envelope's ``command.data`` carries ``{id, label, effort, is_default}``;
    ``response`` repeats it as TSV. Either is enough — ``data`` is preferred
    and the TSV is the fallback for a build that only emits the text form.
    """
    if not text:
        return None
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        data = (payload.get("command") or {}).get("data")
        if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"]:
            return {"id": data["id"], "label": str(data.get("label") or data["id"])}
        stripped = str(payload.get("response") or "")
    parsed = parse_models_tsv(stripped)
    return parsed[0] if parsed else None


def models_cache_path() -> Path:
    return Path.home() / ".vicoa" / "antigravity" / "models.json"


def load_cached_models(
    path: Optional[Path] = None,
    *,
    ttl_seconds: float = MODELS_CACHE_TTL_SECONDS,
    now: Optional[float] = None,
) -> Optional[List[Dict[str, str]]]:
    """The cached ``agy models`` list, or ``None`` when absent, stale or unreadable."""
    path = path or models_cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    fetched_at = raw.get("fetched_at")
    models = raw.get("models")
    if not isinstance(fetched_at, (int, float)) or not isinstance(models, list):
        return None
    current = time.time() if now is None else now
    if current - float(fetched_at) > ttl_seconds:
        return None
    cleaned = [
        {"id": str(m["id"]), "label": str(m.get("label") or m["id"])}
        for m in models
        if isinstance(m, dict) and m.get("id")
    ]
    return cleaned or None


def store_cached_models(
    models: List[Dict[str, str]],
    path: Optional[Path] = None,
    *,
    now: Optional[float] = None,
) -> None:
    """Best-effort write of the cache; a failure only costs the next session a network call."""
    path = path or models_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": time.time() if now is None else now,
                    "models": models,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def fetch_models(
    binary: str,
    *,
    run: Optional[Callable[[List[str]], Optional[str]]] = None,
    cache_path: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """The live model list, served from the on-disk cache when it is fresh."""
    if run is None:
        run = _run_capture
    cached = load_cached_models(cache_path)
    if cached:
        return cached
    models = parse_models_tsv(run([binary, "models"]))
    if models:
        store_cached_models(models, cache_path)
    return models


def fetch_current_model(
    binary: str,
    *,
    run: Optional[Callable[[List[str]], Optional[str]]] = None,
) -> Optional[Dict[str, str]]:
    """The configured default model. ``-p /model`` answers without spending quota."""
    if run is None:
        run = _run_capture
    return parse_current_model(run([binary, "-p", "/model", "--output-format", "json"]))


__all__ = [
    "BINARIES",
    "CATALOG_ID",
    "DEFAULT_MODEL_SENTINELS",
    "DISPLAY_NAME",
    "EXTRA_DIRS",
    "INSTALL_HINT",
    "MIN_VERSION",
    "MODELS_CACHE_TTL_SECONDS",
    "PERMISSION_FLAGS",
    "PRINT_TIMEOUT",
    "build_command",
    "check_runtime_requirements",
    "fetch_current_model",
    "fetch_models",
    "load_cached_models",
    "models_cache_path",
    "parse_current_model",
    "parse_models_tsv",
    "parse_version",
    "resolve_agy_binary",
    "store_cached_models",
    "version_at_least",
]
