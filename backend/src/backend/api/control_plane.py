"""Control-plane HTTP API.

Fail closed unless ``VICOA_CONTROL_PLANE_TOKEN`` is set. The token is compared
in constant time and is never logged. This router does not expose shell, SQL,
or raw filesystem access.
"""

from __future__ import annotations

import hmac
import os
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from shared.control_plane.mcp_surface import dispatch
from shared.control_plane.store import (
    LIVE_AGENT_CONTROL_DB,
    ControlPlane,
    ControlPlaneError,
)

router = APIRouter(prefix="/control-plane", tags=["control-plane"])


def _token_ok(authorization: str | None) -> None:
    expected = os.environ.get("VICOA_CONTROL_PLANE_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="control plane token is not configured")
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization.split(" ", 1)[1]
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def get_plane() -> ControlPlane:
    path = os.environ.get("VICOA_CONTROL_PLANE_DB", "/var/lib/vicoa/control-plane.db")
    return _open(path)


@lru_cache(maxsize=4)
def _open(path: str) -> ControlPlane:
    return ControlPlane(path)


def _call(tool: str, arguments: dict[str, Any], authorization: str | None) -> dict[str, Any]:
    _token_ok(authorization)
    try:
        return dispatch(get_plane(), tool, arguments)
    except ControlPlaneError as exc:
        status = 409 if exc.code in {"protected", "owner_only", "dependency", "import_hold"} else 400
        if exc.code == "not_found":
            status = 404
        if exc.code == "forbidden":
            status = 403
        raise HTTPException(status_code=status, detail=exc.code) from exc


@router.get("/status")
def status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call("status", {}, authorization)


@router.get("/projects")
def projects(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call("projects", {}, authorization)


@router.get("/jobs")
def jobs(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call("jobs", {}, authorization)


@router.get("/tasks/{task_id}/evidence")
def evidence(task_id: int, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call("evidence", {"task_id": task_id}, authorization)


@router.get("/quota")
def quota(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call("quota_health", {}, authorization)


@router.get("/blockers")
def blockers(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call("blockers", {}, authorization)


@router.post("/goals")
def create_goal(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call("create_goal", body, authorization)


@router.post("/dag")
def submit_dag(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call("submit_dag", body, authorization)


@router.post("/tasks/{task_id}/message")
def message(task_id: int, body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call("message_worker", {"task_id": task_id, "text": body.get("text", "")}, authorization)


@router.post("/mcp")
def mcp(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _call(str(body.get("tool") or ""), dict(body.get("arguments") or {}), authorization)


@router.post("/import")
def import_snapshot(body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _token_ok(authorization)
    source = str(body.get("snapshot") or "")
    if not source or source == str(LIVE_AGENT_CONTROL_DB):
        raise HTTPException(status_code=403, detail="live agent-control database cannot be imported")
    try:
        return get_plane().import_agent_control(source, observations=body.get("observations") or [])
    except ControlPlaneError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
