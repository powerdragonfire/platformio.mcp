"""Live debugging: gdb sessions over a debug probe via `pio debug --interface=gdb`."""

from __future__ import annotations

from typing import Any

from ..core import (
    check_policy,
    default_envs,
    env_names,
    env_setting,
    guard,
    project_config,
)
from ..debugger import DebugStartError, debuggers
from ..pio import resolve_project_dir


def _debug_env(cfg: dict[str, dict[str, Any]], env: str | None) -> str | None:
    """PlatformIO's own rule: an explicit env, else the first default env with build_type = debug, else the first default env."""
    if env:
        return env
    defaults = default_envs(cfg) or env_names(cfg)
    for e in defaults:
        if env_setting(cfg, e, "build_type") == "debug":
            return e
    return defaults[0] if defaults else None


def _frame_text(stopped: dict[str, Any] | None) -> str:
    if not stopped:
        return "no stop event yet (target running)"
    f = stopped.get("frame") or {}
    where = f.get("function") or f.get("address") or "?"
    if f.get("file"):
        where += f" ({str(f['file']).split('/')[-1]}:{f.get('line')})"
    reason = stopped.get("reason") or "stopped"
    if stopped.get("signal_name"):
        reason += f" {stopped['signal_name']}"
    return f"{reason} at {where}"


@guard
def pio_debug_start(project_dir: str | None = None, env: str | None = None, load: bool = True, timeout_s: float = 90) -> dict[str, Any]:
    check_policy("flash")
    path = resolve_project_dir(project_dir)
    cfg = project_config(str(path))
    env = _debug_env(cfg, env)
    configured_tool = env_setting(cfg, env, "debug_tool") if env else None
    try:
        s = debuggers.start(str(path), env, load=load, timeout_s=min(timeout_s, 600))
    except DebugStartError as exc:
        return {
            "ok": False,
            "error": exc.code,
            "summary": f"debug session for env {env or 'default'} did not start: {exc.summary}",
            "env": env,
            "debug_tool": configured_tool,
            "output_tail": exc.output[-2500:],
        }
    info = s.info()
    tool = s.debug_tool or configured_tool or "platform default"
    parts = [f"Debug session {s.id} on env {env or 'default'} via {tool}" + (f" ({s.gdb_version})" if s.gdb_version else "") + "."]
    parts.append("Firmware loaded through the probe; " if load else "Firmware not reloaded (load=false); ")
    parts[-1] += "target " + _frame_text(s.last_stop) + "."
    parts.append(f"Send gdb commands with pio_debug_cmd(session_id='{s.id}', command='bt'|'p var'|'break src/main.cpp:42'|'continue'|'next'|'info registers'|'x/16xw 0x3ffb0000'). Stop with pio_debug_stop before pio_upload.")
    return {
        "ok": True,
        "summary": " ".join(parts),
        **info,
        "load": load,
        "command": s.command,
        "init_script": s.init_script,
        "stopped": s.last_stop,
    }


@guard
def pio_debug_cmd(session_id: str, command: str, timeout_s: float = 30) -> dict[str, Any]:
    s = debuggers.get(session_id)
    r = s.run(command, timeout_s=min(timeout_s, 600))
    console_n = len(r["console"])
    if r["error"]:
        summary = f"gdb error for '{command}': {r['error']}"
    elif r["stopped"]:
        summary = f"'{command}' -> target {_frame_text(r['stopped'])}."
    elif r["timed_out"]:
        summary = f"'{command}' did not finish within {timeout_s}s" + ("; the target is still running (no breakpoint hit). Send 'interrupt' to halt it, or wait with another command." if r["running"] else ".")
    elif r["closed"]:
        summary = f"gdb exited during '{command}' (exit code {r['exit_code']}); the session is closed."
    else:
        summary = f"'{command}' -> {r['result_class'] or 'no result record'}; {console_n} console line(s)."
        if r["console"]:
            summary += " First: " + r["console"][0][:200]
    ok = r["error"] is None and not r["timed_out"] and not (r["closed"] and r["result_class"] != "exit")
    return {"ok": ok, "summary": summary, **r}


@guard
def pio_debug_stop(session_id: str) -> dict[str, Any]:
    info = debuggers.stop(session_id)
    return {"ok": True, "summary": f"debug session {session_id} closed after {info['uptime_s']}s (target resumed by pio_reset_run_target on exit). The probe is free for pio_upload.", **info}


@guard
def pio_debug_list() -> dict[str, Any]:
    rows = debuggers.list()
    return {"ok": True, "summary": f"{len(rows)} open debug session(s)." + (" " + "; ".join(f"{r['session_id']}: env {r['env']} {'running' if r['running'] else 'halted'}" for r in rows) if rows else ""), "sessions": rows}


def register(mcp) -> None:
    mcp.tool(name="pio_debug_start", description=(
        "Open a live gdb session on the board through its debug probe (`pio debug --interface=gdb`, GDB/MI over pipes): "
        "starts the debug server (OpenOCD, J-Link, ST-Link, esp-prog, ...) from `debug_tool` in platformio.ini, builds a debug firmware, "
        "loads it (load=true) and halts at `debug_init_break` (default: main). Returns session_id, the .pioinit script PlatformIO generated, "
        "and the initial stop frame. The session holds the probe: stop it (pio_debug_stop) before pio_upload or pio_flash_and_verify. "
        "Blocked under build_only/read_only policy."
    ))(pio_debug_start)
    mcp.tool(name="pio_debug_cmd", description=(
        "Send one gdb command to an open debug session and get its output back, parsed from GDB/MI: `bt`, `p var`, `p/x reg`, `info locals`, "
        "`info registers`, `x/16xw addr`, `break src/main.cpp:42`, `watch counter`, `next`, `step`, `finish`, `continue`, `monitor reset halt`, "
        "or raw MI such as `-stack-list-frames`. Execution commands (continue/next/step/finish) block until the target stops again or timeout_s passes; "
        "on timeout the target keeps running and `interrupt` halts it. Returns result_class, console lines, error, and stopped {reason, frame{function,file,line}}."
    ))(pio_debug_cmd)
    mcp.tool(name="pio_debug_stop", description="Quit gdb and the debug server, resume the target, and free the probe so pio_upload works again.")(pio_debug_stop)
    mcp.tool(name="pio_debug_list", description="List open debug sessions with env, debug tool, running/halted state, and the last stop frame.")(pio_debug_list)
