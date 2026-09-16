"""Lifecycle tests for ``AntigravitySession`` against ``_fake_agy.py``.

Real OS pipes, real signals, a scripted child: these cover the paths the
fixture-driven mapper tests can't — the ``init`` gate at bring-up, a full
turn's status/usage pushes, SIGINT-then-respawn on the same conversation, the
lazy restart behind a model switch, and a startup failure surfacing as a chat
message rather than a hung session.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from integrations.headless.antigravity import spec
from integrations.headless.antigravity.session import (
    AntigravitySession,
    AntigravityStartupError,
)


_FAKE_AGY = Path(__file__).parent / "_fake_agy.py"


class RecordingClient:
    """The slice of ``AsyncVicoaClient`` the session touches, recorded."""

    def __init__(self) -> None:
        self.messages: List[str] = []
        self.statuses: List[str] = []
        self.session_config_patches: List[Dict[str, Any]] = []
        self.metadata_patches: List[Dict[str, Any]] = []

    async def send_message(self, *, content: str, **_: Any) -> None:
        self.messages.append(content)

    async def update_agent_instance_status(
        self, _instance_id: str, status: str
    ) -> None:
        self.statuses.append(status)

    async def patch_agent_instance(
        self,
        _instance_id: str,
        *,
        session_config: Optional[Dict[str, Any]] = None,
        instance_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if session_config is not None:
            self.session_config_patches.append(session_config)
        if instance_metadata is not None:
            self.metadata_patches.append(instance_metadata)
        return {}

    async def download_attachment(self, _attachment_id: str) -> "tuple[bytes, str]":
        return b"png-bytes", "image/png"

    def metadata(self, key: str) -> List[Any]:
        return [patch[key] for patch in self.metadata_patches if key in patch]


@pytest.fixture
def fake_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A shell shim so the session can exec the fake like a real binary."""
    monkeypatch.setenv("HOME", str(tmp_path))  # keeps ~/.vicoa writes in tmp
    shim = tmp_path / "agy"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE_AGY}" "$@"\n')
    shim.chmod(0o755)
    return str(shim)


