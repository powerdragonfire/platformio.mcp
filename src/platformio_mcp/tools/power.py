"""Current/power profiling of the device under test from an external meter.

Two sources: a serial stream of current readings (an INA219/INA226 on a second board, a USB power
meter that logs over serial, ...) parsed line by line, or a Nordic Power Profiler Kit II through the
optional `ppk2-api` package. The analysis is the same for both: statistics, a bucketed timeline, a
sleep/active split, energy, and a battery-life estimate.
"""

from __future__ import annotations

import re
import statistics
import time
from collections.abc import Callable
from typing import Any, Protocol

from ..core import guard, monitors

# Value followed by an optional unit. Case-sensitive on purpose: a lowercase 'a' after a number is a word, not amps.
DEFAULT_CURRENT_RE = re.compile(r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>[uµ]A|mA|A)?(?![\w.])")
DEFAULT_VOLTAGE_RE = re.compile(r"(?P<voltage>-?\d+(?:\.\d+)?)\s*(?P<vunit>mV|V)(?![\w.])")
UNIT_TO_MA = {"uA": 0.001, "µA": 0.001, "mA": 1.0, "A": 1000.0, None: 1.0, "": 1.0}
PPK2_SAMPLE_RATE = 100_000
PPK2_WINDOW = 1_000  # 10 ms of samples
HISTOGRAM_BINS = 50
EXAMPLE_CAPACITY_MAH = 1000


class PowerSource(Protocol):
    """A meter that delivers current samples in microamps."""

    def start(self) -> None: ...
    def read(self) -> list[float]: ...
    def stop(self) -> None: ...


class Ppk2ApiMissing(RuntimeError):
    pass


def _import_ppk2():
    try:
        from ppk2_api.ppk2_api import PPK2_API
    except ImportError as exc:
        raise Ppk2ApiMissing(
            "the ppk2 source needs the optional `ppk2-api` package: run `uv pip install ppk2-api` "
            'or start the server as `uvx "platformio.mcp[power]"`.'
        ) from exc
    return PPK2_API


class Ppk2Source:
    """Nordic Power Profiler Kit II. Source-meter mode powers the DUT at voltage_mv; ampere-meter mode measures in series."""

    def __init__(self, port: str | None, voltage_mv: int | None) -> None:
        api = _import_ppk2()
        if not port:
            found = api.list_devices()
            if len(found) != 1:
                raise RuntimeError(f"{len(found)} PPK2 device(s) found ({', '.join(found)}); pass port explicitly.")
            port = found[0]
        self.port = port
        self.voltage_mv = voltage_mv
        self.dev = api(port)
        self.dev.get_modifiers()
        if voltage_mv:
            self.dev.use_source_meter()
            self.dev.set_source_voltage(int(voltage_mv))
        else:
            self.dev.use_ampere_meter()

    def start(self) -> None:
        if self.voltage_mv:
            self.dev.toggle_DUT_power("ON")
        self.dev.start_measuring()

    def read(self) -> list[float]:
        raw = self.dev.get_data()
        if not raw:
            return []
        samples, _digital = self.dev.get_samples(raw)
        return [float(s) for s in samples]

    def stop(self) -> None:
        try:
            self.dev.stop_measuring()
        finally:
            if self.voltage_mv:
                self.dev.toggle_DUT_power("OFF")


# --- parsing -------------------------------------------------------------------------------


def parse_current_line(line: str, pattern: re.Pattern[str] | None = None) -> tuple[float, float | None] | None:
    """Return (current_mA, voltage_mV or None) or None when the line carries no reading."""
    if pattern is None:
        m = DEFAULT_CURRENT_RE.search(line)
        if not m:
            return None
        ma = float(m.group("value")) * UNIT_TO_MA[m.group("unit")]
        v = DEFAULT_VOLTAGE_RE.search(line, m.end())
        mv = None
        if v:
            mv = float(v.group("voltage")) * (1000.0 if v.group("vunit") == "V" else 1.0)
        return ma, mv
    m = pattern.search(line)
    if not m:
        return None
    groups = m.groupdict()
    if "value" not in groups or groups["value"] is None:
        return None
    unit = groups.get("unit") or "mA"
    if unit not in UNIT_TO_MA:
        raise ValueError(f"unknown current unit '{unit}' captured by pattern; use uA, µA, mA, or A.")
    ma = float(groups["value"]) * UNIT_TO_MA[unit]
    mv = None
    if groups.get("voltage") is not None:
        mv = float(groups["voltage"])
        vunit = groups.get("vunit") or "V"
        mv *= 1000.0 if vunit == "V" else 1.0
    return ma, mv


