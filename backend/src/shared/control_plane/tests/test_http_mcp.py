"""HTTP canary for the real control-plane router. Disposable database only."""

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import control_plane
from shared.control_plane.store import ControlPlane


def test_http_mcp_canary_rejects_shell_and_keeps_protected_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "plane.db"
    monkeypatch.setenv("VICOA_CONTROL_PLANE_TOKEN", "canary-token")
    monkeypatch.setenv("VICOA_CONTROL_PLANE_DB", str(db))
    control_plane._open.cache_clear()
    plane = ControlPlane(db)
    plane.create_account(account_id="vicoa-agy-1", provider="antigravity", runtime_home=str(tmp_path / "a"))
    plane.create_account(account_id="vicoa-agy-2", provider="antigravity", runtime_home=str(tmp_path / "b"))
    held = plane.create_job(project="/tmp/held", goal="held", import_hold=True)
    task = plane.add_task(held["id"], title="held", plan_key="held", protected=True, import_hold=True)
    plane.protect(task["id"], "fixture")

    app = FastAPI()
    app.include_router(control_plane.router, prefix="/api/v1")
    client = TestClient(app)
    headers = {"Authorization": "Bearer canary-token"}

    status = client.get("/api/v1/control-plane/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["accounts"]
    assert str(tmp_path) not in status.text

    created = client.post(
        "/api/v1/control-plane/mcp",
        headers=headers,
        json={"tool": "create_goal", "arguments": {"project": "/tmp/canary", "goal": "disposable"}},
    )
    assert created.status_code == 200
    claimed = client.post(
        "/api/v1/control-plane/mcp",
        headers=headers,
        json={"tool": "claim_job", "arguments": {"job_id": created.json()["id"], "manager_id": "hermes-main"}},
    )
    assert claimed.status_code == 200
    assert claimed.json()["started"] == []
    shell = client.post("/api/v1/control-plane/mcp", headers=headers, json={"tool": "shell", "arguments": {}})
    assert shell.status_code == 403
    protected = client.post(
        "/api/v1/control-plane/mcp",
        headers=headers,
        json={"tool": "message_worker", "arguments": {"task_id": task["id"], "text": "do not send this to live work"}},
    )
    assert protected.status_code == 409
    control_plane._open.cache_clear()
    os.environ.pop("VICOA_CONTROL_PLANE_TOKEN", None)
