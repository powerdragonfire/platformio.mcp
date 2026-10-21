"""ESP32 flash layout: partition-table validation against the build and the device, and core dump retrieval."""

from __future__ import annotations

import glob
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..core import default_envs, env_setting, guard, monitors, project_config
from ..partitions import TABLE_OFFSET, TABLE_SIZE, Partition, check_partitions, diff_partitions, parse_number, parse_partition_bin, parse_partition_csv
from ..pio import log_dir, resolve_project_dir, run_pio
from ..toolchain import load_toolchain, project_metadata
from .devices import _pick_port
from .project import _all_boards

DEFAULT_CSV = "default.csv"
COREDUMP_INSTALL_HINT = "install the analyzer with `uv pip install esp-coredump` (or run the server as `uvx \"platformio.mcp[coredump]\"`), then call pio_coredump again."


def _esptool(args: list[str], tool: str, timeout: float = 120):
    """Run esptool.py from PlatformIO's own tool-esptoolpy package."""
    return run_pio(["pkg", "exec", "-p", "tool-esptoolpy", "--", "esptool.py", *args], tool=tool, timeout=timeout)


def _env_for(path: Path, env: str | None) -> tuple[dict, str]:
    cfg = project_config(str(path))
    env = env or (default_envs(cfg) or [None])[0]
    if not env:
        raise ValueError("platformio.ini declares no environments.")
    return cfg, env


def _board(cfg: dict, env: str) -> dict[str, Any]:
    board_id = env_setting(cfg, env, "board")
    if not board_id:
        return {}
    try:
        return next((b for b in _all_boards() if b.get("id") == board_id), {"id": board_id})
    except Exception:
        return {"id": board_id}


def _flash_size(cfg: dict, env: str, board: dict) -> tuple[int | None, str]:
    declared = env_setting(cfg, env, "board_upload.flash_size")
    if declared:
        try:
            return parse_number(str(declared).rstrip("Bb")), "board_upload.flash_size"
        except ValueError:
            pass
    if board.get("rom"):
        return int(board["rom"]), f"board {board.get('id')} catalogue"
    return None, "unknown"


def _framework_dir(path: Path, env: str) -> Path | None:
    """The framework-arduinoespressif32 package this env compiles against, from its include paths."""
    try:
        _, meta = project_metadata(str(path), env)
    except Exception:
        meta = {}
    for inc in (meta.get("includes") or {}).get("build", []) or []:
        norm = inc.replace("\\", "/")
        if "/framework-arduinoespressif32" in norm:
            head = norm.split("/framework-arduinoespressif32", 1)[0]
            pkg = norm[len(head) + 1 :].split("/", 1)[0]
            return Path(head) / pkg
    home = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio")) / "packages"
    hits = sorted(glob.glob(str(home / "framework-arduinoespressif32*")))
    return Path(hits[0]) if hits else None


def _find_partition_csv(path: Path, cfg: dict, env: str) -> tuple[Path | None, str]:
    declared = env_setting(cfg, env, "board_build.partitions")
    if declared:
        candidate = (path / declared).resolve() if not Path(declared).is_absolute() else Path(declared)
        if candidate.exists():
            return candidate, "board_build.partitions"
        fw = _framework_dir(path, env)
        if fw and (fw / "tools" / "partitions" / Path(declared).name).exists():
            return fw / "tools" / "partitions" / Path(declared).name, "board_build.partitions (framework file)"
        raise FileNotFoundError(f"board_build.partitions = {declared} but no such file under {path} or the framework's tools/partitions.")
    if (path / "partitions.csv").exists():
        return path / "partitions.csv", "partitions.csv in project root"
    fw = _framework_dir(path, env)
    if fw and (fw / "tools" / "partitions" / DEFAULT_CSV).exists():
        return fw / "tools" / "partitions" / DEFAULT_CSV, f"framework default ({fw.name}/tools/partitions/{DEFAULT_CSV})"
    return None, "not found"


def _refuse_if_port_held(port: str) -> None:
    held = monitors.session_on_port(port)
    if held:
        raise RuntimeError(f"serial monitor session {held.id} holds {port}; call pio_monitor_stop('{held.id}') first, then retry.")


