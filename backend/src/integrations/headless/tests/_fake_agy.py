"""Scripted stand-in for ``agy --input-format stream-json`` used by
``test_antigravity_session.py``.

Speaks just enough of the driver protocol, as captured from 1.2.2, to prove
the session composes with real OS pipes and signals:

* prints ``init`` eagerly (before any stdin), with the ``--conversation`` id
  when one was passed, else a fresh one;
* each ``user`` line runs one scripted turn keyed on the prompt text:
  - ``slow …``   — a turn that never finishes on its own (for SIGINT tests);
  - ``deny …``   — a permission-denied tool turn (empty response,
                   ``denied_actions``);
  - anything else — echoes the prompt back as one ``agent_response``;
* SIGINT mid-turn emits ``result{ERROR, "interrupted"}`` and exits 1;
* stdin EOF exits 0;
* ``--fail-startup`` exits 2 with an error on stderr before ``init``;
* argv is echoed to stderr on start so a test can assert the flags.

Leading underscore keeps pytest from collecting it.
"""

from __future__ import annotations

import json
import signal
import sys
import time
import uuid


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    argv = sys.argv[1:]
    sys.stderr.write("argv: " + json.dumps(argv) + "\n")
    sys.stderr.flush()
    if "--fail-startup" in argv:
        sys.stderr.write("error: authentication required\n")
        sys.stderr.flush()
        return 2

    conversation_id = str(uuid.uuid4())
    if "--conversation" in argv:
        conversation_id = argv[argv.index("--conversation") + 1]
    model = argv[argv.index("--model") + 1] if "--model" in argv else None
    init = {"cwd": ".", "tools": ["run_command"], "permission_mode": "request-review"}
    if model:
        init["model"] = model
    emit({"event": "init", "conversation_id": conversation_id, "init": init})

    step = 0
    in_turn = False

    def on_sigint(signum, frame):  # noqa: ARG001
        if in_turn:
            emit(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": conversation_id,
                        "status": "ERROR",
                        "response": "",
                        "error": "interrupted",
                        "num_turns": 1,
                    },
                }
            )
        sys.stderr.write("error: interrupted\n")
        sys.exit(1)

    signal.signal(signal.SIGINT, on_sigint)

    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("event") != "user":
            continue
        content = msg["message"]["content"]
        in_turn = True
        emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": step,
                    "state": "DONE",
                    "step_type": "user_input",
                },
            }
        )
        step += 1
        if content.startswith("slow"):
            while True:
                time.sleep(0.05)
        if content.startswith("deny"):
            emit(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": conversation_id,
                        "step_index": step,
                        "state": "ACTIVE",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {
                            "name": "run_command",
                            "parameters": {"CommandLine": "ls"},
                        },
                    },
                }
            )
            emit(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": conversation_id,
                        "step_index": step,
                        "state": "ERROR",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {
                            "name": "run_command",
                            "parameters": {"CommandLine": "ls"},
                            "error": {
                                "type": "TOOL_ERROR",
                                "message": 'permission check failed for command "ls": user denied permission',
                            },
                        },
                    },
                }
            )
            step += 1
            emit(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": conversation_id,
                        "status": "SUCCESS",
                        "response": "",
                        "num_turns": 1,
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 1,
                            "thinking_tokens": 0,
                            "cache_read_tokens": 0,
                            "total_tokens": 11,
                        },
                        "denied_actions": [
                            {"action": "command", "display_name": "RunCommand"}
                        ],
                    },
                }
            )
            in_turn = False
            continue
        emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": step,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": "echo: ",
                },
            }
        )
        emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conversation_id,
                    "step_index": step,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": content + "\n",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 5,
                        "thinking_tokens": 0,
                        "cache_read_tokens": 20,
                        "total_tokens": 105,
                    },
                },
            }
        )
        step += 1
        emit(
            {
                "event": "result",
                "result": {
                    "conversation_id": conversation_id,
                    "status": "SUCCESS",
                    "response": "echo: " + content + "\n",
                    "num_turns": 1,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 5,
                        "thinking_tokens": 0,
                        "cache_read_tokens": 20,
                        "total_tokens": 105,
                    },
                },
            }
        )
        in_turn = False


if __name__ == "__main__":
    sys.exit(main())
