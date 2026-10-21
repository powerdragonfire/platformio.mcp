"""Partition-table parsing/checking and the flash tools with esptool and the toolchain faked out."""

import time

import pytest
from test_monitor import FakePort

from platformio_mcp import partitions, toolchain
from platformio_mcp.core import MonitorManager
from platformio_mcp.pio import PioResult
from platformio_mcp.tools import flash

DEFAULT_CSV = """# Name,   Type, SubType, Offset,  Size, Flags
nvs,      data, nvs,     0x9000,  0x5000,
otadata,  data, ota,     0xe000,  0x2000,
app0,     app,  ota_0,   0x10000, 0x140000,
app1,     app,  ota_1,   0x150000,0x140000,
spiffs,   data, spiffs,  0x290000,0x160000,
coredump, data, coredump,0x3F0000,0x10000,
"""


# --- pure parsers -----------------------------------------------------------------------------


def test_parse_csv_default_layout():
    parts = partitions.parse_partition_csv(DEFAULT_CSV)
    assert [p.name for p in parts] == ["nvs", "otadata", "app0", "app1", "spiffs", "coredump"]
    app0 = parts[2]
    assert (app0.type_name, app0.subtype_name, app0.offset, app0.size) == ("app", "ota_0", 0x10000, 0x140000)
    assert parts[-1].subtype_name == "coredump" and parts[-1].end == 0x400000


def test_parse_csv_auto_offsets_sizes_and_flags():
    text = "nvs, data, nvs, , 24K\nfactory, app, factory, , 1M, encrypted\nfs, data, spiffs, , 0x1000, readonly\n"
    nvs, factory, fs = partitions.parse_partition_csv(text)
    assert nvs.offset == 0x9000 and nvs.size == 24 * 1024
    assert factory.offset == 0x10000 and factory.size == 1024 * 1024 and factory.to_dict()["flags"] == ["encrypted"]
    assert fs.offset == 0x110000 and fs.to_dict()["flags"] == ["readonly"]


def test_parse_csv_rejects_garbage():
    with pytest.raises(ValueError):
        partitions.parse_partition_csv("app0, app, ota_0\n")
    with pytest.raises(ValueError):
        partitions.parse_partition_csv("x, blob, nvs, 0x9000, 0x1000\n")


def test_binary_roundtrip_with_md5_and_padding():
    parts = partitions.parse_partition_csv(DEFAULT_CSV)
    blob = partitions.to_binary(parts)
    assert len(blob) == partitions.TABLE_SIZE and blob[6 * 32 : 6 * 32 + 2] == partitions.MD5_MAGIC
    back = partitions.parse_partition_bin(blob)
    assert [p.to_dict() for p in back] == [p.to_dict() for p in parts]
    assert partitions.parse_partition_bin(partitions.to_binary(parts, with_md5=False)) == parts


def test_binary_rejects_erased_or_foreign_data():
    assert partitions.parse_partition_bin(b"\xff" * 64) == []
    with pytest.raises(ValueError):
        partitions.parse_partition_bin(b"\x00" * 64)


def test_check_partitions_healthy_table():
    parts = partitions.parse_partition_csv(DEFAULT_CSV)
    issues = partitions.check_partitions(parts, flash_size=4 * 1024 * 1024, app_size=1_000_000)
    assert [i["severity"] for i in issues] == ["info"]
    assert issues[0]["code"] == "app_fits" and "76.3%" in issues[0]["message"]


def test_check_partitions_finds_every_problem():
    text = "nvs, data, nvs, 0x9000, 0x5000\napp0, app, ota_0, 0x10800, 0x140000\napp1, app, ota_1, 0x100000, 0x100000\nfs, data, spiffs, 0x200000, 0x300000\n"
    issues = partitions.check_partitions(partitions.parse_partition_csv(text), flash_size=4 * 1024 * 1024, app_size=0x140001)
    codes = {i["code"] for i in issues}
    assert {"misaligned", "overlap", "exceeds_flash", "ota_without_otadata", "uneven_ota_slots", "app_too_big", "no_coredump"} <= codes
    assert all(i["fix"] for i in issues)


