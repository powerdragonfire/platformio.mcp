"""Memory telemetry parsing and the pio_memory_watch tool with a fake serial port."""

import time

import pytest
from test_monitor import FakePort

from platformio_mcp import core
from platformio_mcp.parsers import parse_memory_telemetry
from platformio_mcp.tools import runtime
from platformio_mcp.tools.runtime import _fit, analyze_memory, pio_memory_watch

ARDUINO_LINES = [
    "Booting...",
    "Free heap: 245000 min: 230000 largest: 110000",
    "loopTask: stack hwm 1836",
    "Free heap: 244000 min: 229500 largest: 109000",
    "ESP.getFreeHeap(): 243000",
    "freeHeap=242000 freePsram=4190000",
    "min free heap: 229000",
    "largest free block: 108000",
    "Stack HWM for wifiTask: 512 bytes",
    "uxTaskGetStackHighWaterMark(NULL) = 812",
    "httpTask: 96 bytes free",
]

ESP_IDF_BLOCK = [
    "I (1234) app: Heap summary for capabilities 0x00001800:",
    "  At 0x3ffae6e0 len 6432 free 4256 allocated 1936 min_free 4256",
    "    largest_free_block 4224 alloc_blocks 15 free_blocks 1 total_blocks 16",
    "  At 0x3ffb8000 len 155840 free 120000 allocated 30000 min_free 118000",
    "    largest_free_block 90000 alloc_blocks 100 free_blocks 5 total_blocks 105",
    "  Totals:",
    "    free 124256 allocated 31936 min_free 122256 largest_free_block 90000",
    "I (1235) app: done",
]

VTASKLIST = [
    "Name          State  Prio  Stack  Num",
    "--------------------------------------",
    "loopTask      R      1     1836   6",
    "IDLE0         R      0     420    3",
    "wifi          B      23    2100   9",
    "",
    "not a row X 1 2 3",
]


def test_parse_arduino_and_hwm_lines():
    p = parse_memory_telemetry(ARDUINO_LINES)
    assert p["recognized"]
    assert p["series"]["free_heap"] == [245000, 244000, 243000, 242000]
    assert p["series"]["min_free_heap"] == [230000, 229500, 229000]
    assert p["series"]["largest_free_block"] == [110000, 109000, 108000]
    assert p["series"]["psram_free"] == [4190000]
    assert p["tasks"] == {"loopTask": [1836], "wifiTask": [512], "unknown": [812], "httpTask": [96]}
    first = p["samples"][0]
    assert first["line"] == 1 and first["metric"] == "free_heap" and "Free heap" in first["raw"]
    assert "heap_line" in p["formats"] and "stack_hwm" in p["formats"]


def test_parse_esp_idf_heap_info_uses_totals_only():
    p = parse_memory_telemetry(ESP_IDF_BLOCK)
    assert p["series"]["free_heap"] == [124256]
    assert p["series"]["allocated"] == [31936]
    assert p["series"]["min_free_heap"] == [122256]
    assert p["series"]["largest_free_block"] == [90000]
    assert p["formats"] == ["esp_idf_heap_info"]


def test_parse_vtasklist_rows_need_header():
    p = parse_memory_telemetry(VTASKLIST)
    assert p["tasks"] == {"loopTask": [1836], "IDLE0": [420], "wifi": [2100]}
    row = next(s for s in p["samples"] if s["task"] == "wifi")
    assert row["state"] == "B" and row["priority"] == 23 and row["task_number"] == 9
    assert parse_memory_telemetry(["loopTask      R      1     1836   6"])["tasks"] == {}


def test_parse_generic_units_and_custom_pattern():
    p = parse_memory_telemetry(["heap headroom: 12 KB", "temp: 55", "mem=4321 tick"], pattern=r"mem=(?P<value>\d+)")
    assert p["series"]["heap_headroom"] == [12 * 1024]
    assert p["series"]["custom"] == [4321]
    assert "temp" not in " ".join(p["series"])
    named = parse_memory_telemetry(["rx=77"], pattern=r"(?P<name>rx)=(?P<value>\d+)")
    assert named["series"] == {"rx": [77]}
    with pytest.raises(ValueError):
        parse_memory_telemetry(["x"], pattern=r"\d+")


def test_parse_nothing_recognized():
    p = parse_memory_telemetry(["hello", "WiFi connected", "temperature 23.5"])
    assert p == {"samples": [], "series": {}, "tasks": {}, "formats": [], "recognized": False}


