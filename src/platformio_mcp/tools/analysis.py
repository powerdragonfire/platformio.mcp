"""Firmware analysis: crash decoding, size budgeting, and flash-then-verify on hardware."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ..core import (
    check_policy,
    default_envs,
    env_setting,
    guard,
    monitors,
    project_config,
)
from ..parsers import parse_memory
from ..pio import DEFAULT_TIMEOUTS, resolve_project_dir, run_pio
from ..toolchain import (
    Address,
    Toolchain,
    extract_crash,
    find_gdb,
    group_by_file,
    load_toolchain,
    parse_nm,
    parse_size_sections,
    parse_size_totals,
    require_elf,
    run_tool,
    symbolize,
)
from ..unwind import PanicBlock, arch_for_prefix, arch_for_text, code_ranges, extract_panic_block, stack_scan, unwind_with_gdb
from .build import pio_upload
from .devices import _pick_port
from .project import _all_boards

DEFAULT_FAIL_PATTERN = (
    r"Guru Meditation|panic'ed|abort\(\) was called|assert failed|HardFault|Hard Fault|BusFault|UsageFault|MemManage|"
    r"stack overflow|Task watchdog|Brownout|CORRUPT HEAP|Backtrace:|rst:0x[0-9a-f]+ \((?:SW_CPU_RESET|TG\dWDT_SYS_RESET|RTCWDT_RTC_RESET|PANIC)"
)


def _board_limits(cfg: dict, env: str) -> dict[str, Any]:
    board = env_setting(cfg, env, "board")
    if not board:
        return {}
    try:
        for b in _all_boards():
            if b.get("id") == board:
                return {"board": board, "flash_bytes": b.get("rom"), "ram_bytes": b.get("ram"), "mcu": b.get("mcu")}
    except Exception:
        pass
    return {"board": board}


def _pio_memory(path: str, env: str) -> tuple[dict, str | None]:
    """PlatformIO's own RAM/Flash accounting (`pio run -t checkprogsize`), which knows partition tables and IRAM rules."""
    try:
        res = run_pio(["run", "-d", path, "-e", env, "-t", "checkprogsize"], cwd=path, timeout=DEFAULT_TIMEOUTS["build"], tool="checkprogsize")
    except Exception:
        return {}, None
    return (parse_memory(res.output) if res.ok else {}), res.log_path


GDB_INSTALL_HINT = "Install GDB with `pio pkg install -g -t platformio/tool-riscv32-esp-elf-gdb` (or start one `pio debug` session, which installs it), then decode again for a full call chain."


def _elf_note(elf) -> str:
    return f"ELF: {elf} (modified {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(elf.stat().st_mtime))})."


def _chain(frames: list[dict]) -> str:
    return " <- ".join(f"{f['function']} ({f['file'].split('/')[-1]}:{f['line']})" for f in frames[:6])