def test_check_partitions_single_slot_and_otadata_mismatch():
    single = "otadata, data, ota, 0xe000, 0x2000\nfactory, app, factory, 0x10000, 0x100000\n"
    codes = {i["code"] for i in partitions.check_partitions(partitions.parse_partition_csv(single))}
    assert "otadata_without_ota" in codes and "no_nvs" in codes
    one_ota = "nvs, data, nvs, 0x9000, 0x5000\notadata, data, ota, 0xe000, 0x2000\napp0, app, ota_0, 0x10000, 0x100000\n"
    codes = {i["code"] for i in partitions.check_partitions(partitions.parse_partition_csv(one_ota), flash_size=16 * 1024 * 1024)}
    assert "single_ota_slot" in codes and "unused_flash" in codes


def test_diff_partitions_orders_by_severity():
    exp = partitions.parse_partition_csv(DEFAULT_CSV)
    act = partitions.parse_partition_csv(DEFAULT_CSV.replace("0x140000", "0x180000").replace("coredump, data, coredump,0x3F0000,0x10000,", "extra, data, spiffs, 0x3F0000, 0x10000,"))
    diff = partitions.diff_partitions(exp, act)
    assert [(d["name"], d["kind"]) for d in diff] == [("coredump", "missing_on_device"), ("app0", "changed"), ("app1", "changed"), ("extra", "extra_on_device")]
    assert diff[1]["fields"] == ["size"]
    assert partitions.diff_partitions(exp, exp) == []


# --- tools ------------------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "platformio.ini").write_text("[env:esp]\nplatform = espressif32\nboard = esp32dev\nboard_build.partitions = parts.csv\nmonitor_port = /dev/esp\n")
    (tmp_path / "parts.csv").write_text(DEFAULT_CSV)
    build = tmp_path / ".pio" / "build" / "esp"
    build.mkdir(parents=True)
    (build / "firmware.bin").write_bytes(b"\x00" * 200_000)
    elf = build / "firmware.elf"
    elf.write_bytes(b"\x7fELF")
    monkeypatch.setattr(flash, "project_config", lambda p: {"env:esp": {"board": "esp32dev", "board_build.partitions": "parts.csv", "monitor_port": "/dev/esp"}})
    monkeypatch.setattr(flash, "_all_boards", lambda: [{"id": "esp32dev", "rom": 4 * 1024 * 1024, "mcu": "esp32"}])
    monkeypatch.setattr(flash, "_pick_port", lambda port, project_dir, env: (port or "/dev/esp", 115200))
    tc = toolchain.Toolchain(env="esp", cc_path="/tc/bin/xtensa-esp32-elf-gcc", elf_path=str(elf), prefix="/tc/bin/xtensa-esp32-elf-", addr2line=None, nm=None, size=None)
    monkeypatch.setattr(flash, "load_toolchain", lambda project_dir, env: tc)
    return tmp_path


class FakeEsptool:
    """Serves bytes for read_flash and records the commands it saw."""

    def __init__(self, image: bytes):
        self.image = image
        self.calls = []

    def __call__(self, args, cwd=None, timeout=None, tool="pio", **kw):
        self.calls.append(args)
        assert args[:6] == ["pkg", "exec", "-p", "tool-esptoolpy", "--", "esptool.py"]
        rest = args[6:]
        offset, size, out = int(rest[3], 16), int(rest[4], 16), rest[5]
        with open(out, "wb") as fh:
            fh.write(self.image[offset : offset + size].ljust(size, b"\xff"))
        return PioResult(args=args, returncode=0, output="Read OK", duration_s=0.1, log_path="/log/esptool.log")


def test_partition_table_from_csv_reports_fit_and_layout(project):
    r = flash.pio_partition_table(project_dir=str(project))
    assert r["ok"] is True and r["env"] == "esp"
    assert r["csv_source"] == "board_build.partitions" and r["flash_size"] == 4 * 1024 * 1024
    assert r["firmware_size"] == 200_000 and [p["name"] for p in r["partitions"]][2] == "app0"
    assert {i["code"] for i in r["issues"]} == {"app_fits"}
    assert "15.3%" in r["summary"] and "No layout problems" in r["summary"]


