"""Unwind RISC-V Espressif panic output into call frames.

RISC-V Espressif chips (ESP32-C2/C3/C5/C6/C61/H2/H4/P4/S31) print a register dump and a raw
"Stack memory:" dump on a panic, with no "Backtrace:" line. GDB can rebuild the call chain from those
two pieces using the call-frame information in the ELF. Espressif's `esp-idf-panic-decoder` package
serves the dumped registers and stack to GDB as a fake remote target; this module cuts the panic block
out of serial output, runs the toolchain's GDB against it, and parses `bt`.

When GDB is not installed, `stack_scan` symbolizes every stack word that points into an executable
section of the ELF. Those frames include stale return addresses, so callers label them as such.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

RISCV_MCU_RE = re.compile(r"^esp32(?:c\d+|h\d+|p\d+|s31)$", re.I)
XTENSA_MCU_RE = re.compile(r"^(?:esp32|esp32s2|esp32s3|esp8266)$", re.I)

REG_HEADER_RE = re.compile(r"Core\s+(\d+)\s+register dump:", re.I)
REG_PAIR_RE = re.compile(r"\b([A-Z][A-Z0-9_]*(?:/[A-Z0-9]+)?)\s*:\s*0x([0-9a-fA-F]{1,8})\b")
STACK_HEADER_RE = re.compile(r"^\s*Stack memory:\s*$", re.I)
STACK_LINE_RE = re.compile(r"^\s*(?:0x)?([0-9a-fA-F]{8}):((?:\s+0x[0-9a-fA-F]{8})+)\s*$")
PANIC_CORE_RE = re.compile(r"Core\s+(\d+)\s+panic'ed", re.I)


def arch_for_mcu(mcu: str | None) -> str | None:
    """'esp32c6' -> 'riscv', 'esp32s3' -> 'xtensa', anything else -> None."""
    if not mcu:
        return None
    if RISCV_MCU_RE.match(mcu):
        return "riscv"
    if XTENSA_MCU_RE.match(mcu):
        return "xtensa"
    return None


def arch_for_prefix(prefix: str | None) -> str | None:
    """Toolchain prefix '/x/bin/riscv32-esp-elf-' -> 'riscv'; 'xtensa-esp32-elf-' -> 'xtensa'; 'arm-none-eabi-' -> 'arm'."""
    name = Path(prefix or "").name.lower()
    if name.startswith("riscv"):
        return "riscv"
    if name.startswith("xtensa"):
        return "xtensa"
    if name.startswith("arm"):
        return "arm"
    return None


def arch_for_text(text: str) -> str | None:
    """Guess the CPU family from panic output alone: MEPC is RISC-V only, EXCVADDR/EPC1 are Xtensa only."""
    if re.search(r"\bMEPC\s*:", text):
        return "riscv"
    if re.search(r"\b(?:EXCVADDR|EPC1|EXCCAUSE)\s*:", text) or re.search(r"Backtrace:\s*0x[0-9a-fA-F]+:0x", text):
        return "xtensa"
    return None


@dataclass
class PanicBlock:
    core: int
    cores_seen: list[int]
    regs: dict[str, int]
    stack_base: int
    stack_words: list[int] = field(default_factory=list)

    def render(self) -> str:
        """The block in the exact shape esp-idf-panic-decoder parses: one register dump, lowercase hex stack lines."""
        names = list(self.regs)
        reg_lines = ["  ".join(f"{n:<7} : 0x{self.regs[n]:08x}" for n in names[i : i + 4]) for i in range(0, len(names), 4)]
        stack_lines = []
        for i in range(0, len(self.stack_words), 8):
            chunk = self.stack_words[i : i + 8]
            stack_lines.append(f"{self.stack_base + i * 4:08x}: " + " ".join(f"0x{w:08x}" for w in chunk))
        return "\n".join([f"Core  {self.core} register dump:", *reg_lines, "", "Stack memory:", *stack_lines, ""])


def extract_panic_block(text: str) -> PanicBlock | None:
    """Cut the last RISC-V register dump + stack dump out of serial output.

    Serial buffers often hold several crashes (boot loops) and multi-core chips print one register dump
    per core. The decoder package accepts exactly one dump, so this keeps the last crash and, within it,
    the core named in "Core N panic'ed" (or the first dump when no Guru Meditation line was printed).
    """
    lines = text.splitlines()
    stack_idx = next((i for i in range(len(lines) - 1, -1, -1) if STACK_HEADER_RE.match(lines[i])), None)
    if stack_idx is None:
        return None
    words: list[int] = []
    base = None
    expected = None
    for line in lines[stack_idx + 1 :]:
        m = STACK_LINE_RE.match(line)
        if not m:
            if words:
                break
            continue
        addr = int(m.group(1), 16)
        if expected is not None and addr != expected:
            break  # a gap means the dump was cut or interleaved; keep the contiguous part
        row = [int(w, 16) for w in m.group(2).split()]
        base = addr if base is None else base
        words += row
        expected = addr + 4 * len(row)
    if base is None:
        return None
    prev_stack = next((i for i in range(stack_idx - 1, -1, -1) if STACK_HEADER_RE.match(lines[i])), -1)
    region = lines[prev_stack + 1 : stack_idx]
    dumps: list[tuple[int, dict[str, int]]] = []
    current: dict[str, int] | None = None
    for line in region:
        h = REG_HEADER_RE.search(line)
        if h:
            current = {}
            dumps.append((int(h.group(1)), current))
            continue
        if current is None:
            continue
        pairs = REG_PAIR_RE.findall(line)
        if pairs:
            for name, value in pairs:
                current[name.upper()] = int(value, 16)
        elif current:
            current = None  # first non-register line after the dump ends it
    dumps = [(core, regs) for core, regs in dumps if "MEPC" in regs]
    if not dumps:
        return None
    panic_core = None
    for line in region:
        m = PANIC_CORE_RE.search(line)
        if m:
            panic_core = int(m.group(1))
    chosen = next((d for d in dumps if d[0] == panic_core), dumps[0])
    return PanicBlock(core=chosen[0], cores_seen=[c for c, _ in dumps], regs=chosen[1], stack_base=base, stack_words=words)


# --- GDB -----------------------------------------------------------------------------------

# "#1  0x4200002e in level2 (p=...) at src/main.cpp:4"; with frame-arguments none GDB prints "name=..." per argument.
BT_LINE_RE = re.compile(
    r"^#(?P<n>\d+)\s+(?:(?P<addr>0x[0-9a-fA-F]+)\s+in\s+)?(?P<func>.+?)\s+\((?P<args>[^()]*(?:\([^()]*\)[^()]*)*)\)"
    r"(?:\s+at\s+(?P<file>.+):(?P<line>\d+)|\s+from\s+\S+)?\s*$"
)
BT_STOP_RE = re.compile(r"^Backtrace stopped:\s*(.*)$")


def parse_gdb_bt(output: str) -> tuple[list[dict], str | None]:
    """Parse `bt` (with `set print frame-arguments none`) into frames plus GDB's stop reason, if any."""
    frames: list[dict] = []
    stopped = None
    for line in output.splitlines():
        m = BT_LINE_RE.match(line.strip())
        if m:
            func = m["func"].strip()
            frames.append(
                {
                    "frame": int(m["n"]),
                    "address": m["addr"].lower() if m["addr"] else None,
                    "function": None if func == "??" else func,
                    "file": m["file"],
                    "line": int(m["line"]) if m["line"] else None,
                    "resolved": func != "??" and bool(m["file"]),
                }
            )
            continue
        s = BT_STOP_RE.match(line.strip())
        if s:
            stopped = s.group(1).strip()
    return frames, stopped


