"""Continuation helpers. They record sessions. They do not launch workers."""

from __future__ import annotations

from typing import Any

from .knowledge import CONTEXT_SAFETY_MARGIN, PRESSURE_RATIO, estimate_tokens
from .store import ControlPlane, ControlPlaneError

ROLLOVER_MARGIN = CONTEXT_SAFETY_MARGIN
LEGACY_ACCOUNTS = frozenset({"agy-1", "agy-2"})


def rollover_due(used_tokens: int, budget_tokens: int) -> bool:
    if budget_tokens <= 0:
        return False
    return (used_tokens * ROLLOVER_MARGIN) / budget_tokens >= PRESSURE_RATIO


def compare_estimator(samples: list[str], tokenizer=None) -> dict[str, Any]:
    rows = []
    for text in samples:
        estimate = estimate_tokens(text)
        actual = None if tokenizer is None else len(tokenizer.encode(text))
        rows.append({"estimate": estimate, "actual": actual, "chars": len(text)})
    underestimated = [row for row in rows if row["actual"] is not None and row["estimate"] < row["actual"]]
    return {
        "samples": len(rows),
        "tokenizer": tokenizer is not None,
        "underestimated": len(underestimated),
        "margin": ROLLOVER_MARGIN,
        "kept": not underestimated,
    }


def render_pack(pack: dict[str, Any], *, adapter: str) -> str:
    lines = [f"adapter={adapter}", "source=vicoa-control-plane"]
    sections = pack.get("sections") or {}
    if isinstance(sections, dict):
        items = sections.items()
    else:
        items = ((item.get("key", "section"), item.get("text", "")) for item in sections)
    for key, value in items:
        lines.append(f"[{key}] {value}")
    return "\n".join(lines)


def failover_account(plane: ControlPlane, task_id: int, *, drained_account: str) -> dict[str, Any]:
    if drained_account in LEGACY_ACCOUNTS:
        raise ControlPlaneError("legacy_account", "refusing to fail over through a legacy account")
    plane.drain(drained_account)
    decision = plane.route(task_id)
    if decision["account_id"] in LEGACY_ACCOUNTS or decision["account_id"] == drained_account:
        raise ControlPlaneError("legacy_account", "router selected a drained or legacy account")
    return decision