def _decode_riscv(tc: Toolchain, elf, crash: dict, block: PanicBlock) -> dict[str, Any]:
    """RISC-V: unwind the stack dump with GDB; without GDB, symbolize stack words that point into code."""
    gdb = find_gdb(tc.cc_path)
    unwind_error = None
    frames: list[dict] = []
    stopped = None
    method = None
    if gdb:
        try:
            r = unwind_with_gdb(gdb, str(elf), block)
            stopped = r["stopped"]
            for f in r["frames"]:
                address = f["address"] or (f"0x{block.regs['MEPC']:08x}" if f["frame"] == 0 else None)
                frames.append({"address": address, "role": "frame", "frame": f["frame"], "register": "MEPC" if f["frame"] == 0 else None, "function": f["function"], "file": f["file"], "line": f["line"], "resolved": f["resolved"], "inlined": []})
            method = "gdb_unwind"
        except (RuntimeError, OSError) as exc:
            unwind_error = str(exc)
    if method is None:
        addrs = [Address(f"0x{block.regs['MEPC']:08x}", "pc", register="MEPC")]
        if "RA" in block.regs:
            addrs.append(Address(f"0x{block.regs['RA']:08x}", "lr", register="RA"))
        known = {a.address for a in addrs}
        scanned = [a for a in stack_scan(block, code_ranges(str(elf))) if a not in known]
        addrs += [Address(a, "stack_scan", frame=i) for i, a in enumerate(scanned)]
        resolved = symbolize(tc, [a.address for a in addrs])
        for a in addrs:
            hit = resolved.get(a.address, {})
            frames.append({**a.to_dict(), "function": hit.get("function"), "file": hit.get("file"), "line": hit.get("line"), "resolved": hit.get("resolved", False), "inlined": hit.get("inlined", [])})
        method = "stack_scan"
    hit = [f for f in frames if f["resolved"]]
    corrupted = bool(stopped and "corrupt" in stopped.lower())
    parts = []
    if crash["causes"]:
        parts.append("Cause: " + crash["causes"][0].rstrip(".") + ".")
    if len(block.cores_seen) > 1:
        parts.append(f"Register dumps for cores {block.cores_seen}; decoded core {block.core}, the one that panicked.")
    if method == "gdb_unwind":
        if hit:
            parts.append(f"GDB unwound {len(frames)} frame(s), {len(hit)} with source: {_chain(hit)}.")
        else:
            parts.append(f"GDB unwound {len(frames)} frame(s) but none map to source in {elf.name}; the ELF probably does not match the flashed firmware.")
        if stopped:
            parts.append(f"GDB stopped: {stopped}.")
    else:
        why = f"GDB failed ({unwind_error})" if unwind_error else f"no GDB found for {Path(tc.prefix).name}"
        parts.append(f"RISC-V stack not unwound: {why}. Symbolized MEPC, RA and {sum(f['role'] == 'stack_scan' for f in frames)} stack word(s) that point into code; stack_scan frames can be stale return addresses from earlier calls.")
        if hit:
            parts.append(f"{len(hit)}/{len(frames)} resolved: {_chain(hit)}.")
        if not gdb:
            parts.append(GDB_INSTALL_HINT)
    parts.append(_elf_note(elf))
    return {
        "ok": bool(hit),
        "summary": " ".join(parts),
        "causes": crash["causes"],
        "reset_reasons": crash["reset_reasons"],
        "backtrace_corrupted": corrupted,
        "frames": frames,
        "elf_path": str(elf),
        "env": tc.env,
        "arch": "riscv",
        "method": method,
        "core": block.core,
        "gdb": gdb,
        "unwind_error": unwind_error,
        "addr2line": tc.addr2line,
    }


def _decode(project_dir: str | None, env: str | None, text: str, include_all_hex: bool) -> dict[str, Any]:
    text_arch = arch_for_text(text)
    crash = extract_crash(text, include_all_hex=include_all_hex, arch=text_arch)
    block = extract_panic_block(text) if text_arch == "riscv" else None
    if not crash["addresses"] and block is None:
        return {
            "ok": False,
            "error": "no_addresses",
            "summary": "No crash addresses found in the text. Expected an ESP32 'Backtrace: 0x...:0x...' line, a PC/A0/EXCVADDR register dump, a RISC-V MEPC/RA register dump with 'Stack memory:', or a Cortex-M pc/lr dump. Pass include_all_hex=true to try every 0x-prefixed value.",
            "causes": crash["causes"],
            "reset_reasons": crash["reset_reasons"],
        }
    tc = load_toolchain(project_dir, env)
    elf = require_elf(tc)
    arch = arch_for_prefix(tc.prefix) or text_arch
    if arch != text_arch and arch is not None:
        crash = extract_crash(text, include_all_hex=include_all_hex, arch=arch)
    if arch == "riscv":
        block = block or extract_panic_block(text)
        if block is not None:
            return _decode_riscv(tc, elf, crash, block)
    addrs = crash["addresses"]
    resolved = symbolize(tc, [a.address for a in addrs])
    frames = []
    for a in addrs:
        r = resolved.get(a.address, {})
        frames.append({**a.to_dict(), "function": r.get("function"), "file": r.get("file"), "line": r.get("line"), "resolved": r.get("resolved", False), "inlined": r.get("inlined", [])})
    hit = [f for f in frames if f["resolved"]]
    parts = []
    if crash["causes"]:
        parts.append("Cause: " + crash["causes"][0].rstrip(".") + ".")
    if hit:
        parts.append(f"{len(hit)}/{len(frames)} address(es) resolved: {_chain(hit)}.")
    else:
        parts.append(f"None of the {len(frames)} address(es) resolved against {elf.name}; the ELF probably does not match the flashed firmware (rebuild without changes, or flash again), or the addresses are data/stack pointers.")
    if crash["backtrace_corrupted"]:
        parts.append("The backtrace was marked CORRUPTED by the panic handler; frames after the last resolved one are unreliable.")
    if arch == "riscv":
        parts.append("RISC-V panic output had no 'Stack memory:' dump, so only the registers were symbolized; capture the whole panic block for a full call chain.")
    if text_arch and arch and text_arch != arch:
        parts.append(f"The panic output looks like {text_arch} but env {tc.env} builds for {arch}; check that env matches the board that crashed.")
    parts.append(_elf_note(elf))
    return {
        "ok": bool(hit),
        "summary": " ".join(parts),
        "causes": crash["causes"],
        "reset_reasons": crash["reset_reasons"],
        "backtrace_corrupted": crash["backtrace_corrupted"],
        "frames": frames,
        "elf_path": str(elf),
        "env": tc.env,
        "arch": arch,
        "method": "addr2line",
        "addr2line": tc.addr2line,
    }