def test_fit_and_verdicts():
    assert _fit([10, 20, 30, 40]) == pytest.approx(10.0)
    assert _fit([5]) == 0.0
    stable = analyze_memory([f"Free heap: {240000 + (i % 2) * 40}" for i in range(10)])
    assert stable["metrics"]["free_heap"]["verdict"] == "stable" and not stable["metrics"]["free_heap"]["leak_suspected"]
    leak = analyze_memory([f"Free heap: {240000 - i * 500}" for i in range(10)], duration_s=9)
    m = leak["metrics"]["free_heap"]
    assert m["verdict"] == "shrinking" and m["leak_suspected"] and m["change"] == -4500
    assert m["bytes_per_second"] == pytest.approx(-500, abs=1)
    grow = analyze_memory([f"allocated: {1000 + i * 300}" for i in range(6)])
    assert grow["metrics"]["allocated"]["verdict"] == "growing" and grow["metrics"]["allocated"]["leak_suspected"]
    two = analyze_memory(["Free heap: 100000", "Free heap: 50000"])
    assert two["metrics"]["free_heap"]["verdict"] == "stable"  # fewer than 3 samples never trends


def test_stack_warnings_and_fragmentation():
    a = analyze_memory(ARDUINO_LINES + ["Free heap: 200000 largest: 60000"], stack_warn_bytes=600)
    by_task = {s["task"]: s for s in a["stacks"]}
    assert by_task["httpTask"]["warning"] and by_task["wifiTask"]["warning"] and not by_task["loopTask"]["warning"]
    assert a["stacks"][0]["task"] == "httpTask"
    assert a["fragmentation"]["fragmented"] and a["fragmentation"]["ratio"] == 0.3
    ok = analyze_memory(["Free heap: 100000 largest: 90000"])
    assert ok["fragmentation"]["fragmented"] is False


def _manager(chunks, monkeypatch):
    mgr = core.MonitorManager(opener=lambda port, baud: FakePort(chunks))
    monkeypatch.setattr(runtime, "monitors", mgr)
    return mgr


def test_memory_watch_one_shot(monkeypatch):
    mgr = _manager([b"Free heap: 240000 min: 230000 largest: 100000\n", b"Free heap: 236000\n", b"Free heap: 232000\nloopTask: stack hwm 300\n"], monkeypatch)
    monkeypatch.setattr(runtime, "_pick_port", lambda port, project_dir, env: ("/dev/fake", 115200))
    r = pio_memory_watch(port="/dev/fake", seconds=0.3)
    assert r["ok"] and r["port"] == "/dev/fake" and r["baud"] == 115200
    assert r["metrics"]["free_heap"]["samples"] == 3
    assert r["stacks"][0]["task"] == "loopTask" and r["stacks"][0]["warning"]
    assert "shrinking" in r["summary"] and "loopTask 300 B" in r["summary"]
    assert "instrumentation_hint" not in r
    assert mgr.list() == []  # one-shot session closed


def test_memory_watch_session_and_hint(monkeypatch):
    mgr = _manager([b"hello\nWiFi connected\n"], monkeypatch)
    s = mgr.start("/dev/fake", 115200)
    time.sleep(0.05)
    r = pio_memory_watch(session_id=s.id, seconds=0.2)
    assert r["ok"] and r["recognized"] is False
    assert "No heap or stack telemetry" in r["summary"]
    assert "ESP.getFreeHeap()" in r["instrumentation_hint"]["arduino_esp32"]
    assert r["session_id"] == s.id and r["line_count"] == 2
    assert mgr.get(s.id)  # session left open for the caller
    mgr.stop(s.id)


def test_memory_watch_custom_pattern_via_session(monkeypatch):
    mgr = _manager([b"mem=900\nmem=800\nmem=700\nmem=600\n"], monkeypatch)
    s = mgr.start("/dev/fake", 115200)
    time.sleep(0.05)
    r = pio_memory_watch(session_id=s.id, seconds=0.1, pattern=r"mem=(?P<value>\d+)")
    assert r["metrics"]["custom"]["verdict"] == "shrinking" and r["formats"] == ["custom"]
    mgr.stop(s.id)


def test_memory_watch_bad_pattern_is_structured(monkeypatch):
    _manager([b"x\n"], monkeypatch)
    monkeypatch.setattr(runtime, "_pick_port", lambda port, project_dir, env: ("/dev/fake", 115200))
    r = pio_memory_watch(port="/dev/fake", seconds=0.05, pattern="(")
    assert r["ok"] is False and r["error"] == "bad_regex"
