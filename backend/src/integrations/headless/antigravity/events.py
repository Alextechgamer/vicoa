"""Turn ``agy --output-format stream-json`` events into Vicoa ``messages`` rows.

Pure translation, in the mould of ``pi_family.event_mapper``: the mapper owns
no I/O and no lifecycle, takes one decoded NDJSON frame and returns the rows
it implies, so it can be driven straight from the archived wire captures in
``tests/fixtures/antigravity/*.ndjson``.

Structural facts from the captures (agy 1.2.2, 2026-09-16) this file relies on:

* The stream is ``init`` once, then per turn: ``step_update`` frames and one
  ``result``. Step indices are per conversation, not per turn — a resumed
  conversation continues counting where it left off.
* ``agent_response`` streams ``text_delta`` fragments in ``ACTIVE`` frames and
  finishes with a ``DONE`` frame that may carry a final fragment (often just
  ``"\\n"``) plus the step's ``usage``. A response that is only thinking has a
  ``DONE`` with no ``text_delta`` at all. Rows are emitted at ``DONE`` from
  the accumulated fragments; there is no streaming-row API to feed deltas to.
* A tool step is ``ACTIVE`` (name + ``tool_info.parameters``) then ``DONE``
  (``tool_info.output`` for commands; writes have no output) or ``ERROR``
  (``tool_info.error.{type,message}``). ``parameters`` is a *summary* — a
  write carries ``TargetFile`` but never the content.
* A permission denial is a tool ``ERROR`` whose message starts with
  ``permission check failed``; the turn ends right there (no closing text,
  ``result.response == ""``) and ``result.denied_actions`` names what was
  refused. Headless mode cannot prompt, so this is the whole permission
  story — the notice tells the user how to unblock it.
* ``result.denied_actions`` is **cumulative for the process and deduplicated
  by action** (probed 2026-09-16: deny, plain, deny -> the same one-entry
  list on all three results; a respawn starts empty). So the notice is gated
  on the denial *frames* seen this turn, and ``denied_actions`` only supplies
  the display names — otherwise every later turn, including an interrupted
  one, would repeat the first denial's notice.
* ``result.usage`` / ``num_turns`` are cumulative for the session; per-step
  ``usage`` is what says how full the context is right now.
* ``system_message`` and ``checkpoint`` steps carry no user-facing text.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from integrations.headless.format_tools import format_tool_use


logger = logging.getLogger(__name__)


#: Cap on the tool-output tail folded into a chat row — the same 200-char
#: budget the Claude and Pi paths use for their ``Result:`` line.
TOOL_RESULT_MAX_CHARS = 200

_PERMISSION_DENIAL_MARKERS = ("permission check failed", "denied permission")


@dataclass
class Emission:
    """One Vicoa ``messages`` row the caller should POST."""

    content: str
    metadata: Optional[dict] = None


@dataclass
class TurnResult:
    """The ``result`` frame that closes a turn, reduced to what the session acts on."""

    status: str
    response: str
    error: Optional[str]
    denied_actions: List[Dict[str, str]]
    usage: Optional[Dict[str, Any]]
    num_turns: Optional[int]

    @property
    def interrupted(self) -> bool:
        """A SIGINT lands as ``status: ERROR, error: "interrupted"`` on 1.2.x."""
        return (
            self.status == "INTERRUPTED"
            or (self.error or "").strip().lower() == "interrupted"
        )

    @property
    def failed(self) -> bool:
        return self.status not in {"SUCCESS", ""} and not self.interrupted


def context_used_tokens(usage: Any) -> Optional[int]:
    """Point-in-time context fill from one step's ``usage``.

    ``input_tokens`` counts the *uncached* prompt; the cached part is reported
    separately as ``cache_read_tokens``, and ``total_tokens`` is just
    input + output. The context the next request replays is all three: the
    prompt (cached or not) plus what the model just produced. Antigravity
    never reports a window size, so the caller records this with no maximum.
    """
    if not isinstance(usage, dict):
        return None
    parts = [
        _coerce_int(usage.get("input_tokens")),
        _coerce_int(usage.get("cache_read_tokens")),
        _coerce_int(usage.get("output_tokens")),
    ]
    if all(part is None for part in parts):
        return None
    return sum(part or 0 for part in parts)


@dataclass
class EventMapper:
    """Stateful translator for one conversation's event stream."""

    agent_type: str = "Antigravity"

    #: From ``init``. The conversation id is what a later launch resumes with.
    conversation_id: Optional[str] = None
    permission_mode: Optional[str] = None
    model: Optional[str] = None
    #: Context fill after the most recent ``agent_response``; ``None`` until
    #: the first one lands.
    context_used_tokens: Optional[int] = None

    _text_partials: Dict[int, List[str]] = field(default_factory=dict)
    _tool_headers: Dict[int, str] = field(default_factory=dict)
    _emitted_this_turn: bool = False
    _pending_result: Optional[TurnResult] = None
    #: Lower-cased messages of the permission denials seen this turn.
    _denials_this_turn: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def handle(self, frame: Dict[str, Any]) -> List[Emission]:
        """Rows implied by ``frame``. Never raises on an unexpected shape."""
        kind = frame.get("event")
        if kind == "init":
            return self._on_init(_as_dict(frame.get("init")), frame)
        if kind == "step_update":
            return self._on_step(_as_dict(frame.get("step_update")))
        if kind == "result":
            return self._on_result(_as_dict(frame.get("result")))
        logger.debug("antigravity: unhandled event %r", kind)
        return []

    def take_result(self) -> Optional[TurnResult]:
        """The result that closed the current turn, consumed once."""
        result = self._pending_result
        self._pending_result = None
        return result

    def reset_turn(self) -> None:
        """Forget partial state — after an interrupt or an unexpected exit."""
        self._text_partials.clear()
        self._tool_headers.clear()
        self._emitted_this_turn = False
        self._pending_result = None
        self._denials_this_turn.clear()

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------

    def _on_init(self, init: Dict[str, Any], frame: Dict[str, Any]) -> List[Emission]:
        conversation_id = _as_str(frame.get("conversation_id"))
        if conversation_id:
            self.conversation_id = conversation_id
        self.permission_mode = _as_str(init.get("permission_mode")) or None
        self.model = _as_str(init.get("model")) or None
        return []

    def _on_step(self, step: Dict[str, Any]) -> List[Emission]:
        step_type = _as_str(step.get("step_type"))
        state = _as_str(step.get("state"))
        index = _coerce_int(step.get("step_index"))
        if index is None:
            index = -1

        if step_type == "user_input":
            # Start of a turn. Anything left over belongs to a turn that never
            # closed (an interrupted one) and must not leak into this one.
            self._text_partials.clear()
            self._emitted_this_turn = False
            self._denials_this_turn.clear()
            return []
        if step_type == "agent_response":
            return self._on_agent_response(index, state, step)
        if "subagent_info" in step:
            # A step that spawned sub-agents carries ``subagent_info`` *instead
            # of* ``tool_info``, whatever its step_type says.
            return self._on_subagent(index, state, _as_dict(step.get("subagent_info")))
        if step_type == "tool":
            return self._on_tool(index, state, step)
        if step_type in {"system_message", "checkpoint"}:
            return []
        logger.debug("antigravity: unhandled step_type %r", step_type)
        return []

    def _on_agent_response(
        self, index: int, state: str, step: Dict[str, Any]
    ) -> List[Emission]:
        delta = step.get("text_delta")
        if isinstance(delta, str) and delta:
            self._text_partials.setdefault(index, []).append(delta)
        if state != "DONE":
            return []
        used = context_used_tokens(step.get("usage"))
        if used is not None:
            self.context_used_tokens = used
        text = "".join(self._text_partials.pop(index, [])).strip()
        if not text:
            return []
        return self._emit(text)

    def _on_tool(self, index: int, state: str, step: Dict[str, Any]) -> List[Emission]:
        info = _as_dict(step.get("tool_info"))
        tool_name = (
            _as_str(step.get("tool_name")) or _as_str(info.get("name")) or "tool"
        )
        parameters = _as_dict(info.get("parameters"))

        if state == "ACTIVE":
            if index in self._tool_headers:
                # A tool can report progress as repeated ACTIVE frames; the
                # header was already written for the first one.
                return []
            header = render_tool_header(tool_name, parameters)
            self._tool_headers[index] = header
            return self._emit(header)

        header = self._tool_headers.pop(index, None)
        rows: List[Emission] = []
        if header is None:
            # DONE/ERROR with no ACTIVE first (a fast tool on a build that
            # collapses the two): write the header now so the result has a
            # card to hang off.
            header = render_tool_header(tool_name, parameters)
            rows.extend(self._emit(header))

        if state == "ERROR":
            error = _as_dict(info.get("error"))
            message = _as_str(error.get("message")) or "the tool reported an error"
            if is_permission_denial(message):
                self._denials_this_turn.append(message.lower())
                rows.extend(
                    self._emit(f"⛔ Permission denied — {_strip_tool_prefix(header)}")
                )
            else:
                rows.extend(self._emit(f"⚠️ Tool failed: {_truncate(message, 400)}"))
            return rows

        output = info.get("output")
        text = output.strip() if isinstance(output, str) else ""
        if text:
            rows.extend(self._emit(f"   Result: {_truncate(text)}"))
        return rows

    def _on_subagent(
        self, index: int, state: str, info: Dict[str, Any]
    ) -> List[Emission]:
        """A step that spawned sub-agents: one header naming them.

        Only the ``subagents[]`` list is documented (``type_name``, ``role``,
        ``conversation_id``); their output is not in this stream, so there is
        nothing more to render than the fact that they ran.
        """
        if state != "ACTIVE" or index in self._tool_headers:
            return []
        names = []
        for entry in _as_list(info.get("subagents")):
            entry_dict = _as_dict(entry)
            label = _as_str(entry_dict.get("role")) or _as_str(
                entry_dict.get("type_name")
            )
            if label:
                names.append(label)
        summary = ", ".join(names) if names else "sub-agent"
        header = f"🔧 Using tool: Agent - `{summary}`"
        self._tool_headers[index] = header
        return self._emit(header)

    def _on_result(self, result: Dict[str, Any]) -> List[Emission]:
        turn = TurnResult(
            status=_as_str(result.get("status")),
            response=_as_str(result.get("response")),
            error=_as_str(result.get("error")) or None,
            denied_actions=[
                {k: _as_str(v) for k, v in _as_dict(entry).items()}
                for entry in _as_list(result.get("denied_actions"))
            ],
            usage=result.get("usage")
            if isinstance(result.get("usage"), dict)
            else None,
            num_turns=_coerce_int(result.get("num_turns")),
        )
        conversation_id = _as_str(result.get("conversation_id"))
        if conversation_id:
            self.conversation_id = conversation_id
        self._pending_result = turn

        rows: List[Emission] = []
        # A closing text that never streamed (a build that only fills
        # ``response``) — but never a duplicate of what did.
        if not self._emitted_this_turn and turn.response.strip():
            rows.extend(self._emit(turn.response.strip()))
        if turn.failed and turn.error:
            rows.extend(
                self._emit(
                    f"⚠️ **{self.agent_type} turn failed**\n\n{_truncate(turn.error, 1200)}"
                )
            )
        if self._denials_this_turn:
            rows.extend(
                self._emit(
                    denied_actions_notice(
                        _denials_for_this_turn(
                            turn.denied_actions, self._denials_this_turn
                        )
                    )
                )
            )
        # Drop tool headers from a turn that ended before their DONE.
        self._tool_headers.clear()
        self._text_partials.clear()
        self._denials_this_turn.clear()
        return rows

    def _emit(self, content: str, metadata: Optional[dict] = None) -> List[Emission]:
        self._emitted_this_turn = True
        return [Emission(content=content, metadata=metadata)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

#: agy tool -> (Vicoa card name, {agy param: card param}). Only where the
#: semantics genuinely match; the card names are the ones the web and mobile
#: clients already have affordances for. Anything else keeps agy's own name.
_TOOL_TRANSLATIONS: Dict[str, "tuple[str, Dict[str, str]]"] = {
    "run_command": ("Bash", {"CommandLine": "command"}),
    "view_file": ("Read", {"AbsolutePath": "file_path"}),
    "write_to_file": ("Write", {"TargetFile": "file_path"}),
    "replace_file_content": ("Edit", {"TargetFile": "file_path"}),
    "multi_replace_file_content": ("Edit", {"TargetFile": "file_path"}),
    "sed_file": ("Edit", {"TargetFile": "file_path"}),
    "list_dir": ("LS", {"DirectoryPath": "path"}),
    "grep_search": ("Grep", {"Query": "pattern", "SearchPath": "path"}),
    "find_by_name": ("Glob", {"Pattern": "pattern", "SearchDirectory": "path"}),
    "read_url_content": ("WebFetch", {"Url": "url"}),
    "search_web": ("WebSearch", {"query": "query"}),
    "notebook_edit": ("NotebookEdit", {"TargetFile": "notebook_path"}),
    "invoke_subagent": ("Agent", {}),
    "browser_subagent": ("Agent", {}),
}

#: Card names ``format_tool_use`` renders as a header-only line for the
#: inputs we can supply. ``Edit`` is deliberately absent: with no old/new
#: strings its branch appends an empty diff block.
_HEADER_ONLY_VIA_FORMAT_TOOL_USE = frozenset(
    {"Bash", "Read", "Write", "Grep", "Glob", "WebFetch", "WebSearch", "NotebookEdit"}
)

_SUMMARY_KEYS = (
    "CommandLine",
    "AbsolutePath",
    "TargetFile",
    "DirectoryPath",
    "SearchPath",
    "SearchDirectory",
    "Query",
    "Pattern",
    "Url",
    "query",
    "url",
    "path",
    "file_path",
)


def render_tool_header(tool_name: str, parameters: Dict[str, Any]) -> str:
    """The ``🔧 Using tool: …`` line the dashboard's tool card parses.

    Translated tools go through ``format_tool_use`` so the string is
    byte-identical to what the Claude path emits for the same card; the rest
    get the same shape with a one-line argument summary.
    """
    translation = _TOOL_TRANSLATIONS.get(tool_name)
    if translation is not None:
        card, mapping = translation
        inputs = {
            card_key: parameters[agy_key]
            for agy_key, card_key in mapping.items()
            if isinstance(parameters.get(agy_key), str) and parameters[agy_key].strip()
        }
        if card in _HEADER_ONLY_VIA_FORMAT_TOOL_USE:
            return format_tool_use(card, inputs)
        summary = _summarize(parameters)
        if card == "Edit":
            path = inputs.get("file_path") or "unknown"
            return f"🔧 Using tool: **Edit** - `{path}`"
        if card == "Agent":
            return (
                f"🔧 Using tool: Agent - {summary}"
                if summary
                else "🔧 Using tool: Agent"
            )
        return (
            f"🔧 Using tool: {card} - {summary}"
            if summary
            else f"🔧 Using tool: {card}"
        )
    summary = _summarize(parameters)
    return (
        f"🔧 Using tool: {tool_name} - {summary}"
        if summary
        else f"🔧 Using tool: {tool_name}"
    )


def _summarize(parameters: Dict[str, Any]) -> str:
    for key in _SUMMARY_KEYS:
        value = parameters.get(key)
        if isinstance(value, str) and value.strip():
            return f"`{value.strip()}`"
    if not parameters:
        return ""
    try:
        rendered = json.dumps(parameters, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(parameters)
    return f"`{_truncate(rendered, 120)}`"


def is_permission_denial(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _PERMISSION_DENIAL_MARKERS)


#: ``denied_actions[].action`` -> how the user unblocks it next time.
_UNBLOCK_HINTS: Dict[str, str] = {
    "command": (
        "add an allow rule under `permissions.allow` in "
        "`~/.gemini/antigravity-cli/settings.json` (for example `command(git)`), "
        "or start a new session with **Skip permissions**"
    ),
    "write_file": (
        "start a new session with **Write approval** (accept-edits) or "
        "**Skip permissions**, or add a `write_file(<path>)` allow rule under "
        "`permissions.allow` in `~/.gemini/antigravity-cli/settings.json`"
    ),
}
_DEFAULT_UNBLOCK_HINT = (
    "add an allow rule under `permissions.allow` in "
    "`~/.gemini/antigravity-cli/settings.json`, or start a new session with "
    "**Skip permissions**"
)


def _denials_for_this_turn(
    denied_actions: List[Dict[str, str]], messages: List[str]
) -> List[Dict[str, str]]:
    """The ``denied_actions`` entries this turn's denial frames refer to.

    The list is cumulative for the process, so an entry only counts when its
    ``action`` is named in one of this turn's error messages (``permission
    check failed for command …`` / ``… for write_file …``). Falls back to the
    whole list when nothing matches — a notice naming one action too many
    beats a turn that stopped with no explanation.
    """
    relevant = [
        entry
        for entry in denied_actions
        if (entry.get("action") or "").strip()
        and any(entry["action"].strip().lower() in message for message in messages)
    ]
    return relevant or denied_actions


def denied_actions_notice(denied_actions: List[Dict[str, str]]) -> str:
    """One system notice per turn that hit an auto-denial.

    Headless agy cannot ask, so Vicoa cannot offer an Approve button the way
    it does for Claude/Codex; the honest thing is to say why the turn stopped
    and what unblocks it.
    """
    labels: List[str] = []
    hints: List[str] = []
    seen_hints: set[str] = set()
    for entry in denied_actions:
        action = (entry.get("action") or "").strip()
        display = (entry.get("display_name") or action or "an action").strip()
        labels.append(
            f"**{display}**"
            + (f" (`{action}`)" if action and action != display else "")
        )
        hint = _UNBLOCK_HINTS.get(action, _DEFAULT_UNBLOCK_HINT)
        if hint not in seen_hints:
            seen_hints.add(hint)
            hints.append(hint)
    what = ", ".join(labels) if labels else "an action"
    how = "; or ".join(hints) if hints else _DEFAULT_UNBLOCK_HINT
    return (
        f"⛔ Antigravity denied {what} and stopped the turn. Headless `agy` "
        f"can't ask for approval, so Vicoa can't approve it for you — {how}."
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _strip_tool_prefix(header: str) -> str:
    prefix = "🔧 Using tool: "
    return header[len(prefix) :] if header.startswith(prefix) else header


def _truncate(text: str, limit: int = TOOL_RESULT_MAX_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


__all__ = [
    "Emission",
    "EventMapper",
    "TOOL_RESULT_MAX_CHARS",
    "TurnResult",
    "context_used_tokens",
    "denied_actions_notice",
    "is_permission_denial",
    "render_tool_header",
]