@guard
def pio_decode_backtrace(project_dir: str | None = None, env: str | None = None, text: str | None = None, session_id: str | None = None, include_all_hex: bool = False) -> dict[str, Any]:
    if session_id:
        s = monitors.get(session_id)
        r = s.read(cursor=0, max_lines=s.max_lines, timeout_s=0)
        text = "\n".join(r["lines"] + ([r["partial_line"]] if r["partial_line"] else []))
    if not text or not text.strip():
        raise ValueError("pass `text` (serial output containing the crash) or `session_id` of an open monitor session.")
    return _decode(project_dir, env, text, include_all_hex)


@guard
def pio_size_report(project_dir: str | None = None, env: str | None = None, top: int = 25, filter: str | None = None) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    cfg = project_config(str(path))
    env = env or (default_envs(cfg) or [None])[0]
    tc = load_toolchain(str(path), env)
    elf = require_elf(tc)
    if not tc.size or not tc.nm:
        raise FileNotFoundError(f"size/nm not found next to {tc.cc_path} (missing: {', '.join(tc.missing())}). Native envs on macOS use Apple tools with a different format; this tool targets GNU toolchains.")
    sections = parse_size_sections(run_tool([tc.size, "-A", str(elf)]))
    totals = parse_size_totals(run_tool([tc.size, "-B", str(elf)])) or {}
    symbols = parse_nm(run_tool([tc.nm, "-S", "-C", "-l", "--size-sort", "--defined-only", str(elf)]))
    if filter:
        rx = re.compile(filter, re.I)
        symbols = [s for s in symbols if rx.search(s["name"]) or (s["file"] and rx.search(s["file"]))]
    by_file = group_by_file(symbols, str(path))
    limits = _board_limits(cfg, tc.env)
    memory, mem_log = _pio_memory(str(path), tc.env)
    if memory:
        flash_pct, ram_pct, source = memory.get("flash", {}).get("percent"), memory.get("ram", {}).get("percent"), "platformio"
    else:
        flash_pct = round(100 * totals["flash_estimate"] / limits["flash_bytes"], 1) if totals and limits.get("flash_bytes") else None
        ram_pct = round(100 * totals["ram_estimate"] / limits["ram_bytes"], 1) if totals and limits.get("ram_bytes") else None
        source = "estimate_from_size"
    region_totals: dict[str, int] = {}
    for s in sections:
        region_totals[s["region"]] = region_totals.get(s["region"], 0) + s["size"]
    parts = [f"{elf.name} for env {tc.env}:"]
    if memory:
        f, r = memory.get("flash", {}), memory.get("ram", {})
        parts.append(f"Flash {f.get('percent')}% ({f.get('used_bytes', 0):,} of {f.get('total_bytes', 0):,} B), RAM {r.get('percent')}% ({r.get('used_bytes', 0):,} of {r.get('total_bytes', 0):,} B) per PlatformIO.")
    elif totals:
        parts.append(f"text {totals['text']:,} B, data {totals['data']:,} B, bss {totals['bss']:,} B")
        parts.append(f"(~{totals['flash_estimate']:,} B flash" + (f" = {flash_pct}% of {limits['board']}" if flash_pct is not None else "") + f", ~{totals['ram_estimate']:,} B static RAM" + (f" = {ram_pct}%" if ram_pct is not None else "") + "; estimated from `size`, so ESP32 rodata may be over-counted as RAM).")
    if symbols:
        big = symbols[0]
        parts.append(f"Largest symbol: {big['name']} ({big['size']:,} B, {big['kind']}" + (f", {big['file'].split('/')[-1]}:{big['line']}" if big.get("file") else "") + ").")
    if by_file:
        parts.append("Biggest files: " + ", ".join(f"{f['file'].split('/')[-1]} {f['size']:,} B" for f in by_file[:3]) + ".")
    parts.append("Note: heap and stack are not counted in static RAM; the Arduino/ESP-IDF runtime adds its own.")
    return {
        "ok": True,
        "summary": " ".join(parts),
        "env": tc.env,
        "elf_path": str(elf),
        "board": limits,
        "totals": totals,
        "memory": memory,
        "memory_source": source,
        "memory_log_path": mem_log,
        "flash_percent": flash_pct,
        "ram_percent": ram_pct,
        "region_totals": region_totals,
        "sections": sections[:40],
        "top_symbols": symbols[:top],
        "symbol_count": len(symbols),
        "top_files": by_file[:top],
        "filter": filter,
    }