def _read_flash(port: str, offset: int, size: int, tool: str) -> tuple[bytes, Any]:
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
        tmp = fh.name
    try:
        res = _esptool(["--port", port, "read_flash", f"0x{offset:x}", f"0x{size:x}", tmp], tool=tool, timeout=300)
        if not res.ok:
            raise RuntimeError(f"esptool read_flash failed (exit {res.returncode}) on {port}: {res.output[-800:]}")
        return Path(tmp).read_bytes(), res
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _mcu(board: dict) -> str | None:
    mcu = (board.get("mcu") or "").lower()
    return mcu if mcu.startswith("esp32") else None


@guard
def pio_partition_table(project_dir: str | None = None, env: str | None = None, read_device: bool = False, port: str | None = None) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    cfg, env = _env_for(path, env)
    board = _board(cfg, env)
    csv_path, source = _find_partition_csv(path, cfg, env)
    if csv_path is None:
        raise FileNotFoundError("no partition CSV: set board_build.partitions in platformio.ini, add partitions.csv to the project, or install the espressif32 Arduino framework (build once).")
    parts = parse_partition_csv(csv_path.read_text(encoding="utf-8"))
    flash_size, flash_source = _flash_size(cfg, env, board)
    build_dir = path / ".pio" / "build" / env
    fw_bin = build_dir / "firmware.bin"
    app_size = fw_bin.stat().st_size if fw_bin.exists() else None
    issues = check_partitions(parts, flash_size=flash_size, app_size=app_size)
    built_table = build_dir / "partitions.bin"
    if built_table.exists() and built_table.stat().st_mtime < csv_path.stat().st_mtime:
        issues.insert(0, {"severity": "warning", "code": "stale_partitions_bin", "message": f"{built_table.name} in the build dir is older than {csv_path.name}; the next upload would flash the old table.", "fix": "Run pio_build (or pio_clean then pio_build) before uploading."})
    device: dict[str, Any] = {}
    if read_device:
        port, _ = _pick_port(port, str(path), env)
        _refuse_if_port_held(port)
        raw, res = _read_flash(port, TABLE_OFFSET, TABLE_SIZE, tool="partition-read")
        if raw[:2] == b"\xff\xff" or not raw.strip(b"\xff"):
            device = {"port": port, "erased": True, "partitions": [], "diff": [], "log_path": res.log_path}
            issues.insert(0, {"severity": "error", "code": "device_table_erased", "message": f"The flash at 0x{TABLE_OFFSET:x} on {port} holds no partition table (erased or never programmed).", "fix": "Run a full pio_upload; it flashes bootloader, partition table, and app together."})
        else:
            dev_parts = parse_partition_bin(raw)
            diff = diff_partitions(parts, dev_parts)
            device = {"port": port, "erased": False, "partitions": [p.to_dict() for p in dev_parts], "diff": diff, "log_path": res.log_path}
            if diff:
                issues.insert(0, {
                    "severity": "error",
                    "code": "device_table_mismatch",
                    "message": f"The table on {port} differs from {csv_path.name} in {len(diff)} partition(s): " + ", ".join(f"{d['name']} ({d['kind']}" + (": " + "/".join(d["fields"]) if d.get("fields") else "") + ")" for d in diff[:6]) + ". Firmware built for one layout but flashed onto another boots into the wrong offsets or corrupts data partitions silently.",
                    "fix": "Run a full pio_upload (pio run -t upload), which reflashes bootloader + partition table + app. pio_run_target('program') and app-only OTA do not rewrite the table.",
                })
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    table_end = max((p.end for p in parts), default=0)
    parts_desc = ", ".join(f"{p.name} {p.type_name}/{p.subtype_name} @0x{p.offset:x} {p.size // 1024} KB" for p in parts)
    summary = [f"{len(parts)} partition(s) from {csv_path.name} ({source}) for env {env}: {parts_desc}."]
    summary.append(f"Table ends at 0x{table_end:x}" + (f" of 0x{flash_size:x} flash ({flash_source})." if flash_size else "; flash size unknown."))
    if app_size is not None:
        fit = next((i for i in issues if i["code"] in ("app_fits", "app_nearly_full", "app_too_big")), None)
        if fit:
            summary.append(fit["message"])
    else:
        summary.append("firmware.bin not built yet, so app fit was not checked; run pio_build first.")
    if read_device:
        summary.append("Device table matches the CSV." if not device.get("diff") and not device.get("erased") else "DEVICE TABLE MISMATCH, see issues.")
    if errors:
        summary.append(f"{len(errors)} error(s): " + " ".join(e["message"] + " Fix: " + e["fix"] for e in errors[:3]))
    elif warnings:
        summary.append(f"{len(warnings)} warning(s): " + " ".join(w["message"] for w in warnings[:2]))
    else:
        summary.append("No layout problems found.")
    return {
        "ok": not errors,
        "summary": " ".join(summary),
        "env": env,
        "csv_path": str(csv_path),
        "csv_source": source,
        "board": board.get("id"),
        "mcu": board.get("mcu"),
        "flash_size": flash_size,
        "flash_size_source": flash_source,
        "table_end": table_end,
        "firmware_bin": str(fw_bin) if app_size is not None else None,
        "firmware_size": app_size,
        "partitions": [p.to_dict() for p in parts],
        "issues": issues,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "device": device,
    }


