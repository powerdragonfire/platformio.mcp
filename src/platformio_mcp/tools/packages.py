"""Libraries, platforms, and tools via `pio pkg`."""

from __future__ import annotations

from typing import Any

from ..core import check_policy, guard
from ..parsers import parse_pkg_list, parse_pkg_search
from ..pio import resolve_project_dir, run_pio, tail

TYPE_FLAG = {"library": "-l", "platform": "-p", "tool": "-t"}


@guard
def pio_pkg_search(query: str, type: str = "library", page: int = 1) -> dict[str, Any]:
    if type not in TYPE_FLAG:
        raise ValueError("type must be library, platform, or tool")
    q = f"type:{type} {query}".strip()
    res = run_pio(["pkg", "search", q, "-p", str(page)], tool="pkg-search", timeout=90)
    if not res.ok:
        return {"ok": False, "error": "search_failed", "summary": res.output[-800:], "log_path": res.log_path}
    parsed = parse_pkg_search(res.output)
    top = parsed["packages"][:3]
    return {
        "ok": True,
        "summary": f"{parsed['total']} {type} package(s) match '{query}' (page {parsed['page']}/{parsed['pages']})." + (" Top: " + ", ".join(f"{p['spec']}@{p['version']}" for p in top) if top else ""),
        **parsed,
        "install_hint": "install with pio_pkg_install(spec='owner/name@^version')",
    }


@guard
def pio_pkg_install(spec: str, project_dir: str | None = None, env: str | None = None, type: str = "library") -> dict[str, Any]:
    check_policy("build")
    if type not in TYPE_FLAG:
        raise ValueError("type must be library, platform, or tool")
    path = resolve_project_dir(project_dir)
    args = ["pkg", "install", "-d", str(path), TYPE_FLAG[type], spec]
    if env:
        args += ["-e", env]
    res = run_pio(args, cwd=str(path), tool="pkg-install", timeout=900)
    if not res.ok:
        return {"ok": False, "error": "install_failed", "summary": f"install of {spec} failed (exit {res.returncode}).", "output_tail": tail(res.output, 30), "log_path": res.log_path}
    return {"ok": True, "summary": f"{type} {spec} installed" + (f" for env {env}" if env else "") + " and recorded in platformio.ini.", "output_tail": tail(res.output, 15), "log_path": res.log_path}


@guard
def pio_pkg_uninstall(spec: str, project_dir: str | None = None, env: str | None = None, type: str = "library") -> dict[str, Any]:
    check_policy("build")
    path = resolve_project_dir(project_dir)
    args = ["pkg", "uninstall", "-d", str(path), TYPE_FLAG[type], spec]
    if env:
        args += ["-e", env]
    res = run_pio(args, cwd=str(path), tool="pkg-uninstall", timeout=300)
    return {"ok": res.ok, "summary": f"{type} {spec} {'removed' if res.ok else 'could not be removed'}.", "output_tail": tail(res.output, 15), "log_path": res.log_path}


@guard
def pio_pkg_list(project_dir: str | None = None, env: str | None = None) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    args = ["pkg", "list", "-d", str(path)]
    if env:
        args += ["-e", env]
    res = run_pio(args, cwd=str(path), tool="pkg-list", timeout=300)
    rows = parse_pkg_list(res.output)
    return {"ok": res.ok, "summary": f"{len(rows)} package(s) resolved: " + ", ".join(f"{r['spec']}@{r['version']}" for r in rows[:12]) + (" ..." if len(rows) > 12 else ""), "packages": rows, "output_tail": tail(res.output, 40), "log_path": res.log_path}


@guard
def pio_pkg_outdated(project_dir: str | None = None, env: str | None = None) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    args = ["pkg", "outdated", "-d", str(path)]
    if env:
        args += ["-e", env]
    res = run_pio(args, cwd=str(path), tool="pkg-outdated", timeout=300)
    return {"ok": res.ok, "summary": "Outdated package report below (Current / Wanted / Latest).", "output": tail(res.output, 60), "log_path": res.log_path}


@guard
def pio_pkg_update(project_dir: str | None = None, env: str | None = None) -> dict[str, Any]:
    check_policy("build")
    path = resolve_project_dir(project_dir)
    args = ["pkg", "update", "-d", str(path)]
    if env:
        args += ["-e", env]
    res = run_pio(args, cwd=str(path), tool="pkg-update", timeout=900)
    return {"ok": res.ok, "summary": "Dependencies updated within platformio.ini version constraints." if res.ok else "Update failed; see output_tail.", "output_tail": tail(res.output, 40), "log_path": res.log_path}


def register(mcp) -> None:
    mcp.tool(name="pio_pkg_search", description=(
        "Search the PlatformIO registry for libraries (default), platforms, or tools. Returns owner/name specs with the "
        "latest version and description. Example query: 'ArduinoJson', 'ssd1306 display', 'mqtt'."
    ))(pio_pkg_search)
    mcp.tool(name="pio_pkg_install", description=(
        "Install a library/platform/tool into the project and add it to platformio.ini (`pio pkg install -l spec`). "
        "spec examples: 'bblanchon/ArduinoJson@^7', 'adafruit/Adafruit NeoPixel', 'https://github.com/user/repo.git'. "
        "Restrict to one env with env=."
    ))(pio_pkg_install)
    mcp.tool(name="pio_pkg_uninstall", description="Remove a library/platform/tool from the project and platformio.ini.")(pio_pkg_uninstall)
    mcp.tool(name="pio_pkg_list", description="List resolved platform, toolchain, and library packages for the project with versions.")(pio_pkg_list)
    mcp.tool(name="pio_pkg_outdated", description="Show which project dependencies have newer versions available.")(pio_pkg_outdated)
    mcp.tool(name="pio_pkg_update", description="Update dependencies within the version ranges declared in platformio.ini.")(pio_pkg_update)
