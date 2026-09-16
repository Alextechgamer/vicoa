"""Headless Antigravity (``agy``) integration — the stream-json driver wrapper.

Package layout mirrors ``pi_family/``:

* ``spec``    — binary lookup, version gate, argv builder, model-list parsing.
* ``events``  — pure NDJSON-frame -> chat-row mapper (fixture-tested).
* ``session`` — the ``agy`` process and one conversation's turn lifecycle.
* ``runner``  — registration, WebSocket, queue, control commands, ``main``.

The daemon spawns ``python -m integrations.headless.antigravity`` from source
and ``vicoa headless --agent antigravity`` from the frozen bundle; both land in
:func:`runner.main`. Like ``pi_family``, this ``__init__`` re-exports only the
spec so the daemon can import it without loading the runner.

Background and the measured protocol notes live in
``plans/todos/agent-integration-followups.md`` §3; the wire captures the
mapper was written against are under ``tests/fixtures/antigravity/``.
"""

from integrations.headless.antigravity.spec import (
    CATALOG_ID,
    DISPLAY_NAME,
    check_runtime_requirements,
    resolve_agy_binary,
)

__all__ = [
    "CATALOG_ID",
    "DISPLAY_NAME",
    "check_runtime_requirements",
    "resolve_agy_binary",
]
