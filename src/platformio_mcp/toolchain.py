"""Find and drive the GNU binutils that ship with a PlatformIO toolchain (addr2line, nm, size).

PlatformIO tells us the C compiler for an environment (`pio project metadata` -> cc_path, e.g.
.../toolchain-xtensa32/bin/xtensa-esp32-elf-gcc). The matching binutils live next to it with the
same target prefix, so `xtensa-esp32-elf-addr2line` and friends can be derived without guessing
package names. The parsers here are pure functions so they are unit-tested on captured output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .core import run_json
from .pio import resolve_project_dir

# --- locating binaries ---------------------------------------------------------------------

CC_SUFFIX_RE = re.compile(r"(?:gcc|cc|clang)(?:-\d+(?:\.\d+)*)?(?:\.exe)?$", re.I)


@dataclass
class Toolchain:
    env: str
    cc_path: str
    elf_path: str | None
    prefix: str  # e.g. "/path/bin/xtensa-esp32-elf-" or "" for host tools
    addr2line: str | None
    nm: str | None
    size: str | None
    platform: str | None = None
    board: str | None = None

    def missing(self) -> list[str]:
        return [name for name in ("addr2line", "nm", "size") if getattr(self, name) is None]


def derive_prefix(cc_path: str) -> str:
    """'/x/bin/xtensa-esp32-elf-gcc' -> '/x/bin/xtensa-esp32-elf-', 'gcc' -> '', 'arm-none-eabi-gcc.exe' -> 'arm-none-eabi-'."""
    name = os.path.basename(cc_path)
    stem = CC_SUFFIX_RE.sub("", name)
    directory = os.path.dirname(cc_path)
    if not stem or stem in ("-",):
        return ""
    if not stem.endswith("-"):
        # e.g. "clang" stripped to "" handled above; "xtensa-esp32-elf-" keeps its dash
        return ""
    return os.path.join(directory, stem) if directory else stem


def _find_binary(prefix: str, name: str, cc_path: str) -> str | None:
    exe = ".exe" if cc_path.lower().endswith(".exe") else ""
    candidate = f"{prefix}{name}{exe}"
    if os.path.sep in candidate or (os.path.altsep and os.path.altsep in candidate):
        return candidate if os.path.exists(candidate) else None
    return shutil.which(candidate) or shutil.which(name)


_META_CACHE: dict[tuple[str, str | None], dict] = {}


def project_metadata(project_dir: str | None, env: str | None) -> tuple[str, dict]:
    """Return (env_name, metadata) for one environment, running `pio project metadata` once per (dir, env)."""
    path = resolve_project_dir(project_dir)
    key = (str(path), env)
    if key not in _META_CACHE:
        args = ["project", "metadata", "--json-output"]
        if env:
            args += ["-e", env]
        _, data = run_json(args, cwd=str(path), tool="project-metadata", timeout=600)
        if not data:
            raise RuntimeError(f"pio project metadata returned nothing for {path}")
        if env and env not in data:
            raise KeyError(f"environment '{env}' not found; available: {', '.join(data)}")
        name = env or next(iter(data))
        _META_CACHE[key] = {"env": name, "meta": data[name]}
    entry = _META_CACHE[key]
    return entry["env"], entry["meta"]


def load_toolchain(project_dir: str | None, env: str | None) -> Toolchain:
    env_name, meta = project_metadata(project_dir, env)
    cc = meta.get("cc_path") or ""
    if not cc:
        raise RuntimeError(f"pio project metadata reports no compiler for env {env_name}; build the project once first.")
    prefix = derive_prefix(cc)
    return Toolchain(
        env=env_name,
        cc_path=cc,
        elf_path=meta.get("prog_path"),
        prefix=prefix,
        addr2line=_find_binary(prefix, "addr2line", cc),
        nm=_find_binary(prefix, "nm", cc),
        size=_find_binary(prefix, "size", cc),
    )


def run_tool(cmd: list[str], timeout: float = 60) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"`{os.path.basename(cmd[0])}` failed (exit {proc.returncode}): {proc.stderr.strip()[-800:]}")
    return proc.stdout


def require_elf(tc: Toolchain) -> Path:
    if not tc.elf_path:
        raise FileNotFoundError(f"no program path known for env {tc.env}; run pio_build first.")
    p = Path(tc.elf_path)
    if not p.exists():
        raise FileNotFoundError(f"{p} does not exist; run pio_build for env {tc.env} first.")
    return p


# --- crash output -> addresses -------------------------------------------------------------

HEX_ADDR = r"0x[0-9a-fA-F]{6,16}"
ESP_BACKTRACE_RE = re.compile(r"Backtrace:\s*((?:" + HEX_ADDR + r":" + HEX_ADDR + r"\s*)+)(\|<-CORRUPTED)?", re.I)
ESP_PAIR_RE = re.compile(r"(" + HEX_ADDR + r"):(" + HEX_ADDR + r")")
# ESP32 register dump: "PC      : 0x400d1234  PS      : 0x00060030  A0      : 0x800d5678"
ESP_REG_RE = re.compile(r"\b(PC|A0|EXCVADDR|EPC1|EPC2|EPC3|EPC4|MEPC|MTVAL|RA)\s*[:=]\s*(" + HEX_ADDR + r")", re.I)
# Cortex-M dumps from CrashCatcher, Zephyr, mbed, Arduino cores: "pc: 0x08001234", "R15 (PC) = 0x08001234", "LR: 0x0800abcd"
ARM_REG_RE = re.compile(r"\b(?:r15|pc|lr|r14|xpsr|psp|msp)\b\s*(?:\(pc\)|\(lr\))?\s*[:=]\s*(" + HEX_ADDR + r")", re.I)
ARM_FAULT_RE = re.compile(r"(HardFault|Hard Fault|MemManage|BusFault|UsageFault|Bus Fault|Usage Fault|Memory Management Fault|\*\*\* ERROR \*\*\*.*stack overflow.*|Stack overflow in task \S+)", re.I)
ESP_CAUSE_RE = re.compile(r"(Guru Meditation Error:.*|abort\(\) was called at PC (" + HEX_ADDR + r").*|assert failed:.*|Task watchdog got triggered.*|Brownout detector was triggered.*|E \(\d+\) task_wdt:.*|Debug exception reason:.*|panic'ed.*|CORRUPT HEAP:.*)", re.I)
RST_REASON_RE = re.compile(r"rst:0x[0-9a-f]+\s*\((\w+)\)", re.I)
ANY_HEX_RE = re.compile(r"\b(" + HEX_ADDR + r")\b")


@dataclass
class Address:
    address: str
    role: str  # pc | backtrace | lr | register | other
    frame: int | None = None
    register: str | None = None

    def to_dict(self) -> dict:
        return {"address": self.address, "role": self.role, "frame": self.frame, "register": self.register}


def _norm(addr: str) -> str:
    return "0x" + addr[2:].lower().lstrip("0").rjust(8, "0") if len(addr) <= 10 else "0x" + addr[2:].lower()


def extract_crash(text: str, include_all_hex: bool = False) -> dict:
    """Pull crash cause, reset reason, and the addresses worth symbolising out of serial output."""
    addresses: list[Address] = []
    seen: set[tuple[str, str]] = set()

    def add(addr: str, role: str, frame: int | None = None, register: str | None = None) -> None:
        a = _norm(addr)
        if register in ("A0", "RA") and role == "lr":
            # Xtensa stores the return address with the register window bits set (0x800d.. -> 0x400d..).
            value = int(a, 16)
            if value & 0xC0000000 == 0x80000000:
                a = f"0x{(value & 0x3FFFFFFF) | 0x40000000:08x}"
        key = (a, role)
        if key in seen:
            return
        seen.add(key)
        addresses.append(Address(a, role, frame, register))

    for m in ESP_REG_RE.finditer(text):
        reg = m.group(1).upper()
        role = "pc" if reg in ("PC", "EPC1", "MEPC") else ("lr" if reg in ("A0", "RA") else "register")
        add(m.group(2), role, register=reg)
    for m in ESP_CAUSE_RE.finditer(text):
        if m.group(2):
            add(m.group(2), "pc", register="abort_pc")
    corrupted = False
    for m in ESP_BACKTRACE_RE.finditer(text):
        corrupted = corrupted or bool(m.group(2))
        for i, pair in enumerate(ESP_PAIR_RE.finditer(m.group(1))):
            add(pair.group(1), "backtrace", frame=i)
    for m in ARM_REG_RE.finditer(text):
        raw = m.group(0).lower()
        role = "pc" if "pc" in raw or "r15" in raw else ("lr" if "lr" in raw or "r14" in raw else "register")
        add(m.group(1), role, register=raw.split(":")[0].split("=")[0].strip())
    if include_all_hex or not addresses:
        for i, m in enumerate(ANY_HEX_RE.finditer(text)):
            add(m.group(1), "other", frame=None)
    causes = [m.group(1).strip() for m in ESP_CAUSE_RE.finditer(text)] + [m.group(1).strip() for m in ARM_FAULT_RE.finditer(text)]
    seen_causes: list[str] = []
    for c in causes:
        if c not in seen_causes:
            seen_causes.append(c)
    resets = [m.group(1) for m in RST_REASON_RE.finditer(text)]
    return {"causes": seen_causes, "reset_reasons": resets, "addresses": addresses, "backtrace_corrupted": corrupted}


# --- addr2line -----------------------------------------------------------------------------

A2L_LINE_RE = re.compile(r"^(?P<addr>0x[0-9a-fA-F]+):\s*(?P<func>.*?)\s+at\s+(?P<file>.*?):(?P<line>\d+|\?)(?:\s+\(discriminator \d+\))?\s*$")
A2L_UNKNOWN_RE = re.compile(r"^(?P<addr>0x[0-9a-fA-F]+):\s*\?\?\s+\?\?:[0?]\s*$")
A2L_INLINE_RE = re.compile(r"^\s*\(inlined by\)\s+(?P<func>.*?)\s+at\s+(?P<file>.*?):(?P<line>\d+|\?)(?:\s+\(discriminator \d+\))?\s*$")


def parse_addr2line(output: str) -> dict[str, dict]:
    """Parse `addr2line -pfiaC` output into {address: {function, file, line, inlined: [...]}}."""
    frames: dict[str, dict] = {}
    current: dict | None = None
    for line in output.splitlines():
        m = A2L_UNKNOWN_RE.match(line)
        if m:
            current = {"address": _norm(m["addr"]), "function": None, "file": None, "line": None, "resolved": False, "inlined": []}
            frames[current["address"]] = current
            continue
        m = A2L_LINE_RE.match(line)
        if m:
            func = m["func"].strip()
            file = m["file"].strip()
            resolved = func != "??" and file != "??"
            current = {
                "address": _norm(m["addr"]),
                "function": func if func != "??" else None,
                "file": file if file != "??" else None,
                "line": int(m["line"]) if m["line"].isdigit() and m["line"] != "0" else None,
                "resolved": resolved,
                "inlined": [],
            }
            frames[current["address"]] = current
            continue
        m = A2L_INLINE_RE.match(line)
        if m and current is not None:
            current["inlined"].append({"function": m["func"].strip(), "file": m["file"].strip(), "line": int(m["line"]) if m["line"].isdigit() else None})
    return frames


def symbolize(tc: Toolchain, addresses: list[str]) -> dict[str, dict]:
    if not addresses:
        return {}
    if not tc.addr2line:
        raise FileNotFoundError(f"addr2line not found next to {tc.cc_path}; install the toolchain by building once.")
    elf = require_elf(tc)
    out = run_tool([tc.addr2line, "-pfiaC", "-e", str(elf), *dict.fromkeys(addresses)])
    return parse_addr2line(out)


# --- size / nm -----------------------------------------------------------------------------

RAM_SECTION_RE = re.compile(r"^\.(?:dram\d*\.bss|bss|noinit|dram\d*\.noinit|iram\d*\.(?:text|vectors|bss|data)|rtc[._]|ccm|sram|stack|heap|tcm|dtcm|itcm|ram|ext_ram|psram|lp_ram|lpram)", re.I)
BOTH_SECTION_RE = re.compile(r"^\.(?:dram\d*\.data|data|sdata|tdata|ramfunc|fast|iram\d*\.text)$", re.I)
FLASH_SECTION_RE = re.compile(r"^\.(?:flash\.|text|rodata|irom|drom|isr_vector|ARM\.exidx|ARM\.extab|init|fini|ctors|dtors|eh_frame|preinit_array|init_array|fini_array|vectors|srodata|gnu\.linkonce|got)", re.I)


def classify_section(name: str, addr: int) -> str:
    if addr == 0:
        return "debug"
    if BOTH_SECTION_RE.match(name):
        return "both"
    if RAM_SECTION_RE.match(name):
        return "ram"
    if FLASH_SECTION_RE.match(name):
        return "flash"
    return "other"


def parse_size_sections(output: str) -> list[dict]:
    """Parse `size -A` (SysV format) into loaded sections with a flash/ram/both classification."""
    rows: list[dict] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].startswith(".") or not parts[1].isdigit() or not parts[2].isdigit():
            continue
        name, size, addr = parts[0], int(parts[1]), int(parts[2])
        region = classify_section(name, addr)
        if region == "debug" or size == 0:
            continue
        rows.append({"section": name, "size": size, "address": f"0x{addr:08x}", "region": region})
    rows.sort(key=lambda r: -r["size"])
    return rows


def parse_size_totals(output: str) -> dict | None:
    """Parse `size -B` (Berkeley format): text, data, bss."""
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and all(p.isdigit() for p in parts[:4]):
            text, data, bss = (int(p) for p in parts[:3])
            return {"text": text, "data": data, "bss": bss, "flash_estimate": text + data, "ram_estimate": data + bss}
    return None


NM_LINE_RE = re.compile(r"^(?P<addr>[0-9a-fA-F]+)\s+(?P<size>[0-9a-fA-F]+)\s+(?P<type>[A-Za-z?])\s+(?P<name>.*?)(?:\t(?P<file>.+?):(?P<line>\d+))?\s*$")
NM_KIND = {"t": "code", "d": "data", "b": "bss", "r": "rodata", "w": "weak", "v": "weak", "g": "data", "s": "bss", "c": "common"}


def parse_nm(output: str) -> list[dict]:
    """Parse `nm -S -C -l --size-sort` into rows with size, kind, demangled name, and source location."""
    rows: list[dict] = []
    for line in output.splitlines():
        m = NM_LINE_RE.match(line)
        if not m:
            continue
        size = int(m["size"], 16)
        if size == 0:
            continue
        t = m["type"]
        rows.append(
            {
                "name": m["name"].strip(),
                "size": size,
                "kind": NM_KIND.get(t.lower(), "other"),
                "type": t,
                "address": "0x" + m["addr"].lower(),
                "file": m["file"],
                "line": int(m["line"]) if m["line"] else None,
            }
        )
    rows.sort(key=lambda r: -r["size"])
    return rows


def group_by_file(symbols: list[dict], project_dir: str | None = None) -> list[dict]:
    totals: dict[str, dict] = {}
    for s in symbols:
        f = s.get("file") or "<no debug info>"
        if project_dir and f.startswith(project_dir):
            f = f[len(project_dir) :].lstrip("/\\")
        entry = totals.setdefault(f, {"file": f, "size": 0, "symbols": 0, "code": 0, "data": 0, "bss": 0, "rodata": 0})
        entry["size"] += s["size"]
        entry["symbols"] += 1
        if s["kind"] in entry:
            entry[s["kind"]] += s["size"]
    return sorted(totals.values(), key=lambda r: -r["size"])
