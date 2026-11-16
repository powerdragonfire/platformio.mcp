"""RISC-V panic unwinding: block extraction, GDB bt parsing, stack-scan fallback, and the decode tool path."""

import os
import subprocess
import sys
import time

import pytest

from platformio_mcp import toolchain, unwind
from platformio_mcp.tools import analysis

# Shape of an ESP32-C6 Arduino panic (registers and stack written for these tests).
C6_PANIC = """\
boot
Guru Meditation Error: Core  0 panic'ed (Store access fault). Exception was unhandled.

Core  0 register dump:
MEPC    : 0x42000a1c  RA      : 0x42000a3a  SP      : 0x40816f60  GP      : 0x40811e14
TP      : 0x40817100  T0      : 0x4002a69c  T1      : 0x00000000  T2      : 0x00000000
S0/FP   : 0x00000000  S1      : 0x00000000  A0      : 0x00000000  A1      : 0x0000002a
A2      : 0x00000001  A3      : 0x00000000  A4      : 0x00000000  A5      : 0x0000002a
MSTATUS : 0x00001881  MTVEC   : 0x40800001  MCAUSE  : 0x00000007  MTVAL   : 0x00000000
MHARTID : 0x00000000

Stack memory:
40816f60: 0x00000000 0x00000000 0x00000000 0x42000a5c 0x00000000 0x00000000 0x00000000 0x42000a90
40816f80: 0xa5a5a5a5 0x40816fa0 0x42003b2e 0x00000000 0x00000000 0x00000000 0x00000000 0x00000000

ELF file SHA256: 4faf3099c
Rebooting...
"""


def test_arch_detection():
    assert unwind.arch_for_mcu("esp32c6") == "riscv"
    assert unwind.arch_for_mcu("ESP32P4") == "riscv"
    assert unwind.arch_for_mcu("esp32s31") == "riscv"
    assert unwind.arch_for_mcu("esp32s3") == "xtensa"
    assert unwind.arch_for_mcu("stm32f103c8") is None
    assert unwind.arch_for_prefix("/p/toolchain-riscv32-esp/bin/riscv32-esp-elf-") == "riscv"
    assert unwind.arch_for_prefix("/p/bin/xtensa-esp32s3-elf-") == "xtensa"
    assert unwind.arch_for_prefix("arm-none-eabi-") == "arm"
    assert unwind.arch_for_prefix("") is None
    assert unwind.arch_for_text(C6_PANIC) == "riscv"
    assert unwind.arch_for_text("PC      : 0x400d1ff4  EXCVADDR: 0x00000000") == "xtensa"
    assert unwind.arch_for_text("hello") is None


def test_riscv_a0_is_not_a_return_address():
    crash = toolchain.extract_crash(C6_PANIC)
    assert crash["arch"] == "riscv"
    regs = {a.register: a.role for a in crash["addresses"] if a.register}
    assert regs["MEPC"] == "pc" and regs["RA"] == "lr"
    assert "A0" not in regs


def test_xtensa_a0_still_a_return_address():
    crash = toolchain.extract_crash("PC      : 0x400d1ff4  A0      : 0x800d2010  EXCVADDR: 0x00000000")
    a0 = next(a for a in crash["addresses"] if a.register == "A0")
    assert a0.role == "lr" and a0.address == "0x400d2010"


def test_extract_panic_block_single_core():
    b = unwind.extract_panic_block(C6_PANIC)
    assert b.core == 0 and b.cores_seen == [0]
    assert b.regs["MEPC"] == 0x42000A1C and b.regs["S0/FP"] == 0 and b.regs["MHARTID"] == 0
    assert b.stack_base == 0x40816F60 and len(b.stack_words) == 16
    rendered = b.render()
    assert rendered.startswith("Core  0 register dump:\nMEPC    : 0x42000a1c")
    assert "Stack memory:\n40816f60: 0x00000000" in rendered


def test_render_is_parseable_by_esp_idf_panic_decoder():
    from esp_idf_panic_decoder.gdb_panic_server import parse_idf_riscv_panic_output

    info = parse_idf_riscv_panic_output(unwind.extract_panic_block(C6_PANIC).render())
    assert info.regs["MEPC"] == 0x42000A1C
    assert info.stack_base_addr == 0x40816F60 and len(info.stack_data) == 64


