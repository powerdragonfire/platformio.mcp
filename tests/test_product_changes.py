"""Tool-level tests: monitor pattern timeout, global inventory in pio_system_info, multi-env pio_build."""

import pytest
from test_monitor import make_manager

from platformio_mcp import core
from platformio_mcp.pio import PioResult
from platformio_mcp.tools import build, devices, system

BUILD_OK = "Processing {env} (platform: native)\n====== [SUCCESS] Took 1.00 seconds ======\n"
BUILD_FAIL = "Processing {env} (platform: native)\nsrc/main.cpp:3:5: error: 'x' was not declared in this scope\n====== [FAILED] Took 1.00 seconds ======\n"


def _monitor(monkeypatch, chunks):
    mgr, _ = make_manager(chunks)
    monkeypatch.setattr(devices, "monitors", mgr)
    return mgr, mgr.start("/dev/fake", 115200)


def test_monitor_read_unmatched_pattern_is_not_ok(monkeypatch):
    mgr, s = _monitor(monkeypatch, [b"boot loop\n"])
    r = devices.pio_monitor_read(s.id, wait_for="WiFi connected", timeout_s=0.2)
    assert r["ok"] is False and r["error"] == "pattern_timeout" and r["matched"] is False
    assert r["lines"] == ["boot loop"] and r["cursor"] == 1
    assert "not matched within 0.2s" in r["summary"] and "pio_flash_and_verify" in r["summary"]
    mgr.stop(s.id)


def test_monitor_read_matched_pattern_stays_ok(monkeypatch):
    mgr, s = _monitor(monkeypatch, [b"boot\nWiFi connected\n"])
    r = devices.pio_monitor_read(s.id, wait_for="WiFi connected", timeout_s=2)
    assert r["ok"] is True and r["matched"] is True and "pattern_timeout" not in str(r.get("error"))
    mgr.stop(s.id)


def test_monitor_read_without_pattern_stays_ok_when_idle(monkeypatch):
    mgr, s = _monitor(monkeypatch, [])
    r = devices.pio_monitor_read(s.id, timeout_s=0.1)
    assert r["ok"] is True and r["lines"] == []
    mgr.stop(s.id)


def test_monitor_read_unmatched_on_closed_session_reports_session_closed(monkeypatch):
    mgr, s = _monitor(monkeypatch, [b"a\n"])
    s.error, s.closed = "SerialException: unplugged", True
    r = devices.pio_monitor_read(s.id, wait_for="never", timeout_s=0.1)
    assert r["ok"] is False and r["error"] == "session_closed" and r["session_error"] == "SerialException: unplugged"


PLATFORMS = [{"name": "espressif32", "version": "8.0.0", "__pkg_dir": "/pio/platforms/espressif32"}]
TOOLS_TREE = "Tools\n├── tool-esptool @ 2.50400.0 (required: platformio/tool-esptool)\n└── tool-scons @ 4.40502.0 (required: platformio/tool-scons)\n"


def test_system_info_lists_global_platforms_and_tools(monkeypatch, tmp_path):
    (tmp_path / "packages" / "tool-esptool").mkdir(parents=True)
    info = {"core_version": {"value": "6.1.0"}, "core_dir": {"value": str(tmp_path)}, "dev_platform_nums": {"value": 1}, "package_tool_nums": {"value": 2}}
    monkeypatch.setattr(system, "find_pio", lambda: "pio")
    monkeypatch.setattr(system, "run_pio", lambda args, **kw: PioResult(args=args, returncode=0, output=TOOLS_TREE if args[:2] == ["pkg", "list"] else "6.1.0", duration_s=0.1))

    def fake_json(args, **kw):
        return None, (info if args[0] == "system" else PLATFORMS)

    monkeypatch.setattr(system, "run_json", fake_json)
    r = system.pio_system_info()
    assert r["platforms"] == [{"name": "espressif32", "version": "8.0.0", "path": "/pio/platforms/espressif32"}]
    assert r["tools"] == [
        {"name": "tool-esptool", "version": "2.50400.0", "path": str(tmp_path / "packages" / "tool-esptool")},
        {"name": "tool-scons", "version": "4.40502.0", "path": None},
    ]


def _fake_run(outputs):
    calls = []

    def fake(args, **kw):
        env = args[args.index("-e") + 1]
        calls.append(env)
        out = outputs[env]
        return PioResult(args=args, returncode=0 if "SUCCESS" in out else 1, output=out, duration_s=1.0)

    return fake, calls


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "platformio.ini").write_text("[env:a]\nplatform = native\n[env:b]\nplatform = native\n")
    return str(tmp_path)


def test_build_envs_runs_sequentially_and_reports_each(monkeypatch, proj):
    fake, calls = _fake_run({"a": BUILD_OK.format(env="a"), "b": BUILD_FAIL.format(env="b")})
    monkeypatch.setattr(build, "run_pio", fake)
    r = build.pio_build(project_dir=proj, envs=["a", "b", "a"])
    assert calls == ["a", "b"], "duplicates dropped, order kept, failure does not stop later envs"
    assert r["ok"] is False and r["status"] == "partial" and r["failed_envs"] == ["b"]
    assert [x["env"] for x in r["results"]] == ["a", "b"] and r["results"][0]["ok"] and not r["results"][1]["ok"]
    assert "1 of 2 env(s) succeeded" in r["summary"] and "main.cpp:3" in r["summary"]
    assert r["duration_s"] == 2.0


def test_build_envs_all_succeed(monkeypatch, proj):
    fake, _ = _fake_run({"a": BUILD_OK.format(env="a"), "b": BUILD_OK.format(env="b")})
    monkeypatch.setattr(build, "run_pio", fake)
    r = build.pio_build(project_dir=proj, envs=["a", "b"])
    assert r["ok"] is True and r["status"] == "success" and r["failed_envs"] == []


def test_build_rejects_env_and_envs_together(proj):
    r = build.pio_build(project_dir=proj, env="a", envs=["b"])
    assert r["ok"] is False and r["error"] == "ValueError" and "either env" in r["summary"]


def test_build_single_env_result_shape_unchanged(monkeypatch, proj):
    fake, calls = _fake_run({"a": BUILD_OK.format(env="a")})
    monkeypatch.setattr(build, "run_pio", fake)
    r = build.pio_build(project_dir=proj, env="a")
    assert calls == ["a"] and "results" not in r and r["status"] == "success"