@pytest.fixture(autouse=True)
def canned_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model discovery shells out twice; keep the tests hermetic and quick."""
    monkeypatch.setattr(
        spec,
        "fetch_models",
        lambda binary, **kw: [{"id": "m-a", "label": "A"}, {"id": "m-b", "label": "B"}],
    )
    monkeypatch.setattr(
        spec, "fetch_current_model", lambda binary, **kw: {"id": "m-a", "label": "A"}
    )


def _session(
    client: RecordingClient, binary: str, tmp_path: Path, **kwargs: Any
) -> AntigravitySession:
    return AntigravitySession(
        vicoa_client=client,
        instance_id="inst-1",
        cwd=str(tmp_path),
        binary=binary,
        agent_type="Antigravity",
        **kwargs,
    )


def _spawn_argv(session: AntigravitySession) -> List[str]:
    """The argv the fake echoed on its first stderr line."""
    tail = session.stderr_tail()
    for line in tail.splitlines():
        if line.startswith("argv: "):
            return json.loads(line[len("argv: ") :])
    raise AssertionError(f"no argv line in stderr tail: {tail!r}")


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim + SIGINT")
async def test_bring_up_waits_for_init_and_publishes_models(fake_binary, tmp_path):
    client = RecordingClient()
    session = _session(
        client, fake_binary, tmp_path, model="m-b", permission_mode="acceptEdits"
    )
    try:
        await session.start()
        assert session.conversation_id  # learned from init
        assert client.metadata("antigravity_conversation_id") == [
            session.conversation_id
        ]
        assert client.statuses == ["AWAITING_INPUT"]
        argv = _spawn_argv(session)
        assert argv[argv.index("--model") + 1] == "m-b"
        assert argv[argv.index("--mode") + 1] == "accept-edits"
        assert argv[argv.index("--add-dir") + 1] == str(tmp_path)
        assert "--disable-slash-commands" in argv
        await _wait_for(lambda: bool(client.session_config_patches))
        assert client.session_config_patches[-1] == {
            "available_models": [
                {"id": "m-a", "label": "A"},
                {"id": "m-b", "label": "B"},
            ],
            "current_model": "m-b",
        }
    finally:
        await session.aclose()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim + SIGINT")
async def test_turn_posts_text_status_and_usage(fake_binary, tmp_path):
    client = RecordingClient()
    session = _session(client, fake_binary, tmp_path, system_prompt="be terse")
    try:
        await session.start()
        await session.deliver_user_message("hello there")
        assert client.messages == ["echo: be terse\n\n---\n\nhello there"]
        assert client.statuses == ["AWAITING_INPUT", "ACTIVE", "AWAITING_INPUT"]
        usage = client.metadata("usage")
        assert usage and usage[-1]["context"] == {
            "used_tokens": 125,
            "max_tokens": None,
            "cost_usd": None,
        }
        assert session.process_alive  # one process serves many turns
        await session.deliver_user_message("again")
        assert client.messages[-1] == "echo: be terse\n\n---\n\nagain"
    finally:
        await session.aclose()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim + SIGINT")
async def test_denied_turn_posts_one_notice(fake_binary, tmp_path):
    client = RecordingClient()
    session = _session(client, fake_binary, tmp_path)
    try:
        await session.start()
        await session.deliver_user_message("deny please")
        assert client.messages[0] == "🔧 Using tool: Bash - `ls`"
        assert client.messages[1] == "⛔ Permission denied — Bash - `ls`"
        assert client.messages[2].startswith("⛔ Antigravity denied **RunCommand**")
        assert len(client.messages) == 3
        assert client.statuses[-1] == "AWAITING_INPUT"
    finally:
        await session.aclose()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim + SIGINT")
async def test_interrupt_kills_process_and_next_turn_resumes_conversation(
    fake_binary, tmp_path
):
    client = RecordingClient()
    session = _session(client, fake_binary, tmp_path)
    try:
        await session.start()
        first_conversation = session.conversation_id
        turn = asyncio.create_task(session.deliver_user_message("slow burn"))
        await _wait_for(lambda: session.turn_active and "ACTIVE" in client.statuses)
        await asyncio.sleep(0.2)  # let the fake enter its turn loop
        # Not awaited: the runner's consumer is unblocked by the turn future,
        # not by interrupt() returning, so the turn itself must outlast the
        # dying process (its final `result` lands before it exits).
        asyncio.create_task(session.interrupt())
        await asyncio.wait_for(turn, timeout=5.0)
        assert not session.process_alive
        # No "exited during the turn" complaint for a stop we asked for.
        assert not any("exited during the turn" in m for m in client.messages)
        assert client.statuses[-1] == "AWAITING_INPUT"

        await session.deliver_user_message("back")
        assert client.messages[-1] == "echo: back"
        assert session.conversation_id == first_conversation
        argv = _spawn_argv(session)
        assert argv[argv.index("--conversation") + 1] == first_conversation
    finally:
        await session.aclose()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim + SIGINT")
async def test_model_switch_restarts_before_next_turn(fake_binary, tmp_path):
    client = RecordingClient()
    session = _session(client, fake_binary, tmp_path, model="m-a")
    try:
        await session.start()
        await _wait_for(lambda: bool(session.available_models))
        conversation = session.conversation_id
        assert await session.set_model("m-b") is True
        assert await session.set_model("not-a-model") is False
        assert client.session_config_patches[-1] == {
            "agent": "antigravity",
            "model": "m-b",
            "current_model": "m-b",
        }
        assert session.process_alive  # restart is lazy
        await session.deliver_user_message("after switch")
        argv = _spawn_argv(session)
        assert argv[argv.index("--model") + 1] == "m-b"
        assert argv[argv.index("--conversation") + 1] == conversation
        assert client.messages[-1] == "echo: after switch"
        # Permission mode switches ride the same mechanism.
        assert await session.set_permission_mode("bypassPermissions") is True
        assert await session.set_permission_mode("yolo") is False
        await session.deliver_user_message("skip")
        assert "--dangerously-skip-permissions" in _spawn_argv(session)
    finally:
        await session.aclose()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim + SIGINT")
async def test_startup_failure_is_a_chat_ready_error(
    fake_binary, tmp_path, monkeypatch
):
    client = RecordingClient()
    session = _session(client, fake_binary, tmp_path)
    # Route the failure flag through the argv the session builds.
    monkeypatch.setattr(
        session, "build_command", lambda: [fake_binary, "--fail-startup"]
    )
    with pytest.raises(AntigravityStartupError) as excinfo:
        await session.start()
    assert "not signed in" in str(excinfo.value)
    assert not session.process_alive
    await session.aclose()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell shim + SIGINT")
async def test_attachments_are_saved_and_referenced_by_path(fake_binary, tmp_path):
    from vicoa.attachments import AttachmentRef

    client = RecordingClient()
    session = _session(client, fake_binary, tmp_path)
    try:
        await session.start()
        ref = AttachmentRef(id="att-1", mime_type="image/png", filename="shot.png")
        await session.deliver_user_message("look", (ref,))
        posted = client.messages[-1]
        assert posted.startswith("echo: look\n[Attached image: ")
        saved = Path(posted.split("[Attached image: ", 1)[1].rstrip("]"))
        assert saved.read_bytes() == b"png-bytes"
        # The attachments folder is on --add-dir so view_file can open it.
        argv = _spawn_argv(session)
        add_dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        assert str(saved.parent) in add_dirs
    finally:
        await session.aclose()
