"""Event-mapper tests, driven from archived ``agy`` stream-json captures.

The fixtures under ``fixtures/antigravity/`` are verbatim frames from real
``agy --input-format stream-json --output-format stream-json`` runs on 1.2.2
(2026-09-16; only the ``init.tools`` list is abbreviated), so these check the
mapper against the protocol as it actually behaves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from integrations.headless.antigravity.events import (
    Emission,
    EventMapper,
    TurnResult,
    context_used_tokens,
    denied_actions_notice,
    is_permission_denial,
    render_tool_header,
)


FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"


def replay(name: str) -> "tuple[EventMapper, List[Emission], List[TurnResult]]":
    mapper = EventMapper(agent_type="Antigravity")
    emissions: List[Emission] = []
    results: List[TurnResult] = []
    with (FIXTURES / name).open() as handle:
        for line in handle:
            if not line.strip():
                continue
            frame = json.loads(line)
            emissions.extend(mapper.handle(frame))
            if frame.get("event") == "result":
                result = mapper.take_result()
                assert result is not None
                results.append(result)
    return mapper, emissions, results


def test_init_records_conversation_and_permission_mode():
    mapper, _, _ = replay("text_then_denied_command.ndjson")
    assert mapper.conversation_id == "9d05bc51-3d50-47e9-a06c-69ddaf5182ad"
    assert mapper.permission_mode == "request-review"
    assert mapper.model is None  # not overridden with --model in that run


def test_text_turn_assembles_deltas_into_one_row():
    _, emissions, results = replay("text_then_denied_command.ndjson")
    # Turn 1 streamed "pong" (ACTIVE) then "\n" (DONE): exactly one row, stripped.
    assert emissions[0].content == "pong"
    assert emissions[0].metadata is None
    assert results[0].status == "SUCCESS"
    assert results[0].response == "pong\n"
    assert results[0].denied_actions == []


def test_denied_command_turn_renders_header_denial_and_one_notice():
    _, emissions, results = replay("text_then_denied_command.ndjson")
    rows = [e.content for e in emissions[1:]]
    # The thinking-only agent_response (no text_delta) emits nothing.
    assert rows[0] == "🔧 Using tool: Bash - `ls -la`"
    assert rows[1] == "⛔ Permission denied — Bash - `ls -la`"
    assert rows[2].startswith("⛔ Antigravity denied **RunCommand** (`command`)")
    assert "permissions.allow" in rows[2]
    assert "Skip permissions" in rows[2]
    assert len(rows) == 3
    assert results[1].denied_actions == [
        {"action": "command", "display_name": "RunCommand"}
    ]
    # An empty closing response must not become an empty row.
    assert results[1].response == ""


def test_allowed_tools_render_cards_with_results():
    mapper, emissions, results = replay("tools_allowed.ndjson")
    rows = [e.content for e in emissions]
    assert rows == [
        "🔧 Using tool: Bash - `ls -la`",
        "   Result: total 0\ndrwxr-xr-x@  2 nick  staff   64 Sep 14 03:02 .\n"
        "drwxr-xr-x@ 24 nick  staff  768 Sep 16 14:55 ..",
        # write_to_file's DONE carries no output: header only.
        "🔧 Using tool: **Write** - `/Users/nick/.gemini/antigravity-cli/scratch/probe.txt`",
        "done",
    ]
    assert mapper.model == "gemini-3.8-flash-low"
    assert results[0].status == "SUCCESS"
    assert results[0].num_turns == 1
    # Context fill comes from the LAST agent_response step, not the
    # cumulative result usage.
    assert mapper.context_used_tokens == 13423 + 0 + 1


def test_read_ok_write_denied_in_default_mode():
    _, emissions, results = replay("read_ok_write_denied.ndjson")
    rows = [e.content for e in emissions]
    assert rows[0] == "🔧 Using tool: Read - `/Users/nick/agy-probe-vicoa/MARKER.md`"
    assert rows[1] == "   Result: 2 lines, 12 bytes"
    assert (
        rows[2] == "🔧 Using tool: **Write** - `/Users/nick/agy-probe-vicoa/probe.txt`"
    )
    assert (
        rows[3]
        == "⛔ Permission denied — **Write** - `/Users/nick/agy-probe-vicoa/probe.txt`"
    )
    assert rows[4].startswith("⛔ Antigravity denied **WriteToFile** (`write_file`)")
    assert "Write approval" in rows[4]
    assert len(rows) == 5
    assert results[0].denied_actions == [
        {"action": "write_file", "display_name": "WriteToFile"}
    ]


def test_cumulative_denied_actions_do_not_repeat_the_notice():
    """``denied_actions`` is per process and deduped: only a turn with a
    denial frame gets the notice, and only for the actions that turn denied."""
    mapper = EventMapper()

    def deny(step: int, tool: str, action_word: str) -> List[Emission]:
        out = mapper.handle(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": step,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": tool,
                    "tool_info": {"name": tool, "parameters": {}},
                },
            }
        )
        out += mapper.handle(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": step,
                    "state": "ERROR",
                    "step_type": "tool",
                    "tool_name": tool,
                    "tool_info": {
                        "name": tool,
                        "parameters": {},
                        "error": {
                            "type": "TOOL_ERROR",
                            "message": f'permission check failed for {action_word} "x": user denied permission',
                        },
                    },
                },
            }
        )
        return out

    def result(
        denied: List[Dict[str, str]],
        status: str = "SUCCESS",
        error: Optional[str] = None,
    ) -> List[str]:
        payload: Dict[str, object] = {
            "status": status,
            "response": "",
            "num_turns": 1,
            "denied_actions": denied,
        }
        if error:
            payload["error"] = error
        return [
            e.content for e in mapper.handle({"event": "result", "result": payload})
        ]

    command = {"action": "command", "display_name": "RunCommand"}
    write = {"action": "write_file", "display_name": "WriteToFile"}

    # Turn 1: a command is denied -> one notice naming RunCommand.
    mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 0,
                "state": "DONE",
                "step_type": "user_input",
            },
        }
    )
    deny(1, "run_command", "command")
    rows = result([command])
    assert len(rows) == 1 and "**RunCommand**" in rows[0]

    # Turn 2: a plain turn; the cumulative list still carries RunCommand.
    mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 2,
                "state": "DONE",
                "step_type": "user_input",
            },
        }
    )
    assert result([command]) == []

    # An interrupted turn carries it too — still nothing.
    mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 3,
                "state": "DONE",
                "step_type": "user_input",
            },
        }
    )
    assert result([command], status="ERROR", error="interrupted") == []

    # Turn 4: a write is denied; the list now has both, the notice names only the write.
    mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 4,
                "state": "DONE",
                "step_type": "user_input",
            },
        }
    )
    deny(5, "write_to_file", "write_file")
    rows = result([command, write])
    assert len(rows) == 1
    assert "**WriteToFile**" in rows[0] and "**RunCommand**" not in rows[0]

    # Turn 5: the same command again — deduped upstream, but the frame says so.
    mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 6,
                "state": "DONE",
                "step_type": "user_input",
            },
        }
    )
    deny(7, "run_command", "unsandboxed")  # accept-edits wording: no action word
    rows = result([command, write])
    assert (
        len(rows) == 1 and "**RunCommand**" in rows[0]
    )  # falls back to the whole list


def test_result_only_response_is_emitted_when_nothing_streamed():
    """A build that only fills ``response`` still produces the answer row."""
    mapper = EventMapper()
    frames = [
        {
            "event": "init",
            "conversation_id": "c1",
            "init": {"cwd": "/x", "tools": [], "permission_mode": "request-review"},
        },
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "c1",
                "step_index": 0,
                "state": "DONE",
                "step_type": "user_input",
            },
        },
        {
            "event": "result",
            "result": {
                "conversation_id": "c1",
                "status": "SUCCESS",
                "response": "late answer\n",
                "num_turns": 1,
            },
        },
    ]
    emissions: List[Emission] = []
    for frame in frames:
        emissions.extend(mapper.handle(frame))
    assert [e.content for e in emissions] == ["late answer"]


def test_streamed_response_is_not_duplicated_from_result():
    mapper = EventMapper()
    frames = [
        {
            "event": "step_update",
            "step_update": {
                "step_index": 0,
                "state": "DONE",
                "step_type": "user_input",
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": "hi",
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": "\n",
            },
        },
        {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "hi\n", "num_turns": 1},
        },
    ]
    emissions: List[Emission] = []
    for frame in frames:
        emissions.extend(mapper.handle(frame))
    assert [e.content for e in emissions] == ["hi"]


def test_interrupted_result_is_not_a_failure_row():
    """SIGINT lands as ``ERROR/interrupted``; the runner posts its own notice."""
    mapper = EventMapper()
    emissions = mapper.handle(
        {
            "event": "result",
            "result": {
                "status": "ERROR",
                "response": "",
                "error": "interrupted",
                "num_turns": 1,
            },
        }
    )
    assert emissions == []
    result = mapper.take_result()
    assert result is not None
    assert result.interrupted is True
    assert result.failed is False


def test_error_result_renders_failure_row():
    mapper = EventMapper(agent_type="Antigravity")
    emissions = mapper.handle(
        {
            "event": "result",
            "result": {
                "status": "ERROR",
                "response": "",
                "error": "model quota exhausted",
                "num_turns": 1,
            },
        }
    )
    assert [e.content for e in emissions] == [
        "⚠️ **Antigravity turn failed**\n\nmodel quota exhausted"
    ]
    result = mapper.take_result()
    assert result is not None and result.failed


def test_non_permission_tool_error_renders_tool_failed():
    mapper = EventMapper()
    mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 3,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "false"},
                },
            },
        }
    )
    emissions = mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 3,
                "state": "ERROR",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "false"},
                    "error": {"type": "TOOL_ERROR", "message": "exit status 1"},
                },
            },
        }
    )
    assert [e.content for e in emissions] == ["⚠️ Tool failed: exit status 1"]


def test_tool_done_without_active_still_gets_a_header():
    mapper = EventMapper()
    emissions = mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 5,
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "list_dir",
                "tool_info": {
                    "name": "list_dir",
                    "parameters": {"DirectoryPath": "/p"},
                    "output": "a\nb",
                },
            },
        }
    )
    assert [e.content for e in emissions] == [
        "🔧 Using tool: LS - `/p`",
        "   Result: a\nb",
    ]


def test_system_message_and_checkpoint_steps_are_silent():
    mapper = EventMapper()
    for step_type in ("system_message", "checkpoint"):
        assert (
            mapper.handle(
                {
                    "event": "step_update",
                    "step_update": {
                        "step_index": 9,
                        "state": "DONE",
                        "step_type": step_type,
                    },
                }
            )
            == []
        )


def test_subagent_step_renders_agent_header_once():
    mapper = EventMapper()
    step = {
        "step_index": 7,
        "state": "ACTIVE",
        "step_type": "tool",
        "subagent_info": {
            "subagents": [
                {"type_name": "worker", "role": "reviewer", "conversation_id": "s1"}
            ]
        },
    }
    first = mapper.handle({"event": "step_update", "step_update": step})
    again = mapper.handle({"event": "step_update", "step_update": step})
    assert [e.content for e in first] == ["🔧 Using tool: Agent - `reviewer`"]
    assert again == []


def test_unknown_event_and_malformed_frames_do_not_raise():
    mapper = EventMapper()
    assert mapper.handle({"event": "future_thing", "payload": 1}) == []
    assert mapper.handle({"event": "step_update", "step_update": "not-a-dict"}) == []
    assert mapper.handle({"event": "result", "result": None}) == []
    assert mapper.handle({}) == []


def test_reset_turn_drops_partial_text():
    mapper = EventMapper()
    mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": "half",
            },
        }
    )
    mapper.reset_turn()
    emissions = mapper.handle(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 1,
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": "\n",
            },
        }
    )
    assert emissions == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_context_used_tokens_counts_cached_prompt():
    # Docs' second-turn shape: input excludes the cached prefix.
    assert (
        context_used_tokens(
            {
                "input_tokens": 278,
                "output_tokens": 4,
                "cache_read_tokens": 30214,
                "total_tokens": 282,
            }
        )
        == 30496
    )
    assert context_used_tokens({"input_tokens": 13034, "output_tokens": 90}) == 13124
    assert context_used_tokens({}) is None
    assert context_used_tokens(None) is None


def test_render_tool_header_translations():
    assert (
        render_tool_header("run_command", {"CommandLine": "git status"})
        == "🔧 Using tool: Bash - `git status`"
    )
    assert (
        render_tool_header("view_file", {"AbsolutePath": "/a.py"})
        == "🔧 Using tool: Read - `/a.py`"
    )
    assert (
        render_tool_header("replace_file_content", {"TargetFile": "/a.py"})
        == "🔧 Using tool: **Edit** - `/a.py`"
    )
    assert (
        render_tool_header("grep_search", {"Query": "TODO", "SearchPath": "/src"})
        == "🔧 Using tool: Grep - `TODO` in `/src`"
    )
    assert (
        render_tool_header(
            "find_by_name", {"Pattern": "*.py", "SearchDirectory": "/src"}
        )
        == "🔧 Using tool: Glob - `*.py` in `/src`"
    )
    assert (
        render_tool_header("read_url_content", {"Url": "https://x.y"})
        == "🔧 Using tool: WebFetch - `https://x.y`"
    )
    assert (
        render_tool_header("search_web", {"query": "agy"})
        == "🔧 Using tool: WebSearch - `agy`"
    )
    assert (
        render_tool_header("invoke_subagent", {"role": "tester"})
        == '🔧 Using tool: Agent - `{"role": "tester"}`'
    )
    # Unknown tools keep their own name with a one-line summary.
    assert (
        render_tool_header("call_mcp_tool", {"ServerName": "s", "ToolName": "t"})
        == '🔧 Using tool: call_mcp_tool - `{"ServerName": "s", "ToolName": "t"}`'
    )
    assert render_tool_header("finish", {}) == "🔧 Using tool: finish"


def test_is_permission_denial():
    assert is_permission_denial(
        'permission check failed for command "ls": user denied permission'
    )
    assert is_permission_denial("User DENIED permission for write_file(x)")
    assert not is_permission_denial("exit status 1")


def test_denied_actions_notice_merges_duplicate_hints():
    text = denied_actions_notice(
        [
            {"action": "command", "display_name": "RunCommand"},
            {"action": "command", "display_name": "RunCommand"},
            {"action": "mystery", "display_name": "Mystery"},
        ]
    )
    assert (
        text.count("permissions.allow") == 2
    )  # command hint + default hint, each once
    assert "**RunCommand** (`command`)" in text
    assert "**Mystery** (`mystery`)" in text


def test_denied_actions_notice_handles_empty_entries():
    text = denied_actions_notice([{}])
    assert text.startswith("⛔ Antigravity denied **an action**")


def _turn(status: str, error: Optional[str] = None) -> TurnResult:
    return TurnResult(
        status=status,
        response="",
        error=error,
        denied_actions=[],
        usage=None,
        num_turns=None,
    )


def test_turn_result_flags():
    assert _turn("SUCCESS").failed is False
    assert _turn("ERROR", "boom").failed is True
    assert _turn("INTERRUPTED").interrupted is True
    assert _turn("CANCELED").failed is True
