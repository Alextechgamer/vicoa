"""Policy tests for the ChatGPT MCP adapter. Disposable database only."""

import inspect
import json
from pathlib import Path

import pytest

from shared.control_plane.chatgpt_mcp import (
    FORBIDDEN_TOOLS,
    VicoaMcp,
    build_app,
    build_server,
    controller_id,
)
from shared.control_plane.store import ControlPlane


def _plane(tmp_path: Path) -> ControlPlane:
    plane = ControlPlane(tmp_path / "plane.db")
    plane.create_account(
        account_id="vicoa-agy-1",
        provider="antigravity",
        runtime_home=str(tmp_path / "vicoa-agy-1"),
    )
    plane.create_account(
        account_id="agy-1",
        provider="antigravity",
        runtime_home="/home/agentctl/runtimes/agy-1",
    )
    return plane


def test_claim_injects_controller_and_rejects_spoof_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VICOA_MCP_CONTROLLER_ID", "chatgpt-main")
    plane = _plane(tmp_path)
    job = plane.create_job(project="fixture", goal="claim")
    adapter = VicoaMcp(plane)
    assert "manager_id" not in inspect.signature(adapter.claim_job).parameters
    claimed = adapter.claim_job(job["id"])
    assert claimed["manager_id"] == controller_id()
    with pytest.raises(TypeError):
        adapter.claim_job(job["id"], manager_id="someone-else")  # type: ignore[call-arg]


def test_held_task_message_is_refused(tmp_path: Path) -> None:
    plane = _plane(tmp_path)
    job = plane.create_job(project="fixture", goal="held")
    task = plane.add_task(job["id"], title="held", plan_key="held", protected=True)
    plane.protect(task["id"], "fixture")
    adapter = VicoaMcp(plane)
    with pytest.raises(Exception, match="refused"):
        adapter.message_worker(task["id"], "do not send", "held-1")
    assert plane.messages(task["id"]) == []


def test_known_live_ids_are_refused_even_without_protected_flag(tmp_path: Path) -> None:
    plane = _plane(tmp_path)
    job = plane.create_job(project="fixture", goal="renumbered")
    task = plane.add_task(job["id"], title="plain", plan_key="plain")
    with plane._conn() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("UPDATE tasks SET id=6 WHERE id=?", (task["id"],))
        db.execute("UPDATE jobs SET id=125 WHERE id=?", (job["id"],))
        db.execute("PRAGMA foreign_keys=ON")
    adapter = VicoaMcp(plane)
    with pytest.raises(Exception, match="refused"):
        adapter.message_worker(6, "no", "live-6")
    with pytest.raises(Exception, match="refused"):
        adapter.claim_job(125)


def test_legacy_profile_cannot_be_enabled(tmp_path: Path) -> None:
    plane = _plane(tmp_path)
    plane.drain("agy-1")
    adapter = VicoaMcp(plane)
    with pytest.raises(Exception, match="legacy"):
        adapter.enable_profile("agy-1")
    assert plane.account("agy-1")["drained"] == 1


def test_new_work_profile_cannot_be_drained(tmp_path: Path) -> None:
    plane = _plane(tmp_path)
    adapter = VicoaMcp(plane)
    with pytest.raises(Exception, match="not drained"):
        adapter.drain_profile("vicoa-agy-1")
    assert plane.account("vicoa-agy-1")["drained"] == 0


def test_patch_rejects_worker_state(tmp_path: Path) -> None:
    plane = _plane(tmp_path)
    job = plane.create_job(project="fixture", goal="patch")
    task = plane.add_task(job["id"], title="patch", plan_key="patch")
    adapter = VicoaMcp(plane)
    with pytest.raises(Exception, match="not a task preference"):
        adapter.patch_task_prefs(task["id"], {"worker_status": "running"})
    assert plane.task(task["id"])["worker_status"] == "queued"


def test_server_surface_excludes_forbidden_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VICOA_MCP_TOKEN", "x" * 32)
    server = build_server(VicoaMcp(_plane(tmp_path)))
    listed = server._tool_manager.list_tools()
    names = {tool.name for tool in listed}
    assert "vicoa_claim_job" in names
    assert names.isdisjoint(FORBIDDEN_TOOLS)
    blob = json.dumps(sorted(names))
    assert "shell" not in blob


def test_bearer_guard_rejects_missing_and_forwarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    token = "y" * 32
    monkeypatch.setenv("VICOA_MCP_TOKEN", token)
    plane = _plane(tmp_path)
    guard = build_app(plane)
    seen: list[int] = []

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            seen.append(message["status"])

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "client": ("127.0.0.1", 9),
        "headers": [],
        "method": "POST",
        "path": "/mcp",
    }
    asyncio.run(guard(scope, receive, send))
    assert seen == [401]
    seen.clear()
    scope["headers"] = [(b"x-forwarded-for", b"1.2.3.4"), (b"authorization", f"Bearer {token}".encode())]
    asyncio.run(guard(scope, receive, send))
    assert seen == [403]


def test_well_known_metadata_is_plain_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("VICOA_MCP_TOKEN", "w" * 32)
    guard = build_app(_plane(tmp_path))
    seen: list[int] = []

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            seen.append(message["status"])

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "client": ("127.0.0.1", 9),
        "headers": [],
        "method": "GET",
        "path": "/.well-known/oauth-protected-resource/mcp",
    }
    asyncio.run(guard(scope, receive, send))
    assert seen == [404]


def test_inprocess_mcp_client_creates_a_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from mcp import Client

    monkeypatch.setenv("VICOA_MCP_TOKEN", "x" * 32)
    plane = _plane(tmp_path)
    server = build_server(VicoaMcp(plane))

    async def run() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            tools = listed.tools
            names = {tool.name for tool in tools}
            assert "vicoa_claim_job" in names
            assert names.isdisjoint(FORBIDDEN_TOOLS)
            result = await client.call_tool(
                "vicoa_create_job",
                {"project": "fixture", "goal": "mcp client canary"},
            )
            payload = result.structured_content or result.content
            rendered = json.dumps(payload, default=str)
            assert "mcp client canary" in rendered

    asyncio.run(run())
    assert plane.jobs()


def test_response_redacts_configured_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = "z" * 32
    monkeypatch.setenv("VICOA_MCP_TOKEN", token)
    plane = _plane(tmp_path)
    job = plane.create_job(project="fixture", goal=f"leak {token}")
    rendered = json.dumps(VicoaMcp(plane).job(job["id"]))
    assert token not in rendered
    assert "[redacted]" in rendered