@guard
def pio_flash_and_verify(
    project_dir: str | None = None,
    env: str | None = None,
    expect: str = r"setup done|ready|started|Booting|loop",
    fail_on: str = DEFAULT_FAIL_PATTERN,
    timeout_s: float = 30,
    upload_port: str | None = None,
    monitor_port: str | None = None,
    baud: int | None = None,
    stop_open_sessions: bool = False,
    max_lines: int = 500,
    settle_s: float = 1.5,
) -> dict[str, Any]:
    check_policy("flash")
    path = resolve_project_dir(project_dir)
    port, cfg_baud = _pick_port(monitor_port or upload_port, str(path), env)
    baud = baud or cfg_baud or 115200
    held = monitors.session_on_port(port)
    if held:
        if not stop_open_sessions:
            raise RuntimeError(f"serial session {held.id} holds {port}; pass stop_open_sessions=true or call pio_monitor_stop first.")
        monitors.stop(held.id)
    t0 = time.monotonic()
    up = pio_upload(project_dir=str(path), env=env, upload_port=upload_port or port)
    upload_s = round(time.monotonic() - t0, 2)
    if not up.get("ok"):
        failed: dict[str, Any] = {"ok": False, "verdict": "upload_failed", "summary": "Upload failed, nothing verified. " + up.get("summary", ""), "port": port, "baud": baud, "upload": up}
        if up.get("port_error"):
            failed["error"] = up["error"]
            failed["port_diagnosis"] = up.get("port_diagnosis")
        return failed
    combined = f"(?P<pass_>{expect})|(?P<fail_>{fail_on})" if fail_on else f"(?P<pass_>{expect})"
    rx = re.compile(combined)
    s = monitors.start(port, baud)
    try:
        r = s.read(cursor=0, max_lines=max_lines, wait_for=combined, timeout_s=min(timeout_s, 300))
        lines = list(r["lines"])
        matched_line = lines[-1] if r["matched"] and lines else None
        verdict = "timeout"
        if matched_line is not None:
            m = rx.search(matched_line)
            verdict = "fail" if m and m.group("fail_") is not None and (m.group("pass_") is None) else "pass"
        if verdict == "fail" and settle_s > 0:
            extra = s.read(cursor=r["cursor"], max_lines=max_lines, timeout_s=settle_s)
            lines += extra["lines"]
            # Keep collecting while a backtrace is still arriving.
            more = s.read(cursor=extra["cursor"], max_lines=max_lines, timeout_s=settle_s / 2)
            lines += more["lines"]
        port_error = r["error"]
    finally:
        monitors.stop(s.id)
    verify_s = round(time.monotonic() - t0 - upload_s, 2)
    result: dict[str, Any] = {
        "ok": verdict == "pass",
        "verdict": verdict,
        "port": port,
        "baud": baud,
        "matched_line": matched_line,
        "expect": expect,
        "fail_on": fail_on,
        "lines": lines[-max_lines:],
        "line_count": len(lines),
        "upload_s": upload_s,
        "verify_s": verify_s,
        "upload": {k: up.get(k) for k in ("summary", "memory", "warning_count", "log_path", "duration_s")},
        "port_error": port_error,
    }
    if verdict == "pass":
        result["summary"] = f"PASS: flashed {tc_env(up, env)} in {upload_s}s and saw '{matched_line}' on {port} after {verify_s}s of boot output ({len(lines)} line(s))."
    elif verdict == "fail":
        result["summary"] = f"FAIL: firmware flashed but the boot log matched the failure pattern: '{matched_line}'. {len(lines)} line(s) captured."
        text = "\n".join(lines)
        try:
            decoded = _decode(str(path), env, text, include_all_hex=False)
        except Exception as exc:  # decoding is best effort; the verdict stands
            decoded = {"ok": False, "error": type(exc).__name__, "summary": str(exc)}
        result["decoded"] = decoded
        if decoded.get("ok"):
            result["summary"] += " " + decoded["summary"]
    else:
        result["summary"] = f"TIMEOUT: flashed OK but neither expect ('{expect}') nor fail_on matched within {timeout_s}s on {port} at {baud} baud; {len(lines)} line(s) seen." + (
            " No output at all: check baud (monitor_speed), that the board prints on this port, or raise timeout_s." if not lines and not port_error else ""
        ) + (f" Port error: {port_error}." if port_error else "")
    return result