def _coredump_partition(parts: list[Partition]) -> Partition:
    for p in parts:
        if p.type_name == "data" and p.subtype_name == "coredump":
            return p
    raise KeyError("this layout has no coredump partition, so nothing was written at crash time. Add 'coredump, data, coredump, , 0x10000' to the partition CSV, rebuild, upload, and reproduce the crash.")


def _analyzer_available() -> bool:
    return importlib.util.find_spec("esp_coredump") is not None


def _run_analyzer(dump: Path, elf: Path, gdb: str | None, chip: str | None, timeout: float = 180) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "esp_coredump"]
    if chip:
        cmd += ["--chip", chip]
    if gdb:
        cmd += ["--gdb", gdb]
    cmd += ["info_corefile", "--core", str(dump), "--core-format", "raw", str(elf)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "analyzer_timeout", "summary": f"esp-coredump did not finish within {timeout:.0f}s.", "command": cmd}
    text = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr.strip() else "")
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "command": cmd, "output": text[-12000:], **parse_coredump_report(text)}


def parse_coredump_report(text: str) -> dict[str, Any]:
    """Pick the crashed task, its backtrace, and the register block out of `esp-coredump info_corefile` output."""
    crashed = None
    reason = None
    backtrace: list[str] = []
    registers: dict[str, str] = {}
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Crashed task handle") or s.startswith("Crashed task:"):
            crashed = s.split(":", 1)[-1].strip()
        elif reason is None and (s.startswith("Panic reason") or s.startswith("Program received signal") or s.startswith("Exception cause") or "panic'ed" in s):
            reason = s.split(":", 1)[-1].strip() if s.startswith(("Panic reason", "Exception cause")) else s
        if s.startswith("==") and s.endswith("=="):
            title = s.strip("= ").upper()
            section = "stack" if title == "CURRENT THREAD STACK" else "regs" if title == "CURRENT THREAD REGISTERS" else None
            continue
        if section == "stack" and s.startswith("#"):
            backtrace.append(s)
        elif section == "regs" and s and " " in s:
            name, _, rest = s.partition(" ")
            registers[name] = rest.strip()
    return {"crashed_task": crashed, "reason": reason, "backtrace": backtrace, "registers": registers}


