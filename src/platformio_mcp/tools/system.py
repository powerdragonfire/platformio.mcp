"""Environment and health tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import __version__
from ..core import guard, monitors, policy, run_json
from ..parsers import parse_pkg_list
from ..pio import INSTALL_HINT, find_pio, log_dir, run_pio


def _installed_platforms() -> list[dict[str, Any]]:
    try:
        _, rows = run_json(["platform", "list", "--json-output"], tool="platform-list")
    except RuntimeError:
        return []
    return [{"name": r.get("name"), "version": r.get("version"), "path": r.get("__pkg_dir")} for r in rows]


def _installed_tools(core_dir: str | None) -> list[dict[str, Any]]:
    """Global tool and framework packages. `pio pkg list` has no JSON mode, so parse its tree output."""
    res = run_pio(["pkg", "list", "--global", "--only-tools"], tool="pkg-list-global", keep_log=False)
    if not res.ok:
        return []
    packages = Path(core_dir) / "packages" if core_dir else None
    tools = []
    for row in parse_pkg_list(res.output):
        name, version = row["spec"], row["version"]
        path = None
        if packages is not None:
            path = next((str(c) for c in (packages / f"{name}@{version}", packages / name) if c.is_dir()), None)
        tools.append({"name": name, "version": version, "path": path})
    return tools


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
        "platforms": _installed_platforms(),
        "tools": _installed_tools(flat.get("core_dir")),
        "obsolete_core_warning": obsolete,
        "log_dir": str(log_dir()),
        "open_monitor_sessions": monitors.list(),
    }


def register(mcp) -> None:
    mcp.tool(name="pio_system_info", description=(
        "Check that PlatformIO is installed and report its version, core directory, the active safety policy "
        "(full | build_only | read_only), the log directory, any open serial monitor sessions, and the globally installed "
        "platforms and tool packages (name, version, path), which works without a project. "
        "Call this first in a session; if PlatformIO is missing the result tells you how to install it."
    ))(pio_system_info)
