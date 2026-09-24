"""Isolated Antigravity profile homes for Vicoa.

These homes are not the live Agent Control runtimes. Provisioning creates an
empty directory and a control-plane account. It does not copy credentials,
keyring entries, Gemini state, or Atrium sessions.
"""

from __future__ import annotations

from pathlib import Path

from .store import LIVE_AGENT_CONTROL_DB, ControlPlane, ControlPlaneError

LIVE_RUNTIME_HOMES = (
    Path("/home/agentctl/runtimes/agy-1"),
    Path("/home/agentctl/runtimes/agy-2"),
)
PROFILE_IDS = ("vicoa-agy-1", "vicoa-agy-2")


def provision_isolated_profile(plane: ControlPlane, account_id: str, root: str | Path) -> dict[str, str | bool | int]:
    if account_id not in PROFILE_IDS:
        raise ControlPlaneError("rejected", "only vicoa-agy-1 and vicoa-agy-2 may be provisioned here")
    home = (Path(root) / account_id).resolve()
    _refuse_live(home)
    if any(part.name == ".gemini" for part in home.parents):
        raise ControlPlaneError("rejected", "profile home must not sit inside an existing Gemini state directory")
    home.mkdir(parents=True, exist_ok=False)
    (home / ".vicoa-profile").write_text(f"{account_id}\n", encoding="utf-8")
    (home / ".tmux").mkdir()
    plane.create_account(
        account_id=account_id,
        provider="antigravity",
        runtime_home=str(home),
        profile=account_id,
        auth_state="owner_auth_required",
    )
    return {
        "account_id": account_id,
        "authenticated": False,
        "sessions_inherited": 0,
        "live_home": False,
        "credential_files_copied": 0,
    }


def _refuse_live(home: Path) -> None:
    if str(home).startswith("/home/agentctl/") or str(home) == "/home/agentctl":
        raise ControlPlaneError("live_home", "refusing to create a profile under the live agentctl home")
    for live in LIVE_RUNTIME_HOMES:
        if home == live or live in home.parents:
            raise ControlPlaneError("live_home", "refusing to use a live Agent Control runtime")
    if home == LIVE_AGENT_CONTROL_DB.parent or LIVE_AGENT_CONTROL_DB.parent in home.parents:
        raise ControlPlaneError("live_home", "refusing to use the live Agent Control state directory")
