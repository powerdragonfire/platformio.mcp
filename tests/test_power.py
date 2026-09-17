"""pio_power_profile with the meter faked out: serial line parsing, statistics, PPK2 source, trigger."""

import re
import time
from typing import ClassVar

import pytest
from test_monitor import FakePort

from platformio_mcp.monitor import MonitorManager
from platformio_mcp.tools import power

# --- parsing -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("12.5 mA", (12.5, None)),
        ("current: 850 uA", (0.85, None)),
        ("I=1.2A", (1200.0, None)),
        ("42", (42.0, None)),
        ("12.3,4.98", (12.3, None)),
        ("I: 33 mA V: 3.30 V", (33.0, 3300.0)),
        ("cur 7.5mA bus 3298 mV", (7.5, 3298.0)),
        ("bought 12 apples", (12.0, None)),
        ("boot ok", None),
        ("", None),
    ],
)
def test_default_pattern(line, expected):
    assert power.parse_current_line(line) == expected


def test_custom_pattern_with_units_and_voltage():
    rx = re.compile(r"cur=(?P<value>[\d.]+)(?P<unit>uA|mA),v=(?P<voltage>[\d.]+)(?P<vunit>V|mV)")
    assert power.parse_current_line("cur=500uA,v=3.3V", rx) == (0.5, 3300.0)
    assert power.parse_current_line("cur=2.5mA,v=4200mV", rx) == (2.5, 4200.0)
    assert power.parse_current_line("nothing", rx) is None
    with pytest.raises(ValueError):
        power.parse_current_line("x=1kA", re.compile(r"x=(?P<value>\d+)(?P<unit>kA)"))


def test_average_windows_carries_leftovers():
    out, carry = power.average_windows([1000.0] * 5, 4, [])
    assert out == [1.0] and carry == [1000.0]
    out, carry = power.average_windows([2000.0, 2000.0, 2000.0], 4, carry)
    assert out == [1.75] and carry == []


# --- analysis ------------------------------------------------------------------------------


def bimodal(n_sleep=600, n_active=400, sleep=0.8, active=80.0):
    samples = [sleep + (i % 5) * 0.01 for i in range(n_sleep)] + [active + (i % 7) * 0.5 for i in range(n_active)]
    times = [i * 0.01 for i in range(len(samples))]
    return samples, times


def test_auto_threshold_finds_two_modes():
    samples, _ = bimodal()
    thr, how = power.auto_threshold(samples)
    assert how == "auto_bimodal" and 5 < thr < 75


def test_auto_threshold_falls_back_to_median_when_unimodal():
    flat = [10.0 + (i % 3) * 0.001 for i in range(100)]
    thr, how = power.auto_threshold(flat)
    assert how == "auto_median" and abs(thr - 10.001) < 0.01
    thr, how = power.auto_threshold([5.0, 5.0, 5.0, 5.0])
    assert how == "auto_median" and thr == 5.0


def test_analyze_statistics_energy_and_battery():
    samples, times = bimodal()
    a = power.analyze(samples, times, voltage_mv=3300, buckets=10, sleep_threshold_ma=None)
    assert a["sample_count"] == 1000 and a["duration_s"] == 9.99
    assert a["min_ma"] == 0.8 and a["max_ma"] == 83.0
    assert 32 < a["average_ma"] < 34
    assert a["p95_ma"] >= 80
    assert a["sleep_fraction"] == 0.6 and abs(a["sleep_avg_ma"] - 0.82) < 0.01 and 80 < a["active_avg_ma"] < 82
    assert a["threshold_source"] == "auto_bimodal"
    assert len(a["timeline"]) == 10 and a["timeline"][0]["avg_ma"] < 1 and a["timeline"][-1]["avg_ma"] > 79
    assert a["energy_mwh"] == pytest.approx(a["average_ma"] * 3.3 * 9.99 / 3600, rel=1e-3)
    assert a["charge_uah"] == pytest.approx(a["average_ma"] * 9.99 / 3600 * 1000, rel=1e-3)
    assert a["battery_1000mah_hours"] == pytest.approx(1000 / a["average_ma"], rel=1e-2)
    text = power.describe(a, "test")
    assert "60% of the time in sleep" in text and "1000 mAh cell lasts" in text


