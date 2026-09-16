"""Build, upload, clean, and other `pio run` targets."""

from __future__ import annotations

from typing import Any

from ..core import check_policy, guard, monitors
from ..parsers import parse_build_result, parse_diagnostics, parse_memory, parse_targets
from ..pio import DEFAULT_TIMEOUTS, resolve_project_dir, run_pio, tail

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
    }


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
def pio_upload(project_dir: str | None = None, env: str | None = None, upload_port: str | None = None) -> dict[str, Any]:
    check_policy("flash")
    open_sessions = monitors.list()
    if upload_port:
        held = monitors.session_on_port(upload_port)
        if held:
            raise RuntimeError(f"serial monitor session {held.id} holds {upload_port}; call pio_monitor_stop('{held.id}') before uploading.")
    elif open_sessions:
        raise RuntimeError("serial monitor session(s) are open: " + ", ".join(f"{s['session_id']} on {s['port']}" for s in open_sessions) + ". Stop them (pio_monitor_stop) or pass upload_port explicitly.")
    extra = ["--upload-port", upload_port] if upload_port else []
    result = _run(project_dir, env, ["upload"], extra, "upload", DEFAULT_TIMEOUTS["upload"])
    if result["ok"]:
        result["summary"] += " Firmware flashed. Start pio_monitor_start (or pio_monitor_capture) to watch the boot log."
    return result


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
def pio_run_target(target: str, project_dir: str | None = None, env: str | None = None, upload_port: str | None = None) -> dict[str, Any]:
    check_policy("flash" if target in FLASH_TARGETS else "build")
    extra = ["--upload-port", upload_port] if upload_port else []
    timeout = DEFAULT_TIMEOUTS["upload"] if target in FLASH_TARGETS else DEFAULT_TIMEOUTS["build"]
    return _run(project_dir, env, [target], extra, f"target-{target}", timeout)


def register(mcp) -> None:
    mcp.tool(name="pio_build", description=(
        "Compile the project (`pio run`). Returns a status, parsed compiler errors and warnings with file/line/column, "
        "RAM and Flash usage percentages, the last 40 lines of output, and a path to the full log. "
        "Fix the listed errors, then build again. First builds may take minutes while toolchains download."
    ))(pio_build)
    mcp.tool(name="pio_upload", description=(
        "Build and flash firmware to the connected board (`pio run -t upload`). Refuses while a serial monitor "
        "session holds the port; stop it first. Pass upload_port when several boards are attached. "
        "Blocked when PLATFORMIO_MCP_POLICY is build_only or read_only."
    ))(pio_upload)
    mcp.tool(name="pio_clean", description="Delete build artifacts for an env (`pio run -t clean`); full=true also removes downloaded dependencies (fullclean).")(pio_clean)
    mcp.tool(name="pio_list_targets", description=(
        "List extra build targets the platform offers for this project, such as buildfs, uploadfs, erase, size, or menuconfig."
    ))(pio_list_targets)
    mcp.tool(name="pio_run_target", description=(
        "Run one named target from pio_list_targets (e.g. 'buildfs', 'uploadfs', 'erase', 'size'). "
        "Flash-related targets follow the same policy rules as pio_upload."
    ))(pio_run_target)
