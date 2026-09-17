"""Build, upload, clean, and other `pio run` targets."""

from __future__ import annotations

from typing import Any

from ..core import check_policy, guard, monitors
from ..parsers import classify_port_error, parse_build_result, parse_diagnostics, parse_memory, parse_targets
from ..pio import DEFAULT_TIMEOUTS, resolve_project_dir, run_pio, tail
from .devices import _pick_port, diagnose_port

FLASH_TARGETS = {"upload", "uploadfs", "uploadfsota", "erase", "program", "bootloader", "fuses", "uploadeep"}


def _run(project_dir: str | None, env: str | None, targets: list[str], extra: list[str], tool: str, timeout: float) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    args = ["run", "-d", str(path)]
    if env:
        args += ["-e", env]
    for t in targets:
        args += ["-t", t]
    args += extra
    res = run_pio(args, cwd=str(path), timeout=timeout, tool=tool)
    diags = parse_diagnostics(res.output)
    errors = [d.to_dict() for d in diags if d.kind == "error"]
    warnings = [d.to_dict() for d in diags if d.kind == "warning"]
    result = parse_build_result(res.output)
    memory = parse_memory(res.output)
    ok = res.ok and result["status"] != "failed"
    status = "timeout" if res.timed_out else ("success" if ok else "failed")
    parts = [f"{tool} {status} for env {env or ','.join(result['environments']) or 'default'} in {res.duration_s}s."]
    if errors:
        first = errors[0]
        parts.append(f"{len(errors)} error(s); first: {first['file']}:{first['line']}: {first['message']}")
    if warnings:
        parts.append(f"{len(warnings)} warning(s).")
    if memory:
        parts.append("RAM {:.1f}%, Flash {:.1f}%.".format(memory.get("ram", {}).get("percent", 0), memory.get("flash", {}).get("percent", 0)))
    if res.timed_out:
        parts.append("The command timed out; first builds download toolchains and can take several minutes, retry once.")
    return {
        "ok": ok,
        "status": status,
        "summary": " ".join(parts),
        "environments": result["environments"],
        "errors": errors[:50],
        "warnings": warnings[:50],
        "error_count": len(errors),
        "warning_count": len(warnings),
        "memory": memory,
        "duration_s": res.duration_s,
        "exit_code": res.returncode,
        "log_path": res.log_path,
        "output_tail": tail(res.output, 40),
        "port_error": None if ok else classify_port_error(res.output),
    }


def _release_port(upload_port: str | None, stop_open_sessions: bool) -> list[str]:
    """Stop our own monitor sessions that would block the upload, or refuse when not allowed to."""
    held = [monitors.session_on_port(upload_port)] if upload_port else [monitors.get(s["session_id"]) for s in monitors.list()]
    held = [s for s in held if s is not None]
    if not held:
        return []
    if not stop_open_sessions:
        names = ", ".join(f"{s.id} on {s.port}" for s in held)
        if upload_port:
            raise RuntimeError(f"serial monitor session {names} holds the port; call pio_monitor_stop('{held[0].id}') or pass stop_open_sessions=true.")
        raise RuntimeError(f"serial monitor session(s) are open: {names}. Stop them (pio_monitor_stop), pass stop_open_sessions=true, or pass upload_port explicitly.")
    for s in held:
        monitors.stop(s.id)
    return [s.id for s in held]


def _explain_port_failure(result: dict[str, Any], upload_port: str | None, project_dir: str | None, env: str | None) -> dict[str, Any]:
    code = result.get("port_error")
    if not code:
        return result
    port = upload_port
    if not port:
        try:
            port, _ = _pick_port(None, str(resolve_project_dir(project_dir)), env)
        except Exception:
            port = None
    result["error"] = code
    if port:
        diag = diagnose_port(port, code)
        result["port_diagnosis"] = diag
        result["summary"] = f"Upload failed ({code}): {diag['hint']}"
    else:
        result["summary"] = f"Upload failed ({code}) and no single port could be identified; run pio_list_devices and pass upload_port explicitly."
    return result


@guard
def pio_build(project_dir: str | None = None, env: str | None = None, jobs: int | None = None, verbose: bool = False) -> dict[str, Any]:
    check_policy("build")
    extra: list[str] = []
    if jobs:
        extra += ["-j", str(jobs)]
    if verbose:
        extra.append("-v")
    return _run(project_dir, env, [], extra, "build", DEFAULT_TIMEOUTS["build"])