@guard
def pio_coredump(project_dir: str | None = None, env: str | None = None, port: str | None = None, out_path: str | None = None, analyze: bool = True) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    cfg, env = _env_for(path, env)
    board = _board(cfg, env)
    csv_path, source = _find_partition_csv(path, cfg, env)
    if csv_path is None:
        raise FileNotFoundError("no partition CSV found, so the coredump partition offset is unknown; set board_build.partitions or build once.")
    parts = parse_partition_csv(csv_path.read_text(encoding="utf-8"))
    part = _coredump_partition(parts)
    port, _ = _pick_port(port, str(path), env)
    _refuse_if_port_held(port)
    raw, res = _read_flash(port, part.offset, part.size, tool="coredump-read")
    dest = Path(out_path).expanduser() if out_path else log_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-coredump-{env}.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    base: dict[str, Any] = {
        "env": env,
        "port": port,
        "partition": part.to_dict(),
        "csv_path": str(csv_path),
        "dump_path": str(dest),
        "dump_bytes": len(raw),
        "read_log_path": res.log_path,
    }
    if not raw.strip(b"\xff") or raw[:4] == b"\x00\x00\x00\x00":
        return {
            "ok": False,
            "error": "coredump_empty",
            "summary": f"The coredump partition {part.name} (0x{part.offset:x}, {part.size // 1024} KB) on {port} is erased: no crash has been recorded since the last erase, or core dump to flash is disabled (CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH). Raw bytes saved to {dest}.",
            **base,
        }
    elf = None
    gdb = None
    try:
        tc = load_toolchain(str(path), env)
        if tc.elf_path and Path(tc.elf_path).exists():
            elf = Path(tc.elf_path)
            cand = f"{tc.prefix}gdb"
            gdb = cand if tc.prefix and Path(cand).exists() else None
    except Exception:
        pass
    base.update({"elf_path": str(elf) if elf else None, "gdb": gdb, "analyzer_available": _analyzer_available()})
    header = f"Read {len(raw):,} B core dump from {part.name} (0x{part.offset:x}) on {port}; saved to {dest}."
    if not analyze:
        return {"ok": True, "summary": header + " Not analyzed (analyze=false).", **base}
    if elf is None:
        return {"ok": True, "summary": header + f" Not analyzed: no firmware.elf for env {env}; run pio_build (same source as flashed) then re-run.", **base, "analysis": None}
    if not _analyzer_available():
        return {"ok": True, "summary": header + " The esp-coredump analyzer is not installed; " + COREDUMP_INSTALL_HINT, **base, "analysis": None, "install_hint": COREDUMP_INSTALL_HINT}
    analysis = _run_analyzer(dest, elf, gdb, _mcu(board))
    if analysis.get("ok"):
        bt = analysis.get("backtrace") or []
        summary = header + f" Crashed task: {analysis.get('crashed_task') or 'unknown'}." + (f" {analysis['reason']}." if analysis.get("reason") else "") + (" Top frames: " + " | ".join(bt[:4]) + "." if bt else " No backtrace frames parsed; see output.")
    else:
        summary = header + f" esp-coredump failed (exit {analysis.get('exit_code')}): {analysis.get('output', '')[-400:].strip()}"
    return {"ok": bool(analysis.get("ok")), "summary": summary, **base, "analysis": analysis}


def register(mcp) -> None:
    mcp.tool(name="pio_partition_table", description=(
        "Validate the ESP32 flash layout before it bites. Reads the partition CSV the env uses (board_build.partitions, "
        "project partitions.csv, or the Arduino framework default), checks alignment, overlaps, fit against the chip's flash size, "
        "OTA slot/otadata consistency, nvs/coredump presence, whether the built firmware.bin fits the smallest app slot, and whether "
        "the build dir's partitions.bin is stale. With read_device=true it also reads the live table at 0x8000 over serial (esptool) and "
        "diffs it against the CSV, catching the silent-corruption case where an app-only flash left an old table on the chip. Returns partitions plus issues with fixes."
    ))(pio_partition_table)
    mcp.tool(name="pio_coredump", description=(
        "Pull the ESP32 core dump out of the coredump flash partition after a crash (esptool read_flash) and save it. "
        "When the optional esp-coredump analyzer is installed (`platformio.mcp[coredump]`), it runs `esp-coredump info_corefile` "
        "against the env's firmware.elf with the toolchain's gdb and returns the crashed task, reason, registers, and backtrace. "
        "Reports clearly when the partition is erased (no crash recorded) or the analyzer is missing. Stop monitor sessions on the port first."
    ))(pio_coredump)
