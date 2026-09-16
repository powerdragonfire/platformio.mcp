"""Boards, project creation, and project configuration."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from ..core import (
    check_policy,
    default_envs,
    env_names,
    env_setting,
    guard,
    project_config,
    run_json,
)
from ..pio import resolve_project_dir, run_pio

BOARD_FIELDS = ("id", "name", "platform", "mcu", "vendor")


@functools.lru_cache(maxsize=1)
def _all_boards() -> list[dict[str, Any]]:
    _, boards = run_json(["boards", "--json-output"], tool="boards", timeout=180)
    return boards


def _compact_board(b: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": b.get("id"),
        "name": b.get("name"),
        "platform": b.get("platform"),
        "mcu": b.get("mcu"),
        "cpu_mhz": round(b.get("fcpu", 0) / 1_000_000, 1) if b.get("fcpu") else None,
        "ram_kb": round(b.get("ram", 0) / 1024, 1) if b.get("ram") else None,
        "flash_kb": round(b.get("rom", 0) / 1024, 1) if b.get("rom") else None,
        "frameworks": b.get("frameworks", []),
        "vendor": b.get("vendor"),
    }


@guard
def pio_list_boards(query: str, platform: str | None = None, framework: str | None = None, limit: int = 30) -> dict[str, Any]:
    q = query.strip().lower()
    if not q and not platform and not framework:
        raise ValueError("Give a query (e.g. 'esp32-s3', 'uno', 'STM32F4', 'DOIT') or a platform/framework filter; the full list has ~1,700 boards.")
    matches = []
    for b in _all_boards():
        if platform and b.get("platform", "").lower() != platform.lower():
            continue
        if framework and framework.lower() not in [f.lower() for f in b.get("frameworks", [])]:
            continue
        hay = " ".join(str(b.get(f, "")) for f in BOARD_FIELDS).lower()
        if q and q not in hay:
            continue
        matches.append(b)
    # Exact id matches first, then shorter ids (closer matches) first.
    matches.sort(key=lambda b: (b.get("id", "").lower() != q, len(b.get("id", ""))))
    rows = [_compact_board(b) for b in matches[:limit]]
    return {
        "ok": True,
        "summary": f"{len(matches)} boards match '{query}'" + (f" on platform {platform}" if platform else "") + (f"; showing {len(rows)}" if len(matches) > len(rows) else "") + ". Use the `id` value as the board in pio_project_init or platformio.ini.",
        "total_matches": len(matches),
        "boards": rows,
    }


@guard
def pio_board_info(board_id: str) -> dict[str, Any]:
    for b in _all_boards():
        if b.get("id") == board_id:
            c = _compact_board(b)
            c.update(
                {
                    "ram_bytes": b.get("ram"),
                    "flash_bytes": b.get("rom"),
                    "connectivity": b.get("connectivity", []),
                    "debug_tools": sorted((b.get("debug") or {}).get("tools", {}).keys()),
                    "default_debug_tool": next((k for k, v in (b.get("debug") or {}).get("tools", {}).items() if v.get("default")), None),
                    "url": b.get("url"),
                }
            )
            c["ok"] = True
            c["summary"] = f"{c['name']} ({board_id}): {c['mcu']} @ {c['cpu_mhz']} MHz, {c['ram_kb']} KB RAM, {c['flash_kb']} KB flash, frameworks {', '.join(c['frameworks'])}, platform {c['platform']}."
            return c
    raise KeyError(f"no board with id '{board_id}'. Search with pio_list_boards.")


@guard
def pio_project_init(project_dir: str, board: str, framework: str | None = None, project_options: list[str] | None = None) -> dict[str, Any]:
    check_policy("build")
    path = Path(project_dir).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    args = ["project", "init", "-d", str(path), "-b", board]
    if framework:
        args += ["-O", f"framework={framework}"]
    for opt in project_options or []:
        args += ["-O", opt]
    res = run_pio(args, cwd=str(path), tool="project-init", timeout=600)
    if not res.ok:
        return {"ok": False, "error": "init_failed", "summary": f"pio project init failed (exit {res.returncode}).", "output": res.output[-2000:], "log_path": res.log_path}
    ini = (path / "platformio.ini").read_text(encoding="utf-8") if (path / "platformio.ini").exists() else ""
    return {
        "ok": True,
        "summary": f"Project ready at {path} for board {board}" + (f" with framework {framework}" if framework else "") + ". Put source files in src/ (Arduino: src/main.cpp with setup/loop).",
        "project_dir": str(path),
        "platformio_ini": ini,
        "layout": sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir() if not p.name.startswith(".")),
        "log_path": res.log_path,
    }


@guard
def pio_project_envs(project_dir: str | None = None) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    cfg = project_config(str(path))
    envs = []
    for name in env_names(cfg):
        envs.append(
            {
                "name": name,
                "board": env_setting(cfg, name, "board"),
                "platform": env_setting(cfg, name, "platform"),
                "framework": env_setting(cfg, name, "framework"),
                "monitor_speed": env_setting(cfg, name, "monitor_speed"),
                "monitor_port": env_setting(cfg, name, "monitor_port"),
                "upload_port": env_setting(cfg, name, "upload_port"),
                "upload_protocol": env_setting(cfg, name, "upload_protocol"),
                "lib_deps": env_setting(cfg, name, "lib_deps", []),
                "build_flags": env_setting(cfg, name, "build_flags", []),
                "extends": cfg.get(f"env:{name}", {}).get("extends"),
            }
        )
    defaults = default_envs(cfg)
    return {
        "ok": True,
        "summary": f"{len(envs)} environment(s): {', '.join(e['name'] for e in envs)}. Default: {', '.join(defaults)}.",
        "project_dir": str(path),
        "default_envs": defaults,
        "platformio_section": cfg.get("platformio", {}),
        "envs": envs,
        "platformio_ini_path": str(path / "platformio.ini"),
    }


@guard
def pio_project_metadata(project_dir: str | None = None, env: str | None = None) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    args = ["project", "metadata", "--json-output"]
    if env:
        args += ["-e", env]
    _, data = run_json(args, cwd=str(path), tool="project-metadata", timeout=600)
    out = {}
    for name, m in data.items():
        out[name] = {
            "build_type": m.get("build_type"),
            "defines": m.get("defines", []),
            "include_dirs": (m.get("includes") or {}).get("build", [])[:40],
            "toolchain_include_dirs_count": len((m.get("includes") or {}).get("toolchain", [])),
            "libsource_dirs": m.get("libsource_dirs", []),
            "cc": m.get("cc_path"),
            "cxx": m.get("cxx_path"),
            "cxx_flags": m.get("cxx_flags", []),
            "program_path": m.get("prog_path"),
            "extra": m.get("extra"),
        }
    return {"ok": True, "summary": f"Build metadata for {', '.join(out)} (defines, include paths, compiler, flags).", "project_dir": str(path), "envs": out}


def register(mcp) -> None:
    mcp.tool(name="pio_list_boards", description=(
        "Search PlatformIO's board catalogue (~1,700 boards). Query matches board id, name, MCU, or vendor, "
        "e.g. 'esp32-s3', 'uno', 'STM32F4', 'DOIT'. Returns board ids plus MCU, clock, RAM, flash, and frameworks. "
        "Use the returned `id` in platformio.ini or pio_project_init."
    ))(pio_list_boards)
    mcp.tool(name="pio_board_info", description=(
        "Full details for one board id: MCU, clock, RAM and flash sizes in bytes, supported frameworks, "
        "connectivity, and debug probes. Use it to learn memory limits before writing code."
    ))(pio_board_info)
    mcp.tool(name="pio_project_init", description=(
        "Create a new PlatformIO project (or add an environment to an existing one) with `pio project init`. "
        "Give an absolute project_dir, a board id from pio_list_boards, and optionally a framework "
        "(arduino, espidf, stm32cube, zephyr, ...). project_options are extra platformio.ini keys like "
        "'monitor_speed=115200' or 'lib_deps=bblanchon/ArduinoJson'. Never hand-write platformio.ini for a new project; use this."
    ))(pio_project_init)
    mcp.tool(name="pio_project_envs", description=(
        "Read platformio.ini and list every environment with its board, platform, framework, monitor/upload "
        "settings, lib_deps, and build_flags, plus which envs are default. Cheap; call before building or flashing."
    ))(pio_project_envs)
    mcp.tool(name="pio_project_metadata", description=(
        "Computed build metadata per environment: defines, include paths, compiler paths and flags, library dirs. "
        "Useful for understanding what the compiler actually sees. Triggers platform/toolchain install on first use."
    ))(pio_project_metadata)
