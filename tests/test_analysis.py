"""Tool-level tests for analysis tools with the toolchain and hardware faked out."""

import time

import pytest
from test_monitor import FakePort

from platformio_mcp import core, toolchain
from platformio_mcp.tools import analysis


@pytest.fixture
def project(tmp_path):
    (tmp_path / "platformio.ini").write_text("[env:view]\nplatform = espressif32\nboard = esp32doit-devkit-v1\nmonitor_speed = 115200\n")
    elf = tmp_path / ".pio" / "build" / "view" / "firmware.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"\x7fELF")
    return tmp_path, elf


@pytest.fixture
def fake_toolchain(monkeypatch, project):
    path, elf = project
    tc = toolchain.Toolchain(env="view", cc_path="/tc/bin/xtensa-esp32-elf-gcc", elf_path=str(elf), prefix="/tc/bin/xtensa-esp32-elf-", addr2line="/tc/bin/xtensa-esp32-elf-addr2line", nm="/tc/bin/xtensa-esp32-elf-nm", size="/tc/bin/xtensa-esp32-elf-size")
    monkeypatch.setattr(analysis, "load_toolchain", lambda project_dir, env: tc)
    monkeypatch.setattr(toolchain, "load_toolchain", lambda project_dir, env: tc)
    monkeypatch.setattr(analysis, "project_config", lambda p: {"env:view": {"board": "esp32doit-devkit-v1", "monitor_speed": "115200"}})
    monkeypatch.setattr(analysis, "_all_boards", lambda: [{"id": "esp32doit-devkit-v1", "rom": 4194304, "ram": 327680, "mcu": "esp32"}])
    calls = []

    def run_tool(cmd, timeout=60):
        calls.append(cmd)
        exe = cmd[0].rsplit("-", 1)[-1]
        if exe == "addr2line":
            addrs = cmd[cmd.index("-e") + 2 :]
            table = {"0x400d1ff4": "DisplayTask::run() at /p/src/display_task.cpp:22", "0x400d2010": "DisplayTask::run() at /p/src/display_task.cpp:23"}
            return "\n".join(f"{a}: {table.get(a, '?? ??:0')}" for a in addrs) + "\n"
        if exe == "size" and "-A" in cmd:
            return ".flash.text 190234 1074593816\n.dram0.bss 9880 1073480416\n.dram0.data 10108 1073470304\n.debug_info 99 0\n"
        if exe == "size":
            return "   text\t   data\t    bss\t    dec\t    hex\tfilename\n 248438\t 172324\t   9880\t 430642\t  69232\tfirmware.elf\n"
        if exe == "nm":
            return "400d1ff4 00000737 T DisplayTask::run()\t" + str(path) + "/src/display_task.cpp:22\n3f40064c 00002e66 d Roboto_Light_60Bitmaps\n"
        raise AssertionError(cmd)

    monkeypatch.setattr(analysis, "run_tool", run_tool)
    monkeypatch.setattr(toolchain, "run_tool", run_tool)
    monkeypatch.setattr(analysis, "_pio_memory", lambda path, env: ({}, None))
    return calls


CRASH = "Guru Meditation Error: Core  1 panic'ed (LoadProhibited).\nPC      : 0x400d1ff4  A0      : 0x800d2010\nBacktrace: 0x400d1ff4:0x3ffb1f30 0x400d2010:0x3ffb1f50 0x40088b6d:0x3ffb1f70\n"


def test_decode_backtrace_from_text(project, fake_toolchain):
    path, _ = project
    r = analysis.pio_decode_backtrace(project_dir=str(path), env="view", text=CRASH)
    assert r["ok"] is True
    assert r["causes"][0].startswith("Guru Meditation")
    bt = [f for f in r["frames"] if f["role"] == "backtrace"]
    assert [f["line"] for f in bt] == [22, 23, None]
    assert bt[2]["resolved"] is False
    assert "DisplayTask::run() (display_task.cpp:22)" in r["summary"]
    # one addr2line call with every address, no duplicates
    a2l = [c for c in fake_toolchain if c[0].endswith("addr2line")]
    assert len(a2l) == 1 and len(a2l[0][a2l[0].index("-e") + 2 :]) == 3


def test_decode_backtrace_from_session(project, fake_toolchain, monkeypatch):
    path, _ = project
    mgr = core.MonitorManager(opener=lambda port, baud: FakePort([CRASH.encode()]))
    monkeypatch.setattr(analysis, "monitors", mgr)
    s = mgr.start("/dev/fake", 115200)
    time.sleep(0.05)
    r = analysis.pio_decode_backtrace(project_dir=str(path), env="view", session_id=s.id)
    assert r["ok"] and r["frames"][0]["function"] == "DisplayTask::run()"
    mgr.stop(s.id)


def test_decode_backtrace_needs_input_and_addresses(project, fake_toolchain):
    path, _ = project
    r = analysis.pio_decode_backtrace(project_dir=str(path), env="view")
    assert r["ok"] is False and r["error"] == "ValueError"
    r = analysis.pio_decode_backtrace(project_dir=str(path), env="view", text="booted fine")
    assert r["ok"] is False and r["error"] == "no_addresses"


def test_decode_backtrace_missing_elf(project, fake_toolchain):
    path, elf = project
    elf.unlink()
    r = analysis.pio_decode_backtrace(project_dir=str(path), env="view", text=CRASH)
    assert r["ok"] is False and r["error"] == "not_found" and "pio_build" in r["summary"]


