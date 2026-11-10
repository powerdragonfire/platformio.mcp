"""Only explicitly supported, bounded metadata may enter usage events."""

import pytest

from platformio_mcp import __version__, telemetry


@pytest.fixture(autouse=True)
def clean_metadata_env(monkeypatch):
    for key in ("PLATFORMIO_MCP_MODEL", "PLATFORMIO_MCP_REASONING_EFFORT", "PLATFORMIO_MCP_CAPTURE_PROMPTS"):
        monkeypatch.delenv(key, raising=False)


def test_metadata_precedence_and_provenance(monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_MODEL", "configured-model")
    monkeypatch.setenv("PLATFORMIO_MCP_REASONING_EFFORT", "low")
    props = telemetry.metadata_properties({
        "x-codex-turn-metadata": {"model": "client-model", "reasoning_effort": "high"},
        "platformio.mcp": {"model": "host-model", "reasoning_effort": "medium"},
        "secret": "never copy metadata wholesale",
    })
    assert props == {
        "$mcp_llm_model": "client-model", "$mcp_llm_model_source": "client_metadata",
        "reasoning_effort": "high", "reasoning_effort_source": "client_metadata",
    }


@pytest.mark.parametrize("meta", [None, [], "bad", {"x-codex-turn-metadata": "bad"}, {"platformio.mcp": {"model": "unknown", "reasoning_effort": "guess"}}])
def test_unknown_or_malformed_metadata_is_omitted(meta):
    assert telemetry.metadata_properties(meta) == {}


def test_environment_fallback_has_explicit_source(monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_MODEL", "configured-model")
    monkeypatch.setenv("PLATFORMIO_MCP_REASONING_EFFORT", "xhigh")
    props = telemetry.metadata_properties(None)
    assert props["$mcp_llm_model"] == "configured-model"
    assert props["$mcp_llm_model_source"] == "configuration"
    assert props["reasoning_effort"] == "xhigh"
    assert props["reasoning_effort_source"] == "configuration"


@pytest.mark.parametrize("opt_in", [None, "0", "false", "true", "yes", "1"])
def test_prompt_requires_local_opt_in_and_is_bounded(monkeypatch, opt_in):
    if opt_in is not None:
        monkeypatch.setenv("PLATFORMIO_MCP_CAPTURE_PROMPTS", opt_in)
    props = telemetry.metadata_properties({"platformio.mcp": {"prompt": "p" * 5000}})
    if opt_in == "1":
        assert props["user_prompt"] == "p" * 2000
        assert props["user_prompt_truncated"] is True
        assert props["user_prompt_source"] == "client_metadata"
    else:
        assert "user_prompt" not in props


def test_sdk_properties_include_actual_version_and_context_metadata(monkeypatch):
    from types import SimpleNamespace
    props = telemetry.sdk_properties({}, {"ctx": SimpleNamespace(meta={"platformio.mcp": {"reasoning_effort": "high"}})})
    assert props["platformio_mcp_version"] == __version__
    assert props["reasoning_effort"] == "high"


def test_sdk_self_report_takes_precedence_over_static_model(monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_MODEL", "configured-model")
    props = telemetry.sdk_properties({"params": {"arguments": {"llm_model": "call-model"}}}, {})
    assert "$mcp_llm_model" not in props  # SDK owns this per-call field.


@pytest.mark.parametrize("value", ["", "unknown", "secret text with spaces", "m" * 129, {}, None])
def test_invalid_model_does_not_enter_events(value):
    assert telemetry.metadata_properties({"platformio.mcp": {"model": value}}) == {}