def average_windows(samples_ua: list[float], window: int, carry: list[float]) -> tuple[list[float], list[float]]:
    """Average consecutive `window`-sized groups of µA samples into mA values; leftovers are returned as the new carry."""
    buf = carry + samples_ua
    out = []
    full = len(buf) // window
    for i in range(full):
        chunk = buf[i * window : (i + 1) * window]
        out.append(sum(chunk) / window / 1000.0)
    return out, buf[full * window :]


# --- analysis ------------------------------------------------------------------------------


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[rank]


def current_stats(samples_ma: list[float]) -> dict[str, float]:
    return {
        "average_ma": round(sum(samples_ma) / len(samples_ma), 4),
        "min_ma": round(min(samples_ma), 4),
        "max_ma": round(max(samples_ma), 4),
        "p95_ma": round(percentile(samples_ma, 95), 4),
    }


def auto_threshold(samples_ma: list[float], bins: int = HISTOGRAM_BINS) -> tuple[float, str]:
    """Midpoint between the two tallest histogram modes; the median when the data is unimodal."""
    lo, hi = min(samples_ma), max(samples_ma)
    median = statistics.median(samples_ma)
    if hi <= lo or len(samples_ma) < 4:
        return median, "auto_median"
    width = (hi - lo) / bins
    counts = [0] * bins
    for s in samples_ma:
        idx = min(int((s - lo) / width), bins - 1)
        counts[idx] += 1
    modes = []
    for i, c in enumerate(counts):
        left = counts[i - 1] if i > 0 else -1
        right = counts[i + 1] if i < bins - 1 else -1
        if c > 0 and c >= left and c >= right and (c > left or c > right):
            modes.append((c, i))
    modes.sort(reverse=True)
    if len(modes) < 2:
        return median, "auto_median"
    (count_a, a), (count_b, b) = modes[0], modes[1]
    first, second = sorted((a, b))
    if second - first < 3:
        return median, "auto_median"
    # A real sleep/active split has a dip between the modes and the active level is at least twice the sleep level.
    valley = min(counts[first + 1 : second])
    low_c, high_c = lo + (first + 0.5) * width, lo + (second + 0.5) * width
    if valley > min(count_a, count_b) / 2 or (low_c > 0 and high_c < 2 * low_c):
        return median, "auto_median"
    return (low_c + high_c) / 2.0, "auto_bimodal"


def sleep_active_split(samples_ma: list[float], threshold_ma: float) -> dict[str, float | None]:
    sleep = [s for s in samples_ma if s <= threshold_ma]
    active = [s for s in samples_ma if s > threshold_ma]
    return {
        "sleep_fraction": round(len(sleep) / len(samples_ma), 4),
        "sleep_avg_ma": round(sum(sleep) / len(sleep), 4) if sleep else None,
        "active_avg_ma": round(sum(active) / len(active), 4) if active else None,
    }


def timeline(samples_ma: list[float], times_s: list[float], buckets: int) -> list[dict[str, float | None]]:
    if not samples_ma or buckets <= 0:
        return []
    t0, t1 = times_s[0], times_s[-1]
    span = t1 - t0
    sums = [0.0] * buckets
    counts = [0] * buckets
    for s, t in zip(samples_ma, times_s):
        idx = min(int((t - t0) / span * buckets), buckets - 1) if span > 0 else 0
        sums[idx] += s
        counts[idx] += 1
    step = span / buckets if span > 0 else 0.0
    return [{"t_s": round(t0 + i * step, 3), "avg_ma": round(sums[i] / counts[i], 4) if counts[i] else None} for i in range(buckets)]


