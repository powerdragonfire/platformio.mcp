import pytest

from platformio_mcp import core
from platformio_mcp.pio import PioNotFound


def test_policy_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("PLATFORMIO_MCP_POLICY", raising=False)
    assert core.policy() == "full"
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "Build_Only ")
    assert core.policy() == "build_only"
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "nonsense")
    assert core.policy() == "full"


def test_check_policy(monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "build_only")
    core.check_policy("build")
    with pytest.raises(core.PolicyError):
        core.check_policy("flash")
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "read_only")
    with pytest.raises(core.PolicyError):
        core.check_policy("build")


def test_guard_converts_expected_errors(monkeypatch):
    @core.guard
    def boom(kind):
        raise {"pio": PioNotFound("no pio"), "policy": core.PolicyError("nope"), "missing": FileNotFoundError("gone"), "value": ValueError("bad")}[kind]

    assert boom("pio")["error"] == "pio_not_found"
    assert boom("policy")["error"] == "policy_denied"
    assert boom("missing")["error"] == "not_found"
    assert boom("value") == {"ok": False, "error": "ValueError", "summary": "bad"}


CFG = {
    "platformio": {"default_envs": "view"},
    "base_config": {"platform": "espressif32@3.4", "monitor_speed": "921600", "lib_deps": ["a", "b"]},
    "env:view": {"extends": ["base_config"], "board": "esp32doit-devkit-v1", "lib_deps": ["${base_config.lib_deps}", "c"]},
    "env:native": {"platform": "native"},
    "env": {"monitor_speed": "9600"},
}


def test_env_helpers():
    assert core.env_names(CFG) == ["view", "native"]
    assert core.default_envs(CFG) == ["view"]
    assert core.env_setting(CFG, "view", "platform") == "espressif32@3.4"
    assert core.env_setting(CFG, "view", "monitor_speed") == "921600"
    assert core.env_setting(CFG, "native", "monitor_speed") == "9600"
    assert core.env_setting(CFG, "native", "board", "none") == "none"
    assert core.env_setting({**CFG, "env:view": {**CFG["env:view"], "extends": "base_config"}}, "view", "board") == "esp32doit-devkit-v1"


def test_guard_reports_bad_regex_and_unexpected_errors():
    import re

    @core.guard
    def bad_regex():
        re.compile("(")

    @core.guard
    def surprise():
        raise ZeroDivisionError("boom")

    r = bad_regex()
    assert r["ok"] is False and r["error"] == "bad_regex" and "(" in r["summary"]
    r = surprise()
    assert r["ok"] is False and r["error"] == "ZeroDivisionError" and "boom" in r["summary"]
