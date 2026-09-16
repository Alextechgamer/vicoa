"""Tests for the Antigravity spec: argv, version gate, model-list parsing, cache."""

from __future__ import annotations

import json
from typing import List, Optional

import pytest

from integrations.headless.antigravity import spec


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def test_build_command_minimal_has_the_load_bearing_flags():
    argv = spec.build_command(binary="/usr/local/bin/agy", cwd="/proj")
    assert argv[0] == "/usr/local/bin/agy"
    # Driver mode, never --print (it would eat the next token on 1.2.x).
    assert "--print" not in argv and "-p" not in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    # Without --add-dir every tool runs in ~/.gemini/antigravity-cli/scratch.
    assert argv[argv.index("--add-dir") + 1] == "/proj"
    # A CLI-answered slash command on stdin would otherwise end the session.
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--print-timeout") + 1] == spec.PRINT_TIMEOUT
    assert "--model" not in argv
    assert "--mode" not in argv
    assert "--conversation" not in argv
    assert "--effort" not in argv


@pytest.mark.parametrize(
    "mode, expected",
    [
        (None, ()),
        ("default", ()),
        ("acceptEdits", ("--mode", "accept-edits")),
        ("plan", ("--mode", "plan")),
        ("bypassPermissions", ("--dangerously-skip-permissions",)),
        ("something-else", ()),
    ],
)
def test_build_command_permission_modes(mode, expected):
    argv = spec.build_command(binary="agy", cwd="/p", permission_mode=mode)
    tail = tuple(argv[argv.index("/p") + 1 :])
    assert tail == expected


def test_build_command_model_and_sentinels():
    argv = spec.build_command(binary="agy", cwd="/p", model="gemini-3.8-flash-high")
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-high"
    for sentinel in ("default", "auto", "", "  "):
        assert "--model" not in spec.build_command(
            binary="agy", cwd="/p", model=sentinel
        )


def test_build_command_conversation_and_extra_dirs():
    argv = spec.build_command(
        binary="agy", cwd="/p", conversation_id="abc-123", extra_dirs=("/att",)
    )
    assert argv[-2:] == ["--conversation", "abc-123"]
    add_dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert add_dirs == ["/p", "/att"]


# ---------------------------------------------------------------------------
# Binary + version gate
# ---------------------------------------------------------------------------


def test_resolve_binary_prefers_which_then_extra_dirs(tmp_path, monkeypatch):
    assert spec.resolve_agy_binary(which=lambda name: "/opt/bin/agy") == "/opt/bin/agy"
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    binary = home / ".local" / "bin" / "agy"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    assert spec.resolve_agy_binary(which=lambda name: None) == str(binary)
    binary.unlink()
    assert spec.resolve_agy_binary(which=lambda name: None) is None


def test_parse_version_and_at_least():
    assert spec.parse_version("1.2.2") == (1, 2, 2)
    assert spec.parse_version("agy version 1.10.0 (abc)") == (1, 10, 0)
    assert spec.parse_version("") is None
    assert spec.version_at_least("1.2.2", "1.1.15")
    assert spec.version_at_least("1.1.15", "1.1.15")
    assert not spec.version_at_least("1.1.14", "1.1.15")
    assert not spec.version_at_least("1.0.10", "1.1.15")
    # Fails open on an unreadable version.
    assert spec.version_at_least("dev build", "1.1.15")
    assert spec.version_at_least(None, "1.1.15")