def energy(average_ma: float, duration_s: float, voltage_mv: float | None) -> dict[str, float | None]:
    hours = duration_s / 3600.0
    charge_uah = average_ma * hours * 1000.0
    mwh = average_ma * (voltage_mv / 1000.0) * hours if voltage_mv else None
    return {"charge_uah": round(charge_uah, 3), "energy_mwh": round(mwh, 4) if mwh is not None else None}


def hours_on_mah(average_ma: float, capacity_mah: float) -> float | None:
    return round(capacity_mah / average_ma, 1) if average_ma > 0 else None


def analyze(samples_ma: list[float], times_s: list[float], voltage_mv: float | None, buckets: int, sleep_threshold_ma: float | None) -> dict[str, Any]:
    stats = current_stats(samples_ma)
    duration = times_s[-1] - times_s[0] if len(times_s) > 1 else 0.0
    if sleep_threshold_ma is None:
        threshold, threshold_source = auto_threshold(samples_ma)
    else:
        threshold, threshold_source = float(sleep_threshold_ma), "argument"
    split = sleep_active_split(samples_ma, threshold)
    return {
        **stats,
        "sample_count": len(samples_ma),
        "duration_s": round(duration, 3),
        "voltage_mv": voltage_mv,
        **energy(stats["average_ma"], duration, voltage_mv),
        "threshold_ma": round(threshold, 4),
        "threshold_source": threshold_source,
        **split,
        "timeline": timeline(samples_ma, times_s, buckets),
        "battery_1000mah_hours": hours_on_mah(stats["average_ma"], EXAMPLE_CAPACITY_MAH),
    }


def describe(a: dict[str, Any], source: str) -> str:
    parts = [f"{a['sample_count']} sample(s) over {a['duration_s']}s from {source}: average {a['average_ma']} mA (min {a['min_ma']}, max {a['max_ma']}, p95 {a['p95_ma']})."]
    if a["sleep_avg_ma"] is not None and a["active_avg_ma"] is not None:
        parts.append(f"{round(a['sleep_fraction'] * 100)}% of the time in sleep at {a['sleep_avg_ma']} mA, active phases average {a['active_avg_ma']} mA (threshold {a['threshold_ma']} mA, {a['threshold_source'].replace('_', ' ')}).")
    else:
        parts.append(f"No sleep/active split: every sample is on one side of the {a['threshold_ma']} mA threshold.")
    if a["energy_mwh"] is not None:
        parts.append(f"{a['energy_mwh']} mWh ({a['charge_uah']} µAh) consumed at {a['voltage_mv']} mV.")
    else:
        parts.append(f"{a['charge_uah']} µAh consumed (pass voltage_mv for energy in mWh).")
    if a["battery_1000mah_hours"]:
        parts.append(f"A {EXAMPLE_CAPACITY_MAH} mAh cell lasts ~{a['battery_1000mah_hours']} h at this average.")
    return " ".join(parts)


# --- collection ----------------------------------------------------------------------------


def _collect_serial(port: str, baud: int, seconds: float, pattern: re.Pattern[str] | None) -> tuple[list[float], list[float], list[float | None], int]:
    s = monitors.start(port, baud)
    samples: list[float] = []
    times: list[float] = []
    volts: list[float | None] = []
    unparsed = 0
    cursor = 0
    t0 = time.monotonic()
    deadline = t0 + seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r = s.read(cursor=cursor, max_lines=1000, timeout_s=min(0.25, remaining))
            cursor = r["cursor"]
            now = time.monotonic() - t0
            for line in r["lines"]:
                parsed = parse_current_line(line, pattern)
                if parsed is None:
                    if line.strip():
                        unparsed += 1
                    continue
                samples.append(parsed[0])
                volts.append(parsed[1])
                times.append(now)
            if r["closed"]:
                if r["error"]:
                    raise RuntimeError(f"port {port} closed while measuring: {r['error']}")
                break
    finally:
        monitors.stop(s.id)
    return samples, times, volts, unparsed


def _collect_source(source: PowerSource, seconds: float, window: int = PPK2_WINDOW, sample_rate: int = PPK2_SAMPLE_RATE, sleep: Callable[[float], None] = time.sleep) -> tuple[list[float], list[float]]:
    samples: list[float] = []
    carry: list[float] = []
    source.start()
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            raw = source.read()
            if raw:
                out, carry = average_windows(raw, window, carry)
                samples += out
            else:
                sleep(0.01)
    finally:
        source.stop()
    step = window / sample_rate
    return samples, [i * step for i in range(len(samples))]