def gdb_command(gdb: str, elf: str, panic_file: str, python: str | None = None) -> list[str]:
    python = python or sys.executable
    if os.name == "nt":  # GDB parses the remote command line itself and eats single backslashes
        python = python.replace("\\", "\\\\")
        panic_file = panic_file.replace("\\", "\\\\")
    return [
        gdb,
        "--batch",
        "-n",
        elf,
        "-ex",
        "set confirm off",
        "-ex",
        "set print frame-arguments none",
        "-ex",
        f'target remote | "{python}" -m esp_idf_panic_decoder "{panic_file}"',
        "-ex",
        "bt",
    ]


def unwind_with_gdb(gdb: str, elf: str, block: PanicBlock, timeout: float = 60) -> dict:
    """Run GDB against the panic block. Returns {frames, stopped, output} or raises RuntimeError."""
    fd, panic_file = tempfile.mkstemp(prefix="pio-mcp-panic-", suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(block.render())
        cmd = gdb_command(gdb, elf, panic_file)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"GDB did not finish unwinding within {timeout:.0f}s") from exc
        output = (proc.stdout or "") + (proc.stderr or "")
        frames, stopped = parse_gdb_bt(proc.stdout or "")
        if not frames:
            raise RuntimeError(f"GDB returned no frames (exit {proc.returncode}): {output.strip()[-600:]}")
        return {"frames": frames, "stopped": stopped, "output": output[-6000:]}
    finally:
        try:
            os.unlink(panic_file)
        except OSError:
            pass


# --- stack scan fallback ---------------------------------------------------------------------


def code_ranges(elf: str) -> list[tuple[int, int]]:
    """Address ranges of the ELF's executable, loaded sections."""
    from elftools.elf.constants import SH_FLAGS
    from elftools.elf.elffile import ELFFile

    ranges = []
    with open(elf, "rb") as f:
        for sec in ELFFile(f).iter_sections():
            if sec["sh_addr"] and sec["sh_size"] and sec["sh_flags"] & SH_FLAGS.SHF_EXECINSTR:
                ranges.append((sec["sh_addr"], sec["sh_addr"] + sec["sh_size"]))
    return sorted(ranges)


def stack_scan(block: PanicBlock, ranges: list[tuple[int, int]], limit: int = 24) -> list[str]:
    """Stack words that point into code, in stack order, de-duplicated. Some are stale return addresses."""
    out: list[str] = []
    for w in block.stack_words:
        if any(lo <= w < hi for lo, hi in ranges):
            a = f"0x{w:08x}"
            if a not in out:
                out.append(a)
                if len(out) >= limit:
                    break
    return out
