"""Runtime memory diagnostics from serial telemetry: heap trend, fragmentation, stack headroom."""

from __future__ import annotations

import time
from typing import Any

from ..core import guard, monitors
from ..parsers import parse_memory_telemetry
from .devices import _pick_port

# A trend only counts when the fitted change over the window beats both of these.
TREND_MIN_BYTES = 256
TREND_MIN_FRACTION = 0.01
FRAGMENTATION_RATIO = 0.5
FREE_METRICS = ("free_heap", "min_free_heap", "largest_free_block", "psram_free")

INSTRUMENTATION_HINT = {
    "arduino_esp32": 'Serial.printf("Free heap: %u min: %u largest: %u\\n", ESP.getFreeHeap(), ESP.getMinFreeHeap(), ESP.getMaxAllocHeap());\n'
    'Serial.printf("loopTask: stack hwm %u\\n", uxTaskGetStackHighWaterMark(NULL));',
    "esp_idf": "heap_caps_print_heap_info(MALLOC_CAP_DEFAULT);  // prints a 'Heap summary' block with a Totals line\n"
    'ESP_LOGI(TAG, "stack hwm %u", uxTaskGetStackHighWaterMark(NULL));',
    "freertos": 'static char buf[1024]; vTaskList(buf); Serial.println("Name State Prio Stack Num"); Serial.print(buf);  // needs configUSE_TRACE_FACILITY and configUSE_STATS_FORMATTING_FUNCTIONS',
    "custom": "Pass pattern=r'mem=(?P<value>\\d+)' (named group `value`, optional `name`) for any other line format.",
}


