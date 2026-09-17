"""Serial port contention: auto-releasing our own monitor sessions and explaining port failures."""

import subprocess
import time

import pytest
from test_monitor import FakePort

from platformio_mcp.monitor import MonitorManager
from platformio_mcp.parsers import classify_port_error
from platformio_mcp.pio import PioResult
from platformio_mcp.tools import analysis, build, devices

PORT = "/dev/cu.usbserial-0001"

UPLOAD_OK = "Processing esp32 (platform: espressif32)\nWriting at 0x00010000... (100 %)\nHash of data verified.\n========================= [SUCCESS] Took 4.20 seconds =========================\n"
UPLOAD_PERMISSION = "Uploading .pio/build/esp32/firmware.bin\nesptool.py v4.5.1\nSerial port COM5\nA fatal error occurred: Could not open COM5, the port doesn't exist\nserial.serialutil.SerialException: could not open port 'COM5': PermissionError(13, 'Access is denied.', None, 5)\n*** [upload] Error 2\n========================= [FAILED] Took 1.10 seconds =========================\n"
UPLOAD_BUSY = "Serial port /dev/cu.usbserial-0001\nserial.serialutil.SerialException: [Errno 16] could not open port /dev/cu.usbserial-0001: [Errno 16] Resource busy: '/dev/cu.usbserial-0001'\n*** [upload] Error 1\n========================= [FAILED] Took 0.80 seconds =========================\n"
UPLOAD_MISSING = "Serial port /dev/ttyUSB0\nserial.serialutil.SerialException: [Errno 2] could not open port /dev/ttyUSB0: [Errno 2] No such file or directory: '/dev/ttyUSB0'\n*** [upload] Error 1\n========================= [FAILED] Took 0.50 seconds =========================\n"
UPLOAD_NO_RESPONSE = "Serial port /dev/cu.usbserial-0001\nConnecting......................................\nA fatal error occurred: Failed to connect to ESP32: Timed out waiting for packet header\n*** [upload] Error 2\n========================= [FAILED] Took 12.30 seconds =========================\n"
BUILD_ERROR = "src/main.cpp:12:5: error: 'foo' was not declared in this scope\n*** [.pio/build/esp32/src/main.cpp.o] Error 1\n========================= [FAILED] Took 2.00 seconds =========================\n"


@pytest.mark.parametrize(
    "text,code",
    [
        (UPLOAD_PERMISSION, "port_permission"),
        (UPLOAD_BUSY, "port_busy"),
        (UPLOAD_MISSING, "port_missing"),
        (UPLOAD_NO_RESPONSE, "no_response"),
        ("avrdude: stk500_recv(): programmer is not responding\n", "no_response"),
        ("Device or resource busy: '/dev/ttyACM0'", "port_busy"),
        (BUILD_ERROR, None),
    ],
)
def test_classify_port_error(text, code):
    assert classify_port_error(text) == code


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "platformio.ini").write_text(f"[env:esp32]\nplatform = espressif32\nboard = esp32dev\nupload_port = {PORT}\nmonitor_speed = 115200\n")
    cfg = {"env:esp32": {"platform": "espressif32", "board": "esp32dev", "upload_port": PORT, "monitor_speed": "115200"}}
    for module in (devices, analysis):
        monkeypatch.setattr(module, "project_config", lambda p, cfg=cfg: cfg)
    return tmp_path


@pytest.fixture
def manager(monkeypatch):
    mgr = MonitorManager(opener=lambda port, baud: FakePort([b"boot\n"]))
    for module in (build, devices, analysis):
        monkeypatch.setattr(module, "monitors", mgr)
    yield mgr
    mgr.stop_all()


@pytest.fixture
def fake_pio(monkeypatch):
    """Replace run_pio in build.py; the script decides the output per call and records args."""
    calls: list[list[str]] = []
    state = {"output": UPLOAD_OK, "rc": 0}

    def run_pio(args, cwd=None, timeout=None, tool="pio", **_):
        calls.append(list(args))
        return PioResult(args=list(args), returncode=state["rc"], output=state["output"], duration_s=0.1, log_path="/tmp/x.log")

    monkeypatch.setattr(build, "run_pio", run_pio)
    monkeypatch.setattr(devices, "_devices", lambda: [{"port": PORT, "description": "CP2102 USB to UART", "hwid": "USB VID:PID=10C4:EA60", "likely_dev_board": True}])
    return calls, state


