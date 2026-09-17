"""OTA upload tool with PlatformIO, espota, and the network faked out."""

import pytest

from platformio_mcp.pio import PioResult
from platformio_mcp.tools import ota

OK_OUTPUT = """Processing esp32 (platform: espressif32; board: esp32dev; framework: arduino)
RAM:   [=         ]  14.2% (used 46504 bytes from 327680 bytes)
Flash: [=======   ]  67.3% (used 881993 bytes from 1310720 bytes)
Configuring upload protocol...
AVAILABLE: cmsis-dap, esp-prog, espota, esptool, iot-bus-jtag, jlink, minimodule, olimex-arm-usb-ocd, olimex-arm-usb-ocd-h, olimex-arm-usb-tiny-h, olimex-jtag-tiny, tumpa
CURRENT: upload_protocol = espota
Uploading .pio/build/esp32/firmware.bin
16:33:51 [DEBUG]: Options: {'esp_ip': '192.168.1.42', 'host_ip': '0.0.0.0', 'esp_port': 3232, 'host_port': 47617, 'auth': 'secret', 'image': '.pio/build/esp32/firmware.bin', 'spiffs': False, 'debug': True, 'progress': True, 'timeout': 10}
16:33:51 [INFO]: Starting on 0.0.0.0:47617
16:33:51 [INFO]: Upload size: 881993
Sending invitation to 192.168.1.42
Authenticating...OK
16:33:52 [INFO]: Waiting for device...
Uploading: [                                                            ] 0%
Uploading: [==============================                              ] 50%
Uploading: [============================================================ ] 100% Done...

16:34:01 [INFO]: Waiting for result...
16:34:02 [INFO]: Result: OK
16:34:02 [INFO]: Success
========================= [SUCCESS] Took 14.02 seconds =========================
"""

NO_RESPONSE = """Uploading .pio/build/esp32/firmware.bin
16:40:00 [INFO]: Starting on 0.0.0.0:51234
16:40:00 [INFO]: Upload size: 881993
Sending invitation to 192.168.1.42 ..........
16:40:10 [ERROR]: No response from the ESP
*** [upload] Error 1
========================== [FAILED] Took 12.11 seconds ==========================
"""

AUTH_FAILED = """Sending invitation to 192.168.1.42
Authenticating...FAIL
16:41:00 [ERROR]: Authentication Failed
*** [upload] Error 1
========================== [FAILED] Took 3.01 seconds ==========================
"""

NO_CALLBACK = """Sending invitation to 192.168.1.42
16:42:00 [INFO]: Waiting for device...
16:42:10 [ERROR]: No response from device
*** [upload] Error 1
========================== [FAILED] Took 13.00 seconds ==========================
"""

DEVICE_REJECTED = """Uploading: [============================================================ ] 100% Done...

16:43:00 [INFO]: Waiting for result...
16:43:00 [INFO]: Result: ERROR[6]: No OTA partition
16:43:00 [ERROR]: Error response from device
*** [upload] Error 1
========================== [FAILED] Took 20.00 seconds ==========================
"""

AUTO_SWITCH = """Warning! We have just detected `upload_port` as IP address or host name of ESP device. `upload_protocol` is switched to `espota`.
Please specify `upload_protocol = espota` in `platformio.ini` project configuration file.
Sending invitation to 192.168.1.42 ..........
16:44:00 [ERROR]: No response from the ESP
========================== [FAILED] Took 12.00 seconds ==========================
"""

BUILD_FAILED = """src/main.cpp:12:5: error: 'foo' was not declared in this scope
*** [.pio/build/esp32/src/main.cpp.o] Error 1
========================== [FAILED] Took 2.00 seconds ==========================
"""


@pytest.mark.parametrize(
    "text, success, error, pct",
    [
        (OK_OUTPUT, True, None, 100),
        (NO_RESPONSE, False, "no_response", None),
        (AUTH_FAILED, False, "auth_failed", None),
        (NO_CALLBACK, False, "no_callback", None),
        (DEVICE_REJECTED, False, "device_rejected", 100),
        ("", False, None, None),
    ],
)
def test_parse_espota(text, success, error, pct):
    r = ota.parse_espota(text)
    assert r["success"] is success
    assert r["error"] == error
    assert r["progress_percent"] == pct
    assert (r["hint"] is None) == (error is None)


def test_parse_espota_flags_auto_switch():
    r = ota.parse_espota(AUTO_SWITCH)
    assert r["auto_switched"] is True and r["error"] == "no_response"
    assert ota.parse_espota(OK_OUTPUT)["auto_switched"] is False