def _fit(values: list[int]) -> float:
    """Least-squares slope per sample (0.0 when there are fewer than two samples)."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = range(n)
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / den


def _metric_stats(name: str, values: list[int], duration_s: float | None) -> dict[str, Any]:
    slope = _fit(values)
    n = len(values)
    change = slope * (n - 1)
    mean = sum(values) / n
    threshold = max(TREND_MIN_BYTES, TREND_MIN_FRACTION * mean)
    verdict = "stable"
    if n >= 3 and abs(change) > threshold:
        verdict = "growing" if change > 0 else "shrinking"
    leak = (verdict == "shrinking" and name in FREE_METRICS) or (verdict == "growing" and name == "allocated")
    per_s = None
    if duration_s and duration_s > 0 and n >= 2:
        per_s = round(change / duration_s, 1)
    return {
        "metric": name,
        "samples": n,
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "change": round(change),
        "bytes_per_sample": round(slope, 1),
        "bytes_per_second": per_s,
        "verdict": verdict,
        "leak_suspected": leak,
    }


def analyze_memory(lines: list[str], pattern: str | None = None, stack_warn_bytes: int = 512, duration_s: float | None = None) -> dict[str, Any]:
    """Pure analysis over captured lines: per-metric trend, stack table, fragmentation hint."""
    parsed = parse_memory_telemetry(lines, pattern=pattern)
    metrics = {name: _metric_stats(name, vals, duration_s) for name, vals in parsed["series"].items()}
    stacks = []
    for task, vals in parsed["tasks"].items():
        low = min(vals)
        stacks.append({"task": task, "stack_free_bytes": low, "samples": len(vals), "last": vals[-1], "warning": low < stack_warn_bytes})
    stacks.sort(key=lambda s: s["stack_free_bytes"])
    fragmentation = None
    if "free_heap" in metrics and "largest_free_block" in metrics:
        free_last, largest_last = metrics["free_heap"]["last"], metrics["largest_free_block"]["last"]
        if free_last > 0:
            ratio = round(largest_last / free_last, 3)
            fragmentation = {"ratio": ratio, "fragmented": ratio < FRAGMENTATION_RATIO, "free_heap": free_last, "largest_free_block": largest_last}
    return {
        "recognized": parsed["recognized"],
        "formats": parsed["formats"],
        "sample_count": len(parsed["samples"]),
        "metrics": metrics,
        "stacks": stacks,
        "fragmentation": fragmentation,
        "samples": parsed["samples"][-200:],
    }


def _summary(a: dict[str, Any], lines_seen: int, source: str, stack_warn_bytes: int) -> str:
    if not a["recognized"]:
        return f"No heap or stack telemetry recognized in {lines_seen} line(s) from {source}. Add one of the instrumentation_hint snippets to the firmware (or pass a custom `pattern`) and run again."
    parts = [f"{a['sample_count']} sample(s) in {lines_seen} line(s) from {source} ({', '.join(a['formats'])})."]
    order = ["free_heap", "min_free_heap", "largest_free_block", "allocated", "psram_free"]
    for name in order + [m for m in a["metrics"] if m not in order]:
        m = a["metrics"].get(name)
        if not m:
            continue
        piece = f"{name}: {m['last']:,} B (min {m['min']:,}, max {m['max']:,}, {m['samples']} samples)"
        if m["verdict"] != "stable":
            piece += f", {m['verdict']} by {abs(m['change']):,} B over the window"
            if m["bytes_per_second"] is not None:
                piece += f" (~{m['bytes_per_second']:+,} B/s)"
            if m["leak_suspected"]:
                piece += " -> possible leak"
        parts.append(piece + ".")
    if a["fragmentation"]:
        f = a["fragmentation"]
        if f["fragmented"]:
            parts.append(f"Heap looks fragmented: the largest free block is only {int(f['ratio'] * 100)}% of free heap ({f['largest_free_block']:,} of {f['free_heap']:,} B); big allocations may fail even though heap is free.")
    if a["stacks"]:
        warned = [s for s in a["stacks"] if s["warning"]]
        if warned:
            parts.append("Stack headroom below " + f"{stack_warn_bytes} B: " + ", ".join(f"{s['task']} {s['stack_free_bytes']} B" for s in warned) + " -> raise those task stack sizes.")
        else:
            tight = a["stacks"][0]
            parts.append(f"Stack headroom OK for {len(a['stacks'])} task(s); tightest is {tight['task']} with {tight['stack_free_bytes']} B free.")
    return " ".join(parts)


def _collect(session, cursor: int, max_lines: int, deadline: float) -> tuple[list[str], int, str | None]:
    lines: list[str] = []
    error = None
    while True:
        remaining = deadline - time.monotonic()
        r = session.read(cursor=cursor, max_lines=max_lines, timeout_s=max(min(remaining, 1.0), 0))
        lines += r["lines"]
        cursor = r["cursor"]
        error = r["error"]
        if r["closed"] or (remaining <= 0 and not r["more_available"]):
            break
    return lines, cursor, error


@guard
def pio_memory_watch(
    session_id: str | None = None,
    port: str | None = None,
    baud: int | None = None,
    project_dir: str | None = None,
    env: str | None = None,
    seconds: float = 15,
    pattern: str | None = None,
    stack_warn_bytes: int = 512,
    max_lines: int = 5000,
) -> dict[str, Any]:
    seconds = min(max(seconds, 0), 300)
    t0 = time.monotonic()
    if session_id:
        s = monitors.get(session_id)
        first = s.read(cursor=0, max_lines=max_lines, timeout_s=0)
        lines = list(first["lines"])
        more, _, error = _collect(s, first["cursor"], max_lines, t0 + seconds)
        lines += more
        info = s.info()
        source, port_used, baud_used = f"session {session_id} on {s.port}", s.port, s.baud
        duration = info["uptime_s"]
    else:
        port_used, cfg_baud = _pick_port(port, project_dir, env)
        baud_used = baud or cfg_baud or 115200
        s = monitors.start(port_used, baud_used)
        try:
            lines, _, error = _collect(s, 0, max_lines, t0 + seconds)
        finally:
            monitors.stop(s.id)
        source = f"{port_used} at {baud_used} baud"
        duration = round(time.monotonic() - t0, 2)
    lines = lines[-max_lines:]
    a = analyze_memory(lines, pattern=pattern, stack_warn_bytes=stack_warn_bytes, duration_s=duration)
    summary = _summary(a, len(lines), source, stack_warn_bytes)
    if error:
        summary += f" Port error: {error}."
    result: dict[str, Any] = {
        "ok": True,
        "summary": summary,
        "port": port_used,
        "baud": baud_used,
        "session_id": session_id,
        "duration_s": duration,
        "line_count": len(lines),
        "port_error": error,
        **a,
    }
    if not a["recognized"]:
        result["instrumentation_hint"] = INSTRUMENTATION_HINT
    return result


def register(mcp) -> None:
    mcp.tool(name="pio_memory_watch", description=(
        "Watch serial output for heap and stack telemetry and diagnose leaks, fragmentation, and stack headroom. "
        "Give session_id of an open monitor session (its buffer plus `seconds` more) or a port for a one-shot capture. "
        "Understands Arduino-ESP32 'Free heap: N min: N largest: N', ESP.getFreeHeap() prints, ESP-IDF heap_caps_print_heap_info blocks, "
        "FreeRTOS vTaskList tables, and uxTaskGetStackHighWaterMark lines; `pattern` (regex with a (?P<value>) group) adds a custom metric. "
        "Returns per-metric min/max/trend with a stable/shrinking/growing verdict, a per-task stack table flagged below stack_warn_bytes, "
        "and a fragmentation hint. When the firmware prints nothing usable, instrumentation_hint has copy-paste snippets."
    ))(pio_memory_watch)