def test_check_runtime_requirements_messages(tmp_path, monkeypatch):
    # An empty HOME so the real ~/.local/bin/agy can't satisfy the lookup.
    monkeypatch.setenv("HOME", str(tmp_path))
    missing = spec.check_runtime_requirements(
        which=lambda n: None, run_version=lambda c: "1.2.2"
    )
    assert (
        missing is not None and "not installed" in missing and "install.sh" in missing
    )

    old = spec.check_runtime_requirements(
        which=lambda n: "/x/agy", run_version=lambda c: "1.0.10"
    )
    assert old is not None and "too old" in old and "agy update" in old

    assert (
        spec.check_runtime_requirements(
            which=lambda n: "/x/agy", run_version=lambda c: "1.1.15"
        )
        is None
    )
    assert (
        spec.check_runtime_requirements(
            which=lambda n: "/x/agy", run_version=lambda c: None
        )
        is None
    )


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

_MODELS_TSV = (
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
    "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
)


def test_parse_models_tsv_skips_noise_and_dupes():
    text = "Loading…\n" + _MODELS_TSV + "gemini-3.8-flash-high\tdupe\n\nno-tab-line\n"
    assert spec.parse_models_tsv(text) == [
        {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"},
        {"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)"},
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (Thinking)"},
        {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)"},
    ]
    assert spec.parse_models_tsv(None) == []
    assert spec.parse_models_tsv("id-only\t") == [{"id": "id-only", "label": "id-only"}]


def test_parse_current_model_prefers_command_data():
    # Verbatim shape of `agy -p /model --output-format json` on 1.2.2.
    envelope = json.dumps(
        {
            "conversation_id": "",
            "status": "SUCCESS",
            "response": "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n",
            "command": {
                "name": "model",
                "data": {
                    "id": "gemini-3.8-flash-high",
                    "label": "Gemini 3.8 Flash (High)",
                    "effort": "high",
                    "is_default": False,
                },
            },
        }
    )
    assert spec.parse_current_model(envelope) == {
        "id": "gemini-3.8-flash-high",
        "label": "Gemini 3.8 Flash (High)",
    }
    # Falls back to the TSV in `response`, then to plain TSV, then None.
    assert spec.parse_current_model(json.dumps({"response": "m1\tModel One\n"})) == {
        "id": "m1",
        "label": "Model One",
    }
    assert spec.parse_current_model("m2\tModel Two\n") == {
        "id": "m2",
        "label": "Model Two",
    }
    assert spec.parse_current_model("") is None
    assert spec.parse_current_model("{}") is None


def test_models_cache_roundtrip_and_ttl(tmp_path):
    path = tmp_path / "models.json"
    models = [{"id": "a", "label": "A"}]
    spec.store_cached_models(models, path, now=1_000.0)
    assert spec.load_cached_models(path, now=1_000.0 + 60) == models
    assert (
        spec.load_cached_models(path, now=1_000.0 + spec.MODELS_CACHE_TTL_SECONDS + 1)
        is None
    )
    path.write_text("not json")
    assert spec.load_cached_models(path) is None
    assert spec.load_cached_models(tmp_path / "missing.json") is None


def test_fetch_models_uses_cache_then_binary(tmp_path):
    path = tmp_path / "models.json"
    calls: List[List[str]] = []

    def run(command: List[str]) -> Optional[str]:
        calls.append(command)
        return _MODELS_TSV

    first = spec.fetch_models("/x/agy", run=run, cache_path=path)
    assert [m["id"] for m in first][:2] == [
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
    ]
    assert calls == [["/x/agy", "models"]]
    second = spec.fetch_models("/x/agy", run=run, cache_path=path)
    assert second == first
    assert len(calls) == 1  # served from the cache


def test_fetch_models_failure_is_empty_and_uncached(tmp_path):
    path = tmp_path / "models.json"
    assert spec.fetch_models("/x/agy", run=lambda c: None, cache_path=path) == []
    assert not path.exists()


def test_fetch_current_model_invokes_print_model():
    seen: List[List[str]] = []

    def run(command: List[str]) -> Optional[str]:
        seen.append(command)
        return "m\tM\n"

    assert spec.fetch_current_model("/x/agy", run=run) == {"id": "m", "label": "M"}
    assert seen == [["/x/agy", "-p", "/model", "--output-format", "json"]]