def test_extract_panic_block_picks_the_core_that_panicked():
    text = C6_PANIC.replace("Core  0 panic'ed", "Core  1 panic'ed").replace(
        "Core  0 register dump:", "Core  0 register dump:\nMEPC    : 0x40000000  RA      : 0x40000004\n\nCore  1 register dump:", 1
    )
    b = unwind.extract_panic_block(text)
    assert b.cores_seen == [0, 1]
    assert b.core == 1 and b.regs["MEPC"] == 0x42000A1C


def test_extract_panic_block_keeps_last_crash_of_a_boot_loop():
    second = C6_PANIC.replace("0x42000a1c", "0x42000b00")
    b = unwind.extract_panic_block(C6_PANIC + second)
    assert b.regs["MEPC"] == 0x42000B00


EARLIER_STACKLESS = """\
Guru Meditation Error: Core  0 panic'ed (Illegal instruction). Exception was unhandled.

Core  0 register dump:
MEPC    : 0x40000000  RA      : 0x40000004  SP      : 0x40810000  GP      : 0x40811e14

ELF file SHA256: 4faf3099c
Rebooting...
"""


def test_extract_panic_block_ignores_registers_of_an_earlier_stackless_panic():
    b = unwind.extract_panic_block(EARLIER_STACKLESS + C6_PANIC)
    assert b.cores_seen == [0]
    assert b.regs["MEPC"] == 0x42000A1C and b.regs["SP"] == 0x40816F60
    assert "Illegal instruction" not in b.text and "Store access fault" in b.text


def test_extract_panic_block_splits_dumps_without_guru_lines_at_a_repeated_core():
    strip = lambda t: "\n".join(l for l in t.splitlines() if "Guru Meditation" not in l)
    b = unwind.extract_panic_block(strip(EARLIER_STACKLESS) + "\n" + strip(C6_PANIC))
    assert b.cores_seen == [0] and b.regs["MEPC"] == 0x42000A1C


def test_extract_panic_block_stops_at_address_gap():
    text = C6_PANIC.replace("40816f80:", "40817000:")
    assert len(unwind.extract_panic_block(text).stack_words) == 8


def test_extract_panic_block_needs_stack_and_mepc():
    assert unwind.extract_panic_block("Core  0 register dump:\nMEPC    : 0x42000a1c\n") is None
    assert unwind.extract_panic_block("Stack memory:\n40816f60: 0x00000000\n") is None


GDB_BT = """\
0x42000a1c in level3 (...) at src/main.cpp:3
3\t__attribute__((noinline)) void level3(volatile int *p) { *p = 42; }
#0  level3 (p=...) at src/main.cpp:3
#1  0x42000a3a in level2 (p=...) at src/main.cpp:4
#2  0x42000a5c in level1 () at src/main.cpp:5
#3  0x42000a90 in setup () at src/main.cpp:11
#4  0x42003b2e in loopTask (...) at /fw/cores/esp32/main.cpp:59
#5  0x4080a5c4 in ?? ()
Backtrace stopped: previous frame identical to this frame (corrupt stack?)
"""


def test_parse_gdb_bt():
    frames, stopped = unwind.parse_gdb_bt(GDB_BT)
    assert [f["function"] for f in frames] == ["level3", "level2", "level1", "setup", "loopTask", None]
    assert frames[0]["address"] is None and frames[1]["address"] == "0x42000a3a"
    assert frames[3] == {"frame": 3, "address": "0x42000a90", "function": "setup", "file": "src/main.cpp", "line": 11, "resolved": True}
    assert frames[5]["resolved"] is False
    assert stopped.startswith("previous frame identical")


def test_parse_gdb_bt_cpp_names():
    frames, _ = unwind.parse_gdb_bt("#0  0x42001000 in std::function<void ()>::operator() (...) at /x/std_function.h:591\n")
    assert frames[0]["function"] == "std::function<void ()>::operator()" and frames[0]["line"] == 591


