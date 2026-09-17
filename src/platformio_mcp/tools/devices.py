"""Serial device discovery and monitor sessions."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any

from ..core import (
    check_policy,
    default_envs,
    env_setting,
    guard,
    monitors,
    project_config,
    run_json,
)

DEV_BOARD_RE = re.compile(r"CP210|CH34|CH9102|FTDI|FT23|Silicon Labs|SLAB|usbserial|usbmodem|wchusbserial|ttyUSB|ttyACM|ESP|Arduino|STLink|ST-Link|JLink|J-Link|CMSIS|DAPLink|Espressif|USB", re.I)
NOISE_PORT_RE = re.compile(r"Bluetooth|debug-console|Jabra|AirPods|iPhone", re.I)


def _devices() -> list[dict[str, Any]]:
    _, data = run_json(["device", "list", "--json-output"], tool="device-list", timeout=60)
    rows = []
    for d in data:
        text = f"{d.get('description', '')} {d.get('hwid', '')} {d.get('port', '')}"
        likely = bool(DEV_BOARD_RE.search(text)) and not NOISE_PORT_RE.search(text)
        rows.append({"port": d.get("port"), "description": d.get("description"), "hwid": d.get("hwid"), "likely_dev_board": likely})
    rows.sort(key=lambda r: not r["likely_dev_board"])
    return rows


@guard
def pio_list_devices() -> dict[str, Any]:
    rows = _devices()
    likely = [r["port"] for r in rows if r["likely_dev_board"]]
    summary = f"{len(rows)} serial port(s). " + (f"Likely dev boards: {', '.join(likely)}." if likely else "No port looks like a USB dev board; check the cable (data, not charge-only) and drivers.")
    return {"ok": True, "summary": summary, "devices": rows, "likely_ports": likely, "open_monitor_sessions": monitors.list()}


def _pick_port(port: str | None, project_dir: str | None, env: str | None) -> tuple[str, int | None]:
    """Resolve port and baud from arguments, then platformio.ini, then auto-detection."""
    baud = None
    if project_dir:
        cfg = project_config(project_dir)
        env = env or (default_envs(cfg) or [None])[0]
        if env:
            port = port or env_setting(cfg, env, "monitor_port") or env_setting(cfg, env, "upload_port")
            speed = env_setting(cfg, env, "monitor_speed")
            baud = int(speed) if speed else None
    if not port:
        likely = [r["port"] for r in _devices() if r["likely_dev_board"]]
        if len(likely) == 1:
            port = likely[0]
        elif not likely:
            raise RuntimeError("no serial port looks like a dev board; run pio_list_devices and pass port explicitly.")
        else:
            raise RuntimeError(f"several candidate ports: {', '.join(likely)}. Pass port explicitly.")
    return port, baud


def _port_holders(port: str) -> tuple[list[dict[str, Any]], str]:
    """Other processes with the port open, via lsof then fuser. Windows has no cheap equivalent."""
    if sys.platform == "win32":
        return [], "unavailable"
    pids: list[int] = []
    method = "unavailable"
    for cmd in (["lsof", "-t", port], ["fuser", port]):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        method = cmd[0]
        pids = [int(t) for t in re.findall(r"\d+", res.stdout)]
        break
    holders: list[dict[str, Any]] = []
    for pid in sorted(set(pids)):
        if pid == os.getpid():
            continue
        command = None
        try:
            ps = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
            command = ps.stdout.strip() or None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        holders.append({"pid": pid, "command": command})
    return holders, method


def _port_hint(diag: dict[str, Any], error: str | None = None) -> str:
    port = diag["port"]
    if diag["held_by_session"]:
        return f"port {port} is held by our monitor session {diag['held_by_session']}; pass stop_open_sessions=true to pio_upload or call pio_monitor_stop('{diag['held_by_session']}') first."
    if diag["held_by_processes"]:
        who = ", ".join(f"PID {h['pid']}" + (f" ({h['command']})" if h["command"] else "") for h in diag["held_by_processes"])
        return f"port {port} is open in another process: {who}. Close that program (serial monitor, screen, minicom, the IDE) and retry."
    if diag["exists"] is False:
        return f"{port} does not exist. Run pio_list_devices; check the USB cable carries data (not charge-only), the driver (CP210x/CH340) is installed, and the board is powered."
    perm = diag.get("permission") or {}
    if perm and not (perm.get("readable") and perm.get("writable")):
        if sys.platform.startswith("linux"):
            return f"no read/write permission on {port}. Add yourself to the serial group (`sudo usermod -aG dialout $USER`, or `uucp` on Arch) and log in again, or `sudo chmod a+rw {port}` for this session."
        return f"no read/write permission on {port}; check ownership and permissions of the device node."
    if error == "no_response":
        return f"{port} opened but the board did not answer. Put it in bootloader mode (hold BOOT, tap EN/RST, release BOOT), try a lower upload_speed, or check that {port} is the board's programming port and not a second UART."
    if error == "port_busy":
        return f"{port} reports busy but no holder was found (process check: {diag['process_check']}). On Windows close any serial monitor or IDE; otherwise unplug and replug the board."
    if error == "port_permission":
        return f"{port} refused access. On Windows close the program using it; on Linux check the dialout/uucp group."
    return f"{port} looks free: it exists, no session or process holds it, and permissions allow read/write."


def diagnose_port(port: str, error: str | None = None) -> dict[str, Any]:
    """Explain why a serial port cannot be used. Reports only; never kills anything."""
    held = monitors.session_on_port(port)
    try:
        listed = [d["port"] for d in _devices()]
        in_list: bool | None = port in listed
    except Exception:
        in_list = None
    if sys.platform == "win32":
        exists = in_list
        permission = None
    else:
        exists = os.path.exists(port)
        permission = {"readable": os.access(port, os.R_OK), "writable": os.access(port, os.W_OK)} if exists else None
    holders, method = _port_holders(port) if exists else ([], "skipped")
    diag: dict[str, Any] = {
        "port": port,
        "exists": exists,
        "in_device_list": in_list,
        "held_by_session": held.id if held else None,
        "held_by_processes": holders,
        "process_check": method,
        "permission": permission,
        "platform": sys.platform,
    }
    diag["hint"] = _port_hint(diag, error)
    return diag


@guard
def pio_port_diagnose(port: str | None = None, project_dir: str | None = None, env: str | None = None) -> dict[str, Any]:
    port, _ = _pick_port(port, project_dir, env)
    diag = diagnose_port(port)
    return {"ok": True, "summary": diag["hint"], **diag}


@guard
def pio_monitor_start(port: str | None = None, baud: int | None = None, project_dir: str | None = None, env: str | None = None, max_lines: int = 5000) -> dict[str, Any]:
    port, cfg_baud = _pick_port(port, project_dir, env)
    baud = baud or cfg_baud or 115200
    monitors._max_lines = max_lines
    s = monitors.start(port, baud)
    return {"ok": True, "summary": f"Monitoring {port} at {baud} baud in session {s.id}. Read with pio_monitor_read(session_id='{s.id}', cursor=0). Stop before flashing.", **s.info()}


@guard
def pio_monitor_read(session_id: str, cursor: int = 0, max_lines: int = 200, wait_for: str | None = None, timeout_s: float = 10) -> dict[str, Any]:
    s = monitors.get(session_id)
    r = s.read(cursor=cursor, max_lines=max_lines, wait_for=wait_for, timeout_s=min(timeout_s, 120))
    n = len(r["lines"])
    if wait_for:
        r["summary"] = f"pattern {'matched' if r['matched'] else 'not matched within ' + str(timeout_s) + 's'}; {n} new line(s). Next cursor {r['cursor']}."
    else:
        r["summary"] = f"{n} new line(s); next cursor {r['cursor']}." + (" More available, read again." if r["more_available"] else "")
    if r["dropped_before_cursor"]:
        r["summary"] += f" {r['dropped_before_cursor']} older line(s) fell out of the buffer."
    if r["closed"]:
        r["summary"] += f" Session closed{': ' + r['error'] if r['error'] else ''}."
    r["ok"] = True
    return r


@guard
def pio_monitor_write(session_id: str, text: str, newline: bool = True) -> dict[str, Any]:
    check_policy("flash")
    s = monitors.get(session_id)
    n = s.write(text, newline=newline)
    return {"ok": True, "summary": f"sent {n} byte(s) to {s.port}.", "session_id": session_id, "bytes": n}


@guard
def pio_monitor_stop(session_id: str) -> dict[str, Any]:
    info = monitors.stop(session_id)
    return {"ok": True, "summary": f"session {session_id} on {info['port']} closed after {info['uptime_s']}s and {info['bytes_received']} bytes.", **info}


@guard
def pio_monitor_list() -> dict[str, Any]:
    rows = monitors.list()
    return {"ok": True, "summary": f"{len(rows)} open session(s).", "sessions": rows}


@guard
def pio_monitor_capture(port: str | None = None, baud: int | None = None, project_dir: str | None = None, env: str | None = None, seconds: float = 5, until: str | None = None, max_lines: int = 500) -> dict[str, Any]:
    port, cfg_baud = _pick_port(port, project_dir, env)
    baud = baud or cfg_baud or 115200
    s = monitors.start(port, baud)
    try:
        r = s.read(cursor=0, max_lines=max_lines, wait_for=until, timeout_s=min(seconds, 120))
    finally:
        monitors.stop(s.id)
    lines = r["lines"]
    summary = f"captured {len(lines)} line(s) from {port} at {baud} baud over up to {seconds}s"
    if until:
        summary += f"; pattern {'matched' if r['matched'] else 'not seen'}"
    if r["error"]:
        summary += f"; port error: {r['error']}"
    return {"ok": True, "summary": summary + ".", "port": port, "baud": baud, "lines": lines, "matched": r["matched"], "partial_line": r["partial_line"], "error": r["error"]}


def register(mcp) -> None:
    mcp.tool(name="pio_list_devices", description=(
        "List serial ports (`pio device list`) and flag which ones look like USB dev boards (CP210x, CH340, FTDI, ESP, Arduino, ST-Link...). "
        "Use the port with pio_upload, pio_monitor_start, or pio_monitor_capture."
    ))(pio_list_devices)
    mcp.tool(name="pio_port_diagnose", description=(
        "Explain why a serial port is unusable before or after a failed upload: whether it exists, whether one of our monitor sessions "
        "holds it, which other process has it open (lsof/fuser on macOS and Linux), and read/write permission, plus the concrete fix. "
        "Port defaults from platformio.ini or the single detected board. Reports only; it never closes anything."
    ))(pio_port_diagnose)
    mcp.tool(name="pio_monitor_start", description=(
        "Open a background serial monitor session and return a session_id. Port and baud default from platformio.ini "
        "(monitor_port/monitor_speed) when project_dir is given, else the single detected dev board and 115200. "
        "Output is buffered (ring buffer, max_lines); read it with pio_monitor_read. The session holds the port, so stop it before pio_upload."
    ))(pio_monitor_start)
    mcp.tool(name="pio_monitor_read", description=(
        "Read new serial lines from a session since `cursor` (start at 0) and get the next cursor back. "
        "Set wait_for to a regex to block up to timeout_s until a matching line arrives (e.g. wait_for='setup done|Guru Meditation'). "
        "Without wait_for and with timeout_s>0 it waits for at least one new line."
    ))(pio_monitor_read)
    mcp.tool(name="pio_monitor_write", description="Send text to the device over the open session's serial port (newline appended by default). Blocked under build_only/read_only policy.")(pio_monitor_write)
    mcp.tool(name="pio_monitor_stop", description="Close a serial monitor session and release the port.")(pio_monitor_stop)
    mcp.tool(name="pio_monitor_list", description="List open serial monitor sessions with port, baud, buffered line count, and next cursor.")(pio_monitor_list)
    mcp.tool(name="pio_monitor_capture", description=(
        "One-shot serial capture with no session to manage: open the port, collect output for up to `seconds` "
        "(or until regex `until` matches), close the port, and return the lines. Ideal right after pio_upload to grab the boot log."
    ))(pio_monitor_capture)