def test_size_report(project, fake_toolchain, monkeypatch):
    path, _ = project
    r = analysis.pio_size_report(project_dir=str(path), env="view", top=5)
    assert r["ok"] is True
    assert r["totals"]["flash_estimate"] == 420762 and r["flash_percent"] == 10.0 and r["memory_source"] == "estimate_from_size"
    assert r["ram_percent"] == round(100 * 182204 / 327680, 1)
    assert r["top_symbols"][0]["name"] == "Roboto_Light_60Bitmaps"
    assert r["top_files"][0]["file"] in ("<no debug info>", "src/display_task.cpp")
    assert any(f["file"] == "src/display_task.cpp" for f in r["top_files"])
    assert {s["section"] for s in r["sections"]} == {".flash.text", ".dram0.bss", ".dram0.data"}
    assert r["region_totals"] == {"flash": 190234, "ram": 9880, "both": 10108}
    assert "esp32doit-devkit-v1" in r["summary"]
    monkeypatch_mem = {"ram": {"percent": 6.1, "used_bytes": 19988, "total_bytes": 327680}, "flash": {"percent": 32.1, "used_bytes": 420762, "total_bytes": 1310720}}
    monkeypatch.setattr(analysis, "_pio_memory", lambda path, env: (monkeypatch_mem, "/tmp/size.log"))
    r = analysis.pio_size_report(project_dir=str(path), env="view")
    assert r["flash_percent"] == 32.1 and r["ram_percent"] == 6.1 and r["memory_source"] == "platformio" and "per PlatformIO" in r["summary"]
    filtered = analysis.pio_size_report(project_dir=str(path), env="view", filter="display")
    assert [s["name"] for s in filtered["top_symbols"]] == ["DisplayTask::run()"]


def _verify_setup(monkeypatch, project, boot_chunks, upload_ok=True):
    path, _ = project
    mgr = core.MonitorManager(opener=lambda port, baud: FakePort(boot_chunks))
    monkeypatch.setattr(analysis, "monitors", mgr)
    monkeypatch.setattr(analysis, "_pick_port", lambda port, project_dir, env: (port or "/dev/fake", 115200))
    uploads = []

    def fake_upload(project_dir, env, upload_port):
        uploads.append(upload_port)
        return {"ok": upload_ok, "summary": "upload ok" if upload_ok else "esptool failed", "environments": ["view"], "memory": {}, "warning_count": 0, "log_path": "/tmp/x.log", "duration_s": 1.0}

    monkeypatch.setattr(analysis, "pio_upload", fake_upload)
    return path, mgr, uploads


def test_flash_and_verify_pass(monkeypatch, project, fake_toolchain):
    path, mgr, uploads = _verify_setup(monkeypatch, project, [b"ets Jun  8 2016\r\nboot\r\n", b"WiFi connected\r\nloop\r\n"])
    r = analysis.pio_flash_and_verify(project_dir=str(path), env="view", expect="WiFi connected", timeout_s=2)
    assert r["ok"] is True and r["verdict"] == "pass"
    assert r["matched_line"] == "WiFi connected" and r["lines"] == ["ets Jun  8 2016", "boot", "WiFi connected"]
    assert uploads == ["/dev/fake"] and mgr.list() == []
    assert r["summary"].startswith("PASS")


def test_flash_and_verify_fail_decodes_crash(monkeypatch, project, fake_toolchain):
    path, mgr, _ = _verify_setup(monkeypatch, project, [b"boot\r\nGuru Meditation Error: Core  1 panic'ed (LoadProhibited).\r\n", b"PC      : 0x400d1ff4\r\nBacktrace: 0x400d1ff4:0x3ffb1f30 0x400d2010:0x3ffb1f50\r\n"])
    r = analysis.pio_flash_and_verify(project_dir=str(path), env="view", expect="WiFi connected", timeout_s=2, settle_s=0.2)
    assert r["ok"] is False and r["verdict"] == "fail"
    assert r["matched_line"].startswith("Guru Meditation")
    assert any("Backtrace" in l for l in r["lines"]), "settle window should have collected the backtrace"
    assert r["decoded"]["ok"] and r["decoded"]["frames"][0]["file"].endswith("display_task.cpp")
    assert "display_task.cpp:22" in r["summary"] and mgr.list() == []


def test_flash_and_verify_timeout(monkeypatch, project, fake_toolchain):
    path, _, _ = _verify_setup(monkeypatch, project, [b"boot\r\n"])
    r = analysis.pio_flash_and_verify(project_dir=str(path), env="view", expect="never", timeout_s=0.3)
    assert r["verdict"] == "timeout" and r["ok"] is False and r["lines"] == ["boot"]
    assert r["summary"].startswith("TIMEOUT")


def test_flash_and_verify_upload_failed_and_policy(monkeypatch, project, fake_toolchain):
    path, mgr, _ = _verify_setup(monkeypatch, project, [b"boot\r\n"], upload_ok=False)
    r = analysis.pio_flash_and_verify(project_dir=str(path), env="view", timeout_s=1)
    assert r["verdict"] == "upload_failed" and mgr.list() == []
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "build_only")
    r = analysis.pio_flash_and_verify(project_dir=str(path), env="view")
    assert r["error"] == "policy_denied"


def test_flash_and_verify_refuses_or_stops_open_session(monkeypatch, project, fake_toolchain):
    path, mgr, uploads = _verify_setup(monkeypatch, project, [b"ready\r\n"])
    held = mgr.start("/dev/fake", 115200)
    r = analysis.pio_flash_and_verify(project_dir=str(path), env="view", timeout_s=1)
    assert r["ok"] is False and held.id in r["summary"] and uploads == []
    r = analysis.pio_flash_and_verify(project_dir=str(path), env="view", timeout_s=1, stop_open_sessions=True)
    assert r["verdict"] == "pass" and uploads == ["/dev/fake"]