def test_gdb_command_runs_the_decoder_module():
    cmd = unwind.gdb_command("/g/riscv32-esp-elf-gdb", "/p/firmware.elf", "/tmp/panic.txt", python="/py/python")
    assert cmd[:4] == ["/g/riscv32-esp-elf-gdb", "--batch", "-n", "/p/firmware.elf"]
    assert 'target remote | "/py/python" -m platformio_mcp.panic_stub "/tmp/panic.txt" --deadline 90' in cmd
    assert cmd[-1] == "bt" and "set print frame-arguments none" in cmd


def _write_panic_file(tmp_path):
    path = tmp_path / "panic.txt"
    path.write_text(unwind.extract_panic_block(C6_PANIC).render())
    return path


def test_panic_stub_exits_when_stdin_closes(tmp_path):
    # esp-idf-panic-decoder 1.5 spins forever on EOF; the stub must exit once GDB is gone.
    proc = subprocess.run([sys.executable, "-m", "platformio_mcp.panic_stub", str(_write_panic_file(tmp_path))], stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
    assert proc.returncode == 0


def test_panic_stub_exits_at_its_deadline(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "platformio_mcp.panic_stub", str(_write_panic_file(tmp_path)), "--deadline", "1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert proc.wait(timeout=20) == 2  # os._exit(2) from the deadline timer
    finally:
        proc.kill()
        proc.stdin.close()
        proc.stdout.close()


@pytest.mark.skipif(os.name == "nt", reason="fake GDB is a POSIX script")
def test_gdb_timeout_leaves_no_decoder_running(tmp_path):
    # A fake GDB that, like the real one, starts the `target remote |` command on a pipe, then hangs.
    pid_file = tmp_path / "stub.pid"
    fake_gdb = tmp_path / "fake-gdb"
    fake_gdb.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        "remote = next(a for a in sys.argv if a.startswith('target remote | '))[len('target remote | '):]\n"
        "p = subprocess.Popen(remote, shell=True, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(600)\n"
    )
    fake_gdb.chmod(0o755)
    with pytest.raises(RuntimeError, match="did not finish"):
        unwind.unwind_with_gdb(str(fake_gdb), "/p/firmware.elf", unwind.extract_panic_block(C6_PANIC), timeout=3)
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.kill(pid, 9)
    pytest.fail("decoder stub still running after GDB was killed")


def test_stack_scan_keeps_code_words_in_order():
    b = unwind.extract_panic_block(C6_PANIC)
    assert unwind.stack_scan(b, [(0x42000000, 0x42100000)]) == ["0x42000a5c", "0x42000a90", "0x42003b2e"]
    assert unwind.stack_scan(b, [(0x42000000, 0x42100000)], limit=1) == ["0x42000a5c"]


# --- the tool path ---------------------------------------------------------------------------


@pytest.fixture
def riscv_toolchain(monkeypatch, tmp_path):
    elf = tmp_path / ".pio" / "build" / "c6" / "firmware.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"\x7fELF")
    tc = toolchain.Toolchain(
        env="c6",
        cc_path="/tc/bin/riscv32-esp-elf-gcc",
        elf_path=str(elf),
        prefix="/tc/bin/riscv32-esp-elf-",
        addr2line="/tc/bin/riscv32-esp-elf-addr2line",
        nm=None,
        size=None,
    )
    monkeypatch.setattr(analysis, "load_toolchain", lambda project_dir, env: tc)
    table = {"0x42000a1c": "level3(int volatile*) at /p/src/main.cpp:3", "0x42000a3a": "level2(int volatile*) at /p/src/main.cpp:4", "0x42000a5c": "level1() at /p/src/main.cpp:5"}

    def run_tool(cmd, timeout=60):
        addrs = cmd[cmd.index("-e") + 2 :]
        return "\n".join(f"{a}: {table.get(a, '?? ??:0')}" for a in addrs) + "\n"

    monkeypatch.setattr(toolchain, "run_tool", run_tool)
    monkeypatch.setattr(analysis, "code_ranges", lambda elf: [(0x42000000, 0x42100000)])
    return tmp_path


def test_decode_riscv_with_gdb(monkeypatch, riscv_toolchain):
    monkeypatch.setattr(analysis, "find_gdb", lambda cc: "/g/riscv32-esp-elf-gdb")
    seen = {}

    def fake_unwind(gdb, elf, block, timeout=60):
        seen["block"] = block
        frames, stopped = unwind.parse_gdb_bt(GDB_BT)
        return {"frames": frames, "stopped": stopped, "output": GDB_BT}

    monkeypatch.setattr(analysis, "unwind_with_gdb", fake_unwind)
    r = analysis.pio_decode_backtrace(project_dir=str(riscv_toolchain), env="c6", text=C6_PANIC)
    assert r["ok"] is True and r["method"] == "gdb_unwind" and r["arch"] == "riscv"
    assert seen["block"].regs["MEPC"] == 0x42000A1C
    assert r["frames"][0]["address"] == "0x42000a1c" and r["frames"][0]["register"] == "MEPC"
    assert [f["function"] for f in r["frames"][:4]] == ["level3", "level2", "level1", "setup"]
    assert r["backtrace_corrupted"] is True
    assert "GDB unwound 6 frame(s), 5 with source: level3 (main.cpp:3) <- level2" in r["summary"]
    assert "Store access fault" in r["summary"]


def test_decode_riscv_cause_comes_from_the_unwound_panic(monkeypatch, riscv_toolchain):
    monkeypatch.setattr(analysis, "find_gdb", lambda cc: None)
    first = "rst:0x1 (POWERON),boot:0xc\n" + C6_PANIC.replace("Store access fault", "Illegal instruction")
    r = analysis.pio_decode_backtrace(project_dir=str(riscv_toolchain), env="c6", text=first + "rst:0xc (SW_CPU),boot:0xc\n" + C6_PANIC)
    assert [c for c in r["causes"] if "Illegal" in c] == []
    assert "Store access fault" in r["causes"][0] and "Cause: Guru Meditation Error: Core  0 panic'ed (Store access fault)" in r["summary"]
    assert r["reset_reasons"] == ["SW_CPU"]


def test_decode_riscv_without_gdb_falls_back_to_stack_scan(monkeypatch, riscv_toolchain):
    monkeypatch.setattr(analysis, "find_gdb", lambda cc: None)
    r = analysis.pio_decode_backtrace(project_dir=str(riscv_toolchain), env="c6", text=C6_PANIC)
    assert r["method"] == "stack_scan" and r["ok"] is True
    roles = [(f["role"], f["address"]) for f in r["frames"]]
    assert roles[:2] == [("pc", "0x42000a1c"), ("lr", "0x42000a3a")]
    assert ("stack_scan", "0x42000a5c") in roles
    assert all(f["register"] != "A0" for f in r["frames"])
    assert "tool-riscv32-esp-elf-gdb" in r["summary"] and "stale return addresses" in r["summary"]


def test_decode_riscv_gdb_failure_falls_back(monkeypatch, riscv_toolchain):
    monkeypatch.setattr(analysis, "find_gdb", lambda cc: "/g/riscv32-esp-elf-gdb")

    def boom(gdb, elf, block, timeout=60):
        raise RuntimeError("GDB returned no frames (exit 1): Remote connection closed")

    monkeypatch.setattr(analysis, "unwind_with_gdb", boom)
    r = analysis.pio_decode_backtrace(project_dir=str(riscv_toolchain), env="c6", text=C6_PANIC)
    assert r["method"] == "stack_scan" and "Remote connection closed" in r["unwind_error"]
    assert "GDB failed" in r["summary"] and "pio pkg install" not in r["summary"]


def test_decode_riscv_registers_only(monkeypatch, riscv_toolchain):
    text = C6_PANIC.split("Stack memory:")[0]
    r = analysis.pio_decode_backtrace(project_dir=str(riscv_toolchain), env="c6", text=text)
    assert r["method"] == "addr2line" and r["arch"] == "riscv"
    assert "no 'Stack memory:' dump" in r["summary"]
    assert all(f["register"] != "A0" for f in r["frames"])