@pytest.fixture
def no_processes(monkeypatch):
    """lsof/ps stand-ins: nobody else holds any port unless the test says so."""
    holders: dict[str, list[tuple[int, str]]] = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=10, **_):
        if cmd[0] == "lsof":
            pids = holders.get(cmd[-1], [])
            return subprocess.CompletedProcess(cmd, 0 if pids else 1, stdout="".join(f"{p}\n" for p, _ in pids), stderr="")
        if cmd[0] == "ps":
            pid = int(cmd[-1])
            name = next((n for ps in holders.values() for p, n in ps if p == pid), "")
            return subprocess.CompletedProcess(cmd, 0, stdout=name + "\n", stderr="")
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(devices.subprocess, "run", fake_run)
    monkeypatch.setattr(devices.os.path, "exists", lambda p: p == PORT)
    monkeypatch.setattr(devices.os, "access", lambda p, mode: True)
    monkeypatch.setattr(devices.sys, "platform", "darwin")
    return holders


def test_upload_refuses_while_our_session_holds_port(project, manager, fake_pio):
    s = manager.start(PORT, 115200)
    r = build.pio_upload(project_dir=str(project), upload_port=PORT)
    assert r["ok"] is False and s.id in r["summary"] and "stop_open_sessions" in r["summary"]
    assert fake_pio[0] == []  # never reached pio
    assert not manager.get(s.id).closed


def test_upload_stop_open_sessions_releases_and_proceeds(project, manager, fake_pio):
    s = manager.start(PORT, 115200)
    r = build.pio_upload(project_dir=str(project), upload_port=PORT, stop_open_sessions=True)
    assert r["ok"] is True
    assert r["stopped_sessions"] == [s.id] and s.id in r["summary"]
    assert fake_pio[0][0][:2] == ["run", "-d"] and "--upload-port" in fake_pio[0][0]
    with pytest.raises(KeyError):
        manager.get(s.id)


def test_upload_without_port_stops_all_sessions(project, manager, fake_pio):
    a = manager.start(PORT, 115200)
    b = manager.start("/dev/cu.other", 9600)
    r = build.pio_upload(project_dir=str(project), stop_open_sessions=True)
    assert r["ok"] and sorted(r["stopped_sessions"]) == sorted([a.id, b.id])
    assert manager.list() == []


def test_run_target_flash_target_gets_same_guard(project, manager, fake_pio):
    s = manager.start(PORT, 115200)
    refused = build.pio_run_target("uploadfs", project_dir=str(project), upload_port=PORT)
    assert refused["ok"] is False and s.id in refused["summary"]
    ok = build.pio_run_target("uploadfs", project_dir=str(project), upload_port=PORT, stop_open_sessions=True)
    assert ok["ok"] and ok["stopped_sessions"] == [s.id]
    # non-flash targets ignore sessions entirely
    manager.start(PORT, 115200)
    assert build.pio_run_target("size", project_dir=str(project))["ok"] is True


def test_upload_failure_permission_names_process_holder(project, manager, fake_pio, no_processes):
    fake_pio[1].update(output=UPLOAD_PERMISSION, rc=1)
    no_processes[PORT] = [(4242, "screen")]
    r = build.pio_upload(project_dir=str(project), upload_port=PORT)
    assert r["ok"] is False and r["error"] == "port_permission" and r["port_error"] == "port_permission"
    diag = r["port_diagnosis"]
    assert diag["held_by_processes"] == [{"pid": 4242, "command": "screen"}] and diag["process_check"] == "lsof"
    assert "PID 4242 (screen)" in r["summary"] and r["summary"].startswith("Upload failed (port_permission)")


def test_upload_failure_busy_after_external_session(project, manager, fake_pio, no_processes):
    """The port is busy, pio fails, and diagnosis points at our own session when one appeared meanwhile."""
    fake_pio[1].update(output=UPLOAD_BUSY, rc=1)
    r = build.pio_upload(project_dir=str(project), upload_port=PORT)
    assert r["error"] == "port_busy"
    assert r["port_diagnosis"]["held_by_processes"] == [] and "busy" in r["summary"]