def test_explicit_threshold_and_no_voltage():
    a = power.analyze([1.0, 2.0, 30.0, 40.0], [0, 1, 2, 3], voltage_mv=None, buckets=2, sleep_threshold_ma=10)
    assert a["threshold_source"] == "argument" and a["sleep_fraction"] == 0.5
    assert a["energy_mwh"] is None and a["charge_uah"] > 0
    assert "pass voltage_mv" in power.describe(a, "x")


def test_hours_on_mah_zero_current():
    assert power.hours_on_mah(0.0, 1000) is None
    assert power.hours_on_mah(50.0, 1000) == 20.0


# --- serial source -------------------------------------------------------------------------


@pytest.fixture
def fake_monitors(monkeypatch):
    holder = {}

    def install(chunks):
        mgr = MonitorManager(opener=lambda port, baud: FakePort(chunks, delay=0.001))
        monkeypatch.setattr(power, "monitors", mgr)
        holder["mgr"] = mgr
        return mgr

    return install


def test_serial_profile(fake_monitors):
    lines = b"".join(f"{0.9 if i % 2 else 60.0} mA, 3.30 V\n".encode() for i in range(40)) + b"noise line\n"
    mgr = fake_monitors([lines])
    r = power.pio_power_profile(source="serial", seconds=0.3, port="/dev/meter", buckets=4)
    assert r["ok"] is True, r
    assert r["sample_count"] == 40 and r["unparsed_lines"] == 1
    assert r["voltage_mv"] == 3300.0 and r["energy_mwh"] is not None
    assert r["sleep_fraction"] == 0.5 and r["threshold_source"] == "auto_bimodal"
    assert len(r["timeline"]) == 4
    assert mgr.list() == []  # session closed afterwards


def test_serial_requires_port_and_reports_no_samples(fake_monitors):
    r = power.pio_power_profile(source="serial", seconds=0.1)
    assert r["ok"] is False and "port is required" in r["summary"]
    fake_monitors([b"hello\nworld\n"])
    r = power.pio_power_profile(source="serial", seconds=0.1, port="/dev/meter", pattern=r"cur=(?P<value>\d+)")
    assert r["ok"] is False and r["error"] == "no_samples" and r["unparsed_lines"] == 2


def test_bad_source_and_seconds():
    assert power.pio_power_profile(source="usb")["ok"] is False
    assert power.pio_power_profile(seconds=0, port="/dev/x")["ok"] is False


# --- trigger -------------------------------------------------------------------------------


def test_trigger_waits_for_firmware_line(monkeypatch):
    firmware = FakePort([b"booting\n", b"radio on\n"], delay=0.02)
    meter = FakePort([b"5 mA\n" * 10], delay=0.001)
    mgr = MonitorManager(opener=lambda port, baud: firmware if port == "/dev/fw" else meter)
    monkeypatch.setattr(power, "monitors", mgr)
    fw = mgr.start("/dev/fw", 115200)
    r = power.pio_power_profile(source="serial", seconds=0.3, port="/dev/meter", trigger="radio on", trigger_session_id=fw.id)
    assert r["ok"] is True and r["trigger_line"] == "radio on" and r["trigger_offset_s"] >= 0
    assert "after trigger 'radio on'" in r["summary"]
    mgr.stop(fw.id)


def test_trigger_timeout_and_missing_session(monkeypatch):
    mgr = MonitorManager(opener=lambda port, baud: FakePort([b"quiet\n"]))
    monkeypatch.setattr(power, "monitors", mgr)
    fw = mgr.start("/dev/fw", 115200)
    r = power.pio_power_profile(source="serial", seconds=0.1, port="/dev/meter", trigger="never", trigger_session_id=fw.id)
    assert r["ok"] is False and "did not appear" in r["summary"]
    r = power.pio_power_profile(source="serial", seconds=0.1, port="/dev/meter", trigger="x")
    assert r["ok"] is False and "trigger_session_id" in r["summary"]
    mgr.stop(fw.id)