@pytest.fixture
def project(tmp_path):
    (tmp_path / "platformio.ini").write_text("[env:esp32]\nplatform = espressif32\nboard = esp32dev\nframework = arduino\n")
    fw = tmp_path / ".pio" / "build" / "esp32" / "firmware.bin"
    fw.parent.mkdir(parents=True)
    fw.write_bytes(b"\xe9" * 1024)
    return tmp_path


@pytest.fixture
def fake_pio(monkeypatch, project):
    monkeypatch.setattr(ota, "project_config", lambda p: {"env:esp32": {"platform": "espressif32", "board": "esp32dev", "framework": "arduino"}})
    monkeypatch.setattr(ota, "resolve_host", lambda host: "192.168.1.42")
    monkeypatch.setattr(ota, "ping", lambda ip, timeout_s=1.5: True)
    calls = []

    def run_pio(args, cwd=None, timeout=None, tool="pio", keep_log=True, env_extra=None, stdin_data=None):
        calls.append({"args": args, "env_extra": env_extra, "timeout": timeout})
        text = run_pio.output
        return PioResult(args=args, returncode=0 if "[SUCCESS]" in text else 1, output=text, duration_s=1.0, log_path="/tmp/x.log")

    run_pio.output = OK_OUTPUT
    monkeypatch.setattr(ota, "run_pio", run_pio)
    return calls, run_pio


def test_upload_ota_success_uses_pio_run_with_auth_and_port(project, fake_pio):
    calls, _ = fake_pio
    r = ota.pio_upload_ota(host="192.168.1.42", project_dir=str(project), auth="secret")
    assert r["ok"] is True and "error" not in r
    assert r["port"] == 3232 and r["upload_path"] == "pio_run" and r["progress_percent"] == 100
    assert r["firmware_bytes"] == 1024 and r["memory"]["flash"]["percent"] == 67.3
    assert calls[0]["args"] == ["run", "-d", str(project), "-e", "esp32", "-t", "upload", "--upload-port", "192.168.1.42"]
    assert calls[0]["env_extra"] == {"PLATFORMIO_UPLOAD_FLAGS": "--auth=secret --port=3232"}
    assert "succeeded" in r["summary"]


def test_upload_ota_filesystem_target_and_esp8266_port(project, fake_pio, monkeypatch):
    calls, _ = fake_pio
    monkeypatch.setattr(ota, "project_config", lambda p: {"env:esp32": {"platform": "espressif8266@4.2.1", "board": "d1_mini", "framework": "arduino"}})
    r = ota.pio_upload_ota(host="d1.local", project_dir=str(project), filesystem=True)
    assert r["port"] == 8266 and calls[0]["args"][6] == "uploadfs"
    assert calls[0]["env_extra"] == {"PLATFORMIO_UPLOAD_FLAGS": "--port=8266"}
    assert calls[0]["args"][-1] == "d1.local"  # .local names are auto-switched by the builder; no IP substitution


def test_upload_ota_plain_hostname_is_replaced_by_ip_unless_protocol_declared(project, fake_pio, monkeypatch):
    calls, _ = fake_pio
    r = ota.pio_upload_ota(host="workshop-esp", project_dir=str(project))
    assert calls[0]["args"][-1] == "192.168.1.42" and r["target_host"] == "192.168.1.42"
    monkeypatch.setattr(ota, "project_config", lambda p: {"env:esp32": {"platform": "espressif32", "upload_protocol": "espota", "upload_flags": "--auth=fromini --port=3333"}})
    r = ota.pio_upload_ota(host="workshop-esp", project_dir=str(project))
    assert calls[1]["args"][-1] == "workshop-esp" and r["port"] == 3333
    assert calls[1]["env_extra"] == {"PLATFORMIO_UPLOAD_FLAGS": "--auth=fromini --port=3333"}


@pytest.mark.parametrize("output, error", [(NO_RESPONSE, "no_response"), (AUTH_FAILED, "auth_failed"), (DEVICE_REJECTED, "device_rejected"), (BUILD_FAILED, "build_failed")])
def test_upload_ota_maps_failures(project, fake_pio, output, error):
    _, run_pio = fake_pio
    run_pio.output = output
    r = ota.pio_upload_ota(host="192.168.1.42", project_dir=str(project))
    assert r["ok"] is False and r["error"] == error
    assert r["summary"].startswith("OTA upload" if error != "build_failed" else "Build failed")
    if error == "device_rejected":
        assert "partition" in r["summary"]