def test_partition_table_flags_oversize_and_stale_bin(project):
    (project / ".pio" / "build" / "esp" / "firmware.bin").write_bytes(b"\x00" * 0x150000)
    stale = project / ".pio" / "build" / "esp" / "partitions.bin"
    stale.write_bytes(b"\xaa\x50")
    old = time.time() - 3600
    import os

    os.utime(stale, (old, old))
    r = flash.pio_partition_table(project_dir=str(project))
    assert r["ok"] is False
    codes = [i["code"] for i in r["issues"]]
    assert codes[0] == "stale_partitions_bin" and "app_too_big" in codes
    assert "huge_app.csv" in r["summary"]


def test_partition_table_missing_csv_is_structured(project, monkeypatch):
    (project / "parts.csv").unlink()
    monkeypatch.setattr(flash, "_framework_dir", lambda path, env: None)
    r = flash.pio_partition_table(project_dir=str(project))
    assert r["ok"] is False and r["error"] == "not_found" and "board_build.partitions" in r["summary"]


def test_partition_table_device_diff(project, monkeypatch):
    on_device = partitions.parse_partition_csv(DEFAULT_CSV.replace("0x140000", "0x180000"))
    image = b"\xff" * partitions.TABLE_OFFSET + partitions.to_binary(on_device)
    fake = FakeEsptool(image.ljust(4 * 1024 * 1024, b"\xff"))
    monkeypatch.setattr(flash, "run_pio", fake)
    r = flash.pio_partition_table(project_dir=str(project), read_device=True, port="/dev/esp")
    assert r["ok"] is False and r["issues"][0]["code"] == "device_table_mismatch"
    assert {d["name"] for d in r["device"]["diff"]} == {"app0", "app1"}
    assert "pio_upload" in r["issues"][0]["fix"] and "program" in r["issues"][0]["fix"]
    assert fake.calls[0][6:] == ["--port", "/dev/esp", "read_flash", "0x8000", "0xc00", fake.calls[0][-1]]
    assert "DEVICE TABLE MISMATCH" in r["summary"]


def test_partition_table_device_matches_or_is_erased(project, monkeypatch):
    parts = partitions.parse_partition_csv(DEFAULT_CSV)
    fake = FakeEsptool((b"\xff" * partitions.TABLE_OFFSET + partitions.to_binary(parts)).ljust(4 * 1024 * 1024, b"\xff"))
    monkeypatch.setattr(flash, "run_pio", fake)
    r = flash.pio_partition_table(project_dir=str(project), read_device=True)
    assert r["ok"] is True and r["device"]["diff"] == [] and "matches" in r["summary"]
    monkeypatch.setattr(flash, "run_pio", FakeEsptool(b"\xff" * (4 * 1024 * 1024)))
    r = flash.pio_partition_table(project_dir=str(project), read_device=True)
    assert r["ok"] is False and r["device"]["erased"] is True and r["issues"][0]["code"] == "device_table_erased"


def test_partition_table_refuses_while_port_held(project, monkeypatch):
    mgr = MonitorManager(opener=lambda port, baud: FakePort([]))
    monkeypatch.setattr(flash, "monitors", mgr)
    s = mgr.start("/dev/esp", 115200)
    try:
        r = flash.pio_partition_table(project_dir=str(project), read_device=True)
        assert r["ok"] is False and s.id in r["summary"] and "pio_monitor_stop" in r["summary"]
    finally:
        mgr.stop(s.id)


COREDUMP_REPORT = """===============================================================
==================== ESP32 CORE DUMP START ====================

Crashed task handle: 0x3ffb8b1c, name: 'loopTask', GDB name: 'process 1073449756'
Crashed task is not in the interrupt context
Panic reason: abort() was called at PC 0x400d1234 on core 1

================== CURRENT THREAD REGISTERS ===================
exccause       0x1d (StoreProhibitedCause)
pc             0x400d1ff4          0x400d1ff4 <DisplayTask::run()+12>
a0             0x800d2010          -2146562032

==================== CURRENT THREAD STACK =====================
#0  0x400d1ff4 in DisplayTask::run () at src/display_task.cpp:22
#1  0x400d2010 in loop () at src/main.cpp:40
#2  0x400893e5 in loopTask (pvParameters=0x0) at main.cpp:14

======================== THREADS INFO =========================
  Id   Target Id         Frame
* 1    process 1073449756 0x400d1ff4 in DisplayTask::run () at src/display_task.cpp:22

===================== ESP32 CORE DUMP END =====================
"""