def tc_env(up: dict[str, Any], env: str | None) -> str:
    envs = up.get("environments") or []
    return f"env {env or ','.join(envs) or 'default'}"


def register(mcp) -> None:
    mcp.tool(name="pio_decode_backtrace", description=(
        "Turn a crash dump into source locations. Give `text` containing an ESP32/ESP-IDF 'Guru Meditation' register dump and "
        "'Backtrace: 0x...:0x...' line, a RISC-V ESP32 (C2/C3/C5/C6/C61/H2/H4/P4/S31) MEPC/RA register dump with its 'Stack memory:' block, "
        "or a Cortex-M HardFault pc/lr dump, or give `session_id` of an open monitor session to scan its buffer. "
        "Xtensa and Cortex-M addresses are resolved with the toolchain's addr2line; RISC-V stacks are unwound with the toolchain's GDB "
        "into a full call chain (method=gdb_unwind), or, without GDB, stack words pointing into code are resolved (method=stack_scan, may include stale frames). "
        "Returns function, file, line per frame, plus the crash cause and reset reason. The ELF must be from the same build that was flashed."
    ))(pio_decode_backtrace)
    mcp.tool(name="pio_size_report", description=(
        "Explain where flash and RAM go in the built firmware.elf: text/data/bss totals with percent of the board's flash and RAM, "
        "loaded sections classified as flash/ram, the biggest symbols (demangled, with file:line), and per-source-file totals. "
        "Use it to shrink firmware on purpose: drop the largest fonts/tables, remove unused features, tune build flags. "
        "Run pio_build first; `filter` is a regex applied to symbol names and file paths."
    ))(pio_size_report)
    mcp.tool(name="pio_flash_and_verify", description=(
        "Hardware-in-the-loop check with no human: build + flash (`pio run -t upload`), then open the serial port and watch the boot log "
        "until `expect` (regex) matches -> verdict pass, or `fail_on` matches -> verdict fail with the crash automatically decoded to file:line, "
        "or timeout_s elapses -> verdict timeout. Port and baud come from platformio.ini (monitor_port/monitor_speed) or the single detected board. "
        "Set expect to a line your firmware prints once it is healthy, e.g. 'WiFi connected'. Blocked under build_only/read_only policy."
    ))(pio_flash_and_verify)
