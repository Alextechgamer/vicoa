"""Isolated profile provisioning. Does not contact Antigravity."""

from pathlib import Path

import pytest

from shared.control_plane.profiles import provision_isolated_profile
from shared.control_plane.store import ControlPlane, ControlPlaneError


def test_provisioned_profiles_do_not_touch_live_homes(tmp_path: Path) -> None:
    plane = ControlPlane(tmp_path / "plane.db")
    first = provision_isolated_profile(plane, "vicoa-agy-1", tmp_path / "homes")
    second = provision_isolated_profile(plane, "vicoa-agy-2", tmp_path / "homes")
    assert first["authenticated"] is False
    assert second["sessions_inherited"] == 0
    homes = {row["runtime_home_hash"] for row in plane.accounts()}
    assert len(homes) == 2
    assert not (tmp_path / "homes" / "vicoa-agy-1" / ".gemini").exists()
    assert not (tmp_path / "homes" / "vicoa-agy-2" / ".gemini").exists()
    plane.drain("vicoa-agy-1")
    job = plane.create_job(project="/tmp/canary", goal="route around a drained profile")
    task = plane.add_task(job["id"], title="route", plan_key="route")
    decision = plane.route(task["id"])
    assert decision["account_id"] == "vicoa-agy-2"
    assert any(item["account_id"] == "vicoa-agy-1" and item["reason"] == "drained" for item in decision["considered"])


def test_provision_refuses_live_agentctl_paths(tmp_path: Path) -> None:
    plane = ControlPlane(tmp_path / "plane.db")
    with pytest.raises(ControlPlaneError) as exc:
        provision_isolated_profile(plane, "vicoa-agy-1", "/home/agentctl/runtimes")
    assert exc.value.code == "live_home"