@guard
def pio_upload(project_dir: str | None = None, env: str | None = None, upload_port: str | None = None, stop_open_sessions: bool = False) -> dict[str, Any]:
    check_policy("flash")
    stopped = _release_port(upload_port, stop_open_sessions)
    extra = ["--upload-port", upload_port] if upload_port else []
    result = _run(project_dir, env, ["upload"], extra, "upload", DEFAULT_TIMEOUTS["upload"])
    result["stopped_sessions"] = stopped
    if result["ok"]:
        result["summary"] += (f" Stopped monitor session(s) {', '.join(stopped)} first." if stopped else "") + " Firmware flashed. Start pio_monitor_start (or pio_monitor_capture) to watch the boot log."
    return _explain_port_failure(result, upload_port, project_dir, env)


@guard
def pio_clean(project_dir: str | None = None, env: str | None = None, full: bool = False) -> dict[str, Any]:
    check_policy("build")
    return _run(project_dir, env, ["fullclean" if full else "clean"], [], "clean", DEFAULT_TIMEOUTS["default"])


@guard
def pio_list_targets(project_dir: str | None = None, env: str | None = None) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    args = ["run", "-d", str(path), "--list-targets"]
    if env:
        args += ["-e", env]
    res = run_pio(args, cwd=str(path), tool="list-targets", timeout=600)
    if not res.ok:
        return {"ok": False, "error": "list_targets_failed", "summary": res.output[-1500:], "log_path": res.log_path}
    targets = parse_targets(res.output)
    return {"ok": True, "summary": f"{len(targets)} target(s): " + ", ".join(sorted({t['name'] for t in targets})), "targets": targets}


@guard
def pio_run_target(target: str, project_dir: str | None = None, env: str | None = None, upload_port: str | None = None, stop_open_sessions: bool = False) -> dict[str, Any]:
    flashes = target in FLASH_TARGETS
    check_policy("flash" if flashes else "build")
    stopped = _release_port(upload_port, stop_open_sessions) if flashes else []
    extra = ["--upload-port", upload_port] if upload_port else []
    timeout = DEFAULT_TIMEOUTS["upload"] if flashes else DEFAULT_TIMEOUTS["build"]
    result = _run(project_dir, env, [target], extra, f"target-{target}", timeout)
    if not flashes:
        return result
    result["stopped_sessions"] = stopped
    if result["ok"] and stopped:
        result["summary"] += f" Stopped monitor session(s) {', '.join(stopped)} first."
    return _explain_port_failure(result, upload_port, project_dir, env)


def register(mcp) -> None:
    mcp.tool(name="pio_build", description=(
        "Compile the project (`pio run`). Returns a status, parsed compiler errors and warnings with file/line/column, "
        "RAM and Flash usage percentages, the last 40 lines of output, and a path to the full log. "
        "Fix the listed errors, then build again. First builds may take minutes while toolchains download."
    ))(pio_build)
    mcp.tool(name="pio_upload", description=(
        "Build and flash firmware to the connected board (`pio run -t upload`). Refuses while a serial monitor "
        "session holds the port unless stop_open_sessions=true, which closes our own session(s) first. Pass upload_port when several boards are attached. "
        "Port failures come back classified (port_busy, port_permission, port_missing, no_response) with a port_diagnosis naming the holder and the fix. "
        "Blocked when PLATFORMIO_MCP_POLICY is build_only or read_only."
    ))(pio_upload)
    mcp.tool(name="pio_clean", description="Delete build artifacts for an env (`pio run -t clean`); full=true also removes downloaded dependencies (fullclean).")(pio_clean)
    mcp.tool(name="pio_list_targets", description=(
        "List extra build targets the platform offers for this project, such as buildfs, uploadfs, erase, size, or menuconfig."
    ))(pio_list_targets)
    mcp.tool(name="pio_run_target", description=(
        "Run one named target from pio_list_targets (e.g. 'buildfs', 'uploadfs', 'erase', 'size'). "
        "Flash-related targets follow the same policy, stop_open_sessions, and port-diagnosis rules as pio_upload."
    ))(pio_run_target)