# --- ppk2 source ---------------------------------------------------------------------------


class FakePPK2:
    """Mimics ppk2_api.PPK2_API closely enough to exercise Ppk2Source."""

    devices: ClassVar[list[str]] = ["/dev/ppk2"]
    calls: ClassVar[list] = []

    def __init__(self, port):
        self.port = port
        self.chunks = [b"a", b"", b"b"]
        FakePPK2.calls = []

    @classmethod
    def list_devices(cls):
        return cls.devices

    def get_modifiers(self):
        FakePPK2.calls.append("modifiers")

    def use_source_meter(self):
        FakePPK2.calls.append("source_meter")

    def use_ampere_meter(self):
        FakePPK2.calls.append("ampere_meter")

    def set_source_voltage(self, mv):
        FakePPK2.calls.append(("voltage", mv))

    def toggle_DUT_power(self, state):
        FakePPK2.calls.append(("power", state))

    def start_measuring(self):
        FakePPK2.calls.append("start")

    def stop_measuring(self):
        FakePPK2.calls.append("stop")

    def get_data(self):
        return self.chunks.pop(0) if self.chunks else b""

    def get_samples(self, raw):
        return ([800.0] * 2000 if raw == b"a" else [60000.0] * 2000), []


def test_ppk2_source_meter_flow(monkeypatch):
    monkeypatch.setattr(power, "_import_ppk2", lambda: FakePPK2)
    r = power.pio_power_profile(source="ppk2", seconds=0.2, voltage_mv=3300)
    assert r["ok"] is True, r
    assert r["port"] == "/dev/ppk2" and r["sample_count"] == 4
    assert r["min_ma"] == 0.8 and r["max_ma"] == 60.0 and r["voltage_mv"] == 3300.0
    assert FakePPK2.calls[:3] == ["modifiers", "source_meter", ("voltage", 3300)]
    assert ("power", "ON") in FakePPK2.calls and FakePPK2.calls[-1] == ("power", "OFF") and "stop" in FakePPK2.calls


def test_ppk2_ampere_meter_and_explicit_port(monkeypatch):
    monkeypatch.setattr(power, "_import_ppk2", lambda: FakePPK2)
    r = power.pio_power_profile(source="ppk2", seconds=0.2, port="/dev/other")
    assert r["ok"] and r["port"] == "/dev/other" and "ampere_meter" in FakePPK2.calls and ("power", "ON") not in FakePPK2.calls


def test_ppk2_ambiguous_devices(monkeypatch):
    monkeypatch.setattr(power, "_import_ppk2", lambda: FakePPK2)
    monkeypatch.setattr(FakePPK2, "devices", [])
    r = power.pio_power_profile(source="ppk2", seconds=0.1)
    assert r["ok"] is False and "0 PPK2 device(s)" in r["summary"]


def test_ppk2_missing_package(monkeypatch):
    real_import = __import__

    def no_ppk2(name, *args, **kwargs):
        if name.startswith("ppk2_api"):
            raise ImportError("No module named 'ppk2_api'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_ppk2)
    r = power.pio_power_profile(source="ppk2", seconds=0.1)
    assert r["ok"] is False and r["error"] == "ppk2_api_missing" and "platformio.mcp[power]" in r["summary"]


def test_collect_source_times_use_window_and_rate():
    class Src:
        def __init__(self):
            self.n = 0

        def start(self):
            pass

        def read(self):
            self.n += 1
            return [1000.0] * 300 if self.n <= 3 else []

        def stop(self):
            pass

    t0 = time.monotonic()
    samples, times = power._collect_source(Src(), 0.05, window=100, sample_rate=1000, sleep=lambda s: None)
    assert samples == [1.0] * 9 and times[1] == pytest.approx(0.1)
    assert time.monotonic() - t0 < 1