def test_upload_failure_missing_port(project, manager, fake_pio, no_processes, monkeypatch):
    fake_pio[1].update(output=UPLOAD_MISSING, rc=1)
    monkeypatch.setattr(devices.os.path, "exists", lambda p: False)
    r = build.pio_upload(project_dir=str(project), upload_port="/dev/ttyUSB0")
    assert r["error"] == "port_missing"
    assert r["port_diagnosis"]["exists"] is False and r["port_diagnosis"]["process_check"] == "skipped"
    assert "does not exist" in r["summary"] and "pio_list_devices" in r["summary"]


def test_upload_failure_no_response_gives_bootloader_hint(project, manager, fake_pio, no_processes):
    fake_pio[1].update(output=UPLOAD_NO_RESPONSE, rc=1)
    r = build.pio_upload(project_dir=str(project))  # port resolved from platformio.ini upload_port
    assert r["error"] == "no_response" and r["port_diagnosis"]["port"] == PORT
    assert "bootloader" in r["summary"] and "upload_speed" in r["summary"]


def test_build_errors_are_not_port_errors(project, manager, fake_pio):
    fake_pio[1].update(output=BUILD_ERROR, rc=1)
    r = build.pio_upload(project_dir=str(project), upload_port=PORT)
    assert r["ok"] is False and r["port_error"] is None and "error" not in r
    assert r["errors"][0]["line"] == 12


def test_flash_and_verify_propagates_port_diagnosis(project, manager, fake_pio, no_processes):
    fake_pio[1].update(output=UPLOAD_BUSY, rc=1)
    no_processes[PORT] = [(77, "minicom")]
    r = analysis.pio_flash_and_verify(project_dir=str(project), upload_port=PORT)
    assert r["verdict"] == "upload_failed" and r["error"] == "port_busy"
    assert r["port_diagnosis"]["held_by_processes"][0]["command"] == "minicom" and "minicom" in r["summary"]


def test_port_diagnose_reports_our_own_session(project, manager, fake_pio, no_processes):
    s = manager.start(PORT, 115200)
    time.sleep(0.02)
    r = devices.pio_port_diagnose(project_dir=str(project))
    assert r["ok"] and r["port"] == PORT and r["held_by_session"] == s.id
    assert s.id in r["summary"] and "pio_monitor_stop" in r["summary"]
    assert r["permission"] == {"readable": True, "writable": True} and r["in_device_list"] is True


def test_port_diagnose_free_port(project, manager, fake_pio, no_processes):
    r = devices.pio_port_diagnose(port=PORT)
    assert r["ok"] and r["held_by_session"] is None and r["held_by_processes"] == []
    assert "looks free" in r["summary"]


def test_port_diagnose_linux_permission_hint(project, manager, fake_pio, no_processes, monkeypatch):
    monkeypatch.setattr(devices.sys, "platform", "linux")
    monkeypatch.setattr(devices.os, "access", lambda p, mode: mode == devices.os.R_OK)
    r = devices.pio_port_diagnose(port=PORT)
    assert r["permission"] == {"readable": True, "writable": False}
    assert "dialout" in r["summary"]


def test_port_diagnose_windows_has_no_process_check(project, manager, fake_pio, no_processes, monkeypatch):
    monkeypatch.setattr(devices.sys, "platform", "win32")
    r = devices.pio_port_diagnose(port=PORT)
    assert r["exists"] is True and r["permission"] is None and r["process_check"] == "unavailable"


def test_port_holders_falls_back_to_fuser(monkeypatch):
    monkeypatch.setattr(devices.sys, "platform", "linux")

    def fake_run(cmd, **_):
        if cmd[0] == "lsof":
            raise FileNotFoundError("lsof")
        if cmd[0] == "fuser":
            return subprocess.CompletedProcess(cmd, 0, stdout="  1234\n", stderr="/dev/ttyUSB0: 1234\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="python3\n", stderr="")

    monkeypatch.setattr(devices.subprocess, "run", fake_run)
    holders, method = devices._port_holders("/dev/ttyUSB0")
    assert method == "fuser" and holders == [{"pid": 1234, "command": "python3"}]
