"""Daemon plumbing for the Antigravity (``agy``) wrapper.

Covers the command vector the daemon would Popen (source and frozen shapes),
alias normalization, the install/version gate, the display name, and the
resume flag — the same surface ``test_machine_daemon_command_build.py`` and
``test_machine_daemon_generic_agents.py`` cover for the other agents.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from integrations.headless.antigravity import spec as antigravity_spec
from vicoa import machine_daemon as daemon_module
from vicoa.machine_daemon import MachineDaemon


@pytest.fixture
def daemon() -> MachineDaemon:
    return MachineDaemon(api_key="test-key", base_url="http://localhost:0")


def _pair_following(cmd: list[str], flag: str) -> str | None:
    try:
        idx = cmd.index(flag)
    except ValueError:
        return None
    return cmd[idx + 1] if idx + 1 < len(cmd) else None


def _build(daemon: MachineDaemon, **kwargs: Any) -> list[str]:
    metadata = kwargs.pop("metadata", None)
    return daemon._build_headless_command(
        directory="/tmp/x",
        agent="antigravity",
        session_id="sess-1",
        metadata=metadata,
        **kwargs,
    )


class TestSourceCommand:
    def test_module_and_identity_flags(self, daemon: MachineDaemon):
        cmd = _build(daemon)
        assert cmd[:3] == [sys.executable, "-m", "integrations.headless.antigravity"]
        assert _pair_following(cmd, "--project-path") == "/tmp/x"
        assert _pair_following(cmd, "--name") == "Antigravity"
        assert _pair_following(cmd, "--session-id") == "sess-1"
        assert "--model" not in cmd
        assert "--permission-mode" not in cmd
        assert "--thinking-effort" not in cmd

    def test_model_permission_prompt_and_system_prompt(self, daemon: MachineDaemon):
        cmd = _build(
            daemon,
            metadata={
                "model": "gemini-3.8-flash-high",
                "permission_mode": "acceptEdits",
                "prompt": "hello",
                "system_prompt": "be brief",
            },
        )
        assert _pair_following(cmd, "--model") == "gemini-3.8-flash-high"
        assert _pair_following(cmd, "--permission-mode") == "acceptEdits"
        assert _pair_following(cmd, "--prompt") == "hello"
        assert _pair_following(cmd, "--system-prompt") == "be brief"

    @pytest.mark.parametrize(
        "mode", ["default", "acceptEdits", "plan", "bypassPermissions"]
    )
    def test_every_catalog_permission_mode_is_accepted(
        self, daemon: MachineDaemon, mode: str
    ):
        cmd = _build(daemon, metadata={"permission_mode": mode})
        assert _pair_following(cmd, "--permission-mode") == mode

    def test_unknown_permission_mode_is_rejected(self, daemon: MachineDaemon):
        # Validated against the catalog enum like every other agent, so the
        # daemon never ships a mode the wrapper can't translate.
        with pytest.raises(ValueError):
            _build(daemon, metadata={"permission_mode": "yolo"})

    def test_resume_appends_instance_and_conversation_handles(
        self, daemon: MachineDaemon
    ):
        cmd = _build(daemon, is_resuming=True, agent_session_id="conv-abc")
        assert _pair_following(cmd, "--resume") == "sess-1"
        assert _pair_following(cmd, "--conversation-id") == "conv-abc"

    def test_resume_without_handle_reattaches_only(self, daemon: MachineDaemon):
        cmd = _build(daemon, is_resuming=True)
        assert _pair_following(cmd, "--resume") == "sess-1"
        assert "--conversation-id" not in cmd


class TestFrozenCommand:
    def test_routes_through_headless_subcommand(
        self, daemon: MachineDaemon, monkeypatch
    ):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        cmd = _build(
            daemon,
            metadata={
                "model": "claude-sonnet-4-6",
                "permission_mode": "plan",
                "prompt": "go",
            },
        )
        assert cmd[:2] == [sys.executable, "headless"]
        assert _pair_following(cmd, "--agent") == "antigravity"
        assert _pair_following(cmd, "--cwd") == "/tmp/x"
        assert _pair_following(cmd, "--model") == "claude-sonnet-4-6"
        assert _pair_following(cmd, "--permission-mode") == "plan"
        assert _pair_following(cmd, "--prompt") == "go"


class TestIdentity:
    @pytest.mark.parametrize(
        "alias", ["antigravity", "Antigravity", "agy", "Antigravity CLI"]
    )
    def test_aliases_normalize(self, daemon: MachineDaemon, alias: str):
        assert daemon._normalize_agent(alias) == "antigravity"

    def test_display_name(self, daemon: MachineDaemon):
        assert daemon._agent_display_name("antigravity") == "Antigravity"

    def test_known_ids_include_antigravity(self, daemon: MachineDaemon):
        assert "antigravity" in daemon._known_agent_ids()
        assert daemon._agent_labels()["antigravity"] == "Antigravity"


class TestInstallCheck:
    def test_missing_binary_reports_install_hint(
        self, daemon: MachineDaemon, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.local/bin/agy
        monkeypatch.setattr(
            daemon_module, "_find_cli_in_common_locations", lambda name: None
        )
        message = daemon._check_agent_installation("antigravity")
        assert message is not None
        assert "not installed" in message
        assert "install.sh" in message

    def test_old_binary_reports_version_gate(self, daemon: MachineDaemon, monkeypatch):
        monkeypatch.setattr(
            daemon_module, "_find_cli_in_common_locations", lambda name: "/x/agy"
        )
        monkeypatch.setattr(
            antigravity_spec, "_run_capture", lambda command, **kw: "1.0.10"
        )
        message = daemon._check_agent_installation("antigravity")
        assert message is not None
        assert "too old" in message
        assert "1.1.15" in message

    def test_current_binary_is_installed(self, daemon: MachineDaemon, monkeypatch):
        monkeypatch.setattr(
            daemon_module, "_find_cli_in_common_locations", lambda name: "/x/agy"
        )
        monkeypatch.setattr(
            antigravity_spec, "_run_capture", lambda command, **kw: "1.2.2"
        )
        assert daemon._check_agent_installation("antigravity") is None