def test_parse_coredump_report():
    r = flash.parse_coredump_report(COREDUMP_REPORT)
    assert r["crashed_task"].startswith("0x3ffb8b1c, name: 'loopTask'")
    assert r["reason"] == "abort() was called at PC 0x400d1234 on core 1"
    assert r["backtrace"][0].startswith("#0  0x400d1ff4 in DisplayTask::run") and len(r["backtrace"]) == 3
    assert r["registers"]["pc"].startswith("0x400d1ff4") and r["registers"]["exccause"].startswith("0x1d")


def _dump_image(payload: bytes) -> bytes:
    image = bytearray(b"\xff" * (4 * 1024 * 1024))
    image[0x3F0000 : 0x3F0000 + len(payload)] = payload
    return bytes(image)


def test_coredump_reads_partition_and_reports_empty(project, monkeypatch, tmp_path):
    fake = FakeEsptool(b"\xff" * (4 * 1024 * 1024))
    monkeypatch.setattr(flash, "run_pio", fake)
    out = tmp_path / "dump.bin"
    r = flash.pio_coredump(project_dir=str(project), out_path=str(out))
    assert r["ok"] is False and r["error"] == "coredump_empty"
    assert out.stat().st_size == 0x10000 and r["partition"]["offset_hex"] == "0x3f0000"
    assert fake.calls[0][6:] == ["--port", "/dev/esp", "read_flash", "0x3f0000", "0x10000", fake.calls[0][-1]]
    assert "CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH" in r["summary"]


def test_coredump_without_analyzer_returns_dump_and_hint(project, monkeypatch, tmp_path):
    monkeypatch.setattr(flash, "run_pio", FakeEsptool(_dump_image(b"\x10\x00\x00\x00" + b"core" * 100)))
    monkeypatch.setattr(flash, "_analyzer_available", lambda: False)
    r = flash.pio_coredump(project_dir=str(project), out_path=str(tmp_path / "d.bin"))
    assert r["ok"] is True and r["analysis"] is None and "esp-coredump" in r["install_hint"]
    assert r["dump_bytes"] == 0x10000 and r["elf_path"].endswith("firmware.elf")


def test_coredump_runs_analyzer_and_summarizes(project, monkeypatch, tmp_path):
    monkeypatch.setattr(flash, "run_pio", FakeEsptool(_dump_image(b"\x10\x00\x00\x00" + b"core" * 100)))
    monkeypatch.setattr(flash, "_analyzer_available", lambda: True)
    seen = {}

    def fake_run(cmd, capture_output, text, errors, timeout):
        seen["cmd"] = cmd

        class P:
            returncode = 0
            stdout = COREDUMP_REPORT
            stderr = ""

        return P()

    monkeypatch.setattr(flash.subprocess, "run", fake_run)
    r = flash.pio_coredump(project_dir=str(project), out_path=str(tmp_path / "d.bin"))
    assert r["ok"] is True and r["analysis"]["crashed_task"].endswith("'process 1073449756'")
    assert "loopTask" in r["summary"] and "DisplayTask::run" in r["summary"]
    cmd = seen["cmd"]
    assert cmd[1:5] == ["-m", "esp_coredump", "--chip", "esp32"] and "--core-format" in cmd and cmd[-1].endswith("firmware.elf")


def test_coredump_needs_coredump_partition(project, monkeypatch):
    (project / "parts.csv").write_text("nvs, data, nvs, 0x9000, 0x5000\nfactory, app, factory, 0x10000, 0x100000\n")
    monkeypatch.setattr(flash, "run_pio", FakeEsptool(b"\xff" * 1024))
    r = flash.pio_coredump(project_dir=str(project))
    assert r["ok"] is False and r["error"] == "KeyError" and "no coredump partition" in r["summary"]


def test_coredump_refuses_while_port_held(project, monkeypatch):
    mgr = MonitorManager(opener=lambda port, baud: FakePort([]))
    monkeypatch.setattr(flash, "monitors", mgr)
    s = mgr.start("/dev/esp", 115200)
    try:
        r = flash.pio_coredump(project_dir=str(project))
        assert r["ok"] is False and s.id in r["summary"]
    finally:
        mgr.stop(s.id)