def _wait_for_trigger(session_id: str, trigger: str, timeout_s: float) -> dict[str, Any]:
    s = monitors.get(session_id)
    start = s.info()["next_cursor"]
    t0 = time.monotonic()
    r = s.read(cursor=start, max_lines=1000, wait_for=trigger, timeout_s=timeout_s)
    waited = round(time.monotonic() - t0, 3)
    if not r["matched"]:
        raise RuntimeError(f"trigger '{trigger}' did not appear on session {session_id} within {timeout_s}s; nothing measured.")
    return {"trigger_line": r["lines"][-1], "trigger_offset_s": waited}


@guard
def pio_power_profile(
    source: str = "serial",
    seconds: float = 10,
    port: str | None = None,
    baud: int = 115200,
    pattern: str | None = None,
    voltage_mv: float | None = None,
    trigger: str | None = None,
    trigger_session_id: str | None = None,
    buckets: int = 20,
    sleep_threshold_ma: float | None = None,
) -> dict[str, Any]:
    if source not in ("serial", "ppk2"):
        raise ValueError("source must be 'serial' (a meter streaming readings over a serial port) or 'ppk2' (Nordic Power Profiler Kit II).")
    if seconds <= 0:
        raise ValueError("seconds must be positive.")
    seconds = min(float(seconds), 600.0)
    rx = re.compile(pattern) if pattern else None
    trig: dict[str, Any] = {}
    if trigger:
        if not trigger_session_id:
            raise ValueError("trigger needs trigger_session_id: an open pio_monitor_start session on the firmware's own serial port.")
        trig = _wait_for_trigger(trigger_session_id, trigger, seconds)
    unparsed = 0
    volts: list[float | None] = []
    if source == "serial":
        if not port:
            raise ValueError("port is required for the serial source: the meter's serial port (run pio_list_devices).")
        samples, times, volts, unparsed = _collect_serial(port, baud, seconds, rx)
    else:
        try:
            meter = Ppk2Source(port, int(voltage_mv) if voltage_mv else None)
        except Ppk2ApiMissing as exc:
            return {"ok": False, "error": "ppk2_api_missing", "summary": str(exc)}
        port = meter.port
        samples, times = _collect_source(meter, seconds)
    if not samples:
        hint = f" {unparsed} line(s) arrived but none matched the current pattern; pass `pattern` with a (?P<value>...) group." if unparsed else " No data arrived: check port, baud, and that the meter is streaming."
        return {"ok": False, "error": "no_samples", "summary": f"no current readings captured from {port} in {seconds}s." + hint, "port": port, "unparsed_lines": unparsed, **trig}
    measured_v = [v for v in volts if v is not None]
    v_mv = float(voltage_mv) if voltage_mv else (round(sum(measured_v) / len(measured_v), 1) if measured_v else None)
    a = analyze(samples, times, v_mv, buckets, sleep_threshold_ma)
    return {
        "ok": True,
        "summary": describe(a, f"{source} on {port}") + (f" Measured after trigger '{trig['trigger_line']}' ({trig['trigger_offset_s']}s in)." if trig else ""),
        "source": source,
        "port": port,
        "seconds": seconds,
        "unparsed_lines": unparsed,
        **a,
        **trig,
    }


def register(mcp) -> None:
    mcp.tool(name="pio_power_profile", description=(
        "Measure the device's current draw for `seconds` and report average/min/max/p95 mA, energy (mWh and µAh when voltage is known), "
        "a bucketed timeline, the share of time in sleep vs active (threshold auto-detected from the two dominant current levels, or `sleep_threshold_ma`), "
        "and a battery-life estimate. source='serial' reads a meter streaming readings on `port` (INA219/INA226 sketch, USB power meter log; "
        "`pattern` regex with (?P<value>) and optional (?P<unit>)/(?P<voltage>) groups, default: first number with uA/mA/A). source='ppk2' drives a "
        "Nordic Power Profiler Kit II (needs the `power` extra). Set `trigger` + `trigger_session_id` to start measuring when the firmware prints a line."
    ))(pio_power_profile)