def test_upload_ota_auto_switch_note(project, fake_pio):
    _, run_pio = fake_pio
    run_pio.output = AUTO_SWITCH
    r = ota.pio_upload_ota(host="192.168.1.42", project_dir=str(project))
    assert r["error"] == "no_response" and "upload_protocol = espota" in r["summary"]


def test_upload_ota_unreachable_host_fails_fast(project, fake_pio, monkeypatch):
    calls, _ = fake_pio
    monkeypatch.setattr(ota, "ping", lambda ip, timeout_s=1.5: False)
    r = ota.pio_upload_ota(host="192.168.1.42", project_dir=str(project))
    assert r["ok"] is False and r["error"] == "host_unreachable" and calls == []
    monkeypatch.setattr(ota, "resolve_host", lambda host: (_ for _ in ()).throw(RuntimeError("host 'nope' does not resolve")))
    r = ota.pio_upload_ota(host="nope", project_dir=str(project))
    assert r["ok"] is False and "does not resolve" in r["summary"] and calls == []
    r = ota.pio_upload_ota(host="nope", project_dir=str(project), verify_reachable=False)
    assert len(calls) == 1 and r["reachable"] is None


def test_upload_ota_direct_espota_path(project, fake_pio, monkeypatch, tmp_path):
    calls, _ = fake_pio
    espota = tmp_path / "pkgs" / "framework-arduinoespressif32" / "tools" / "espota.py"
    espota.parent.mkdir(parents=True)
    espota.write_text("# fake")
    monkeypatch.setenv("PLATFORMIO_PACKAGES_DIR", str(tmp_path / "pkgs"))
    seen = {}

    def fake_direct(espota_path, host, port, auth, image, filesystem, timeout):
        seen.update(espota=espota_path, host=host, port=port, auth=auth, image=image, filesystem=filesystem)
        return PioResult(args=[], returncode=0, output=OK_OUTPUT, duration_s=1.0, log_path="/tmp/y.log")

    monkeypatch.setattr(ota, "_run_espota_direct", fake_direct)
    r = ota.pio_upload_ota(host="192.168.1.42", project_dir=str(project), build=False, auth="pw", port=4000)
    assert r["ok"] and r["upload_path"] == "espota_direct" and calls == []
    assert seen["espota"] == str(espota) and seen["port"] == 4000 and seen["auth"] == "pw" and seen["image"].name == "firmware.bin"


def test_upload_ota_direct_needs_firmware(project, fake_pio, monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORMIO_PACKAGES_DIR", str(tmp_path / "empty"))
    r = ota.pio_upload_ota(host="192.168.1.42", project_dir=str(project), build=False)
    assert r["ok"] is False and r["error"] == "not_found" and "espota.py not found" in r["summary"]
    (tmp_path / "empty" / "framework-arduinoespressif32" / "tools").mkdir(parents=True)
    (tmp_path / "empty" / "framework-arduinoespressif32" / "tools" / "espota.py").write_text("")
    (project / ".pio" / "build" / "esp32" / "firmware.bin").unlink()
    r = ota.pio_upload_ota(host="192.168.1.42", project_dir=str(project), build=False)
    assert r["error"] == "not_found" and "run pio_build first" in r["summary"]


def test_upload_ota_policy_and_missing_host(monkeypatch, project):
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "build_only")
    r = ota.pio_upload_ota(host="192.168.1.42", project_dir=str(project))
    assert r["ok"] is False and r["error"] == "policy_denied"
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "full")
    r = ota.pio_upload_ota(host="  ", project_dir=str(project))
    assert r["ok"] is False and r["error"] == "ValueError"


def test_run_espota_direct_builds_command(monkeypatch, tmp_path):
    seen = {}

    class P:
        stdout, stderr, returncode = "16:00:00 [INFO]: Success\n", "", 0

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return P()

    monkeypatch.setattr(ota.subprocess, "run", fake_run)
    monkeypatch.setattr(ota, "write_log", lambda tool, text: "/tmp/log")
    img = tmp_path / "littlefs.bin"
    img.write_bytes(b"x")
    res = ota._run_espota_direct("/x/espota.py", "10.0.0.5", 3232, "pw", img, True, 30)
    assert res.ok and seen["cmd"][1:] == ["/x/espota.py", "--debug", "--progress", "-i", "10.0.0.5", "-p", "3232", "-f", str(img), "-a", "pw", "--spiffs"]
