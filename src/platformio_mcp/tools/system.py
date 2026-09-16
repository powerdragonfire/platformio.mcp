"""Environment and health tools."""

from __future__ import annotations

from typing import Any

from .. import __version__
from ..core import guard, monitors, policy, run_json
from ..pio import INSTALL_HINT, find_pio, log_dir, run_pio


@guard
def pio_system_info() -> dict[str, Any]:
    cmd = find_pio()
    if cmd is None:
        return {"ok": False, "error": "pio_not_found", "summary": INSTALL_HINT, "server_version": __version__, "policy": policy()}
    ver = run_pio(["--version"], tool="version", keep_log=False)
    obsolete = "Obsolete PIO Core" in ver.raw_output
    _, info = run_json(["system", "info", "--json-output"], tool="system-info")
    flat = {k: v.get("value") for k, v in info.items()}
    summary = f"PlatformIO Core {flat.get('core_version')} at {flat.get('platformio_exe')} (python {flat.get('python_version')}, {flat.get('dev_platform_nums')} platforms installed). Policy: {policy()}."
    if obsolete:
        summary += " Warning: two PIO Core versions are installed; pio prints an 'Obsolete PIO Core' banner. Remove one to silence it."
    return {
        "ok": True,
        "summary": summary,
        "server_version": __version__,
        "policy": policy(),
        "pio_command": cmd,
        "core_version": flat.get("core_version"),
        "python": flat.get("python_version"),
        "system": flat.get("system"),
        "core_dir": flat.get("core_dir"),
        "installed_platforms": flat.get("dev_platform_nums"),
        "installed_tools": flat.get("package_tool_nums"),
        "obsolete_core_warning": obsolete,
        "log_dir": str(log_dir()),
        "open_monitor_sessions": monitors.list(),
    }


def register(mcp) -> None:
    mcp.tool(name="pio_system_info", description=(
        "Check that PlatformIO is installed and report its version, core directory, the active safety policy "
        "(full | build_only | read_only), the log directory, and any open serial monitor sessions. "
        "Call this first in a session; if PlatformIO is missing the result tells you how to install it."
    ))(pio_system_info)
