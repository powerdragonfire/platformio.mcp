"""End-to-end checks against a real PlatformIO install. Run with: uv run pytest -m integration"""

import shutil
from pathlib import Path

import pytest

from platformio_mcp.pio import find_pio
from platformio_mcp.tools.build import pio_build, pio_clean
from platformio_mcp.tools.devices import pio_list_devices
from platformio_mcp.tools.project import (
    pio_board_info,
    pio_list_boards,
    pio_project_envs,
    pio_project_init,
)
from platformio_mcp.tools.quality import pio_check, pio_test
from platformio_mcp.tools.system import pio_system_info

pytestmark = [pytest.mark.integration, pytest.mark.skipif(find_pio() is None, reason="PlatformIO not installed")]

FIXTURE = Path(__file__).parent / "projects" / "native-hello"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_LOG_DIR", str(tmp_path / "logs"))
    dst = tmp_path / "native-hello"
    shutil.copytree(FIXTURE, dst, ignore=shutil.ignore_patterns(".pio"))
    return str(dst)


def test_system_and_boards():
    info = pio_system_info()
    assert info["ok"] and info["core_version"]
    boards = pio_list_boards("esp32doit")
    assert any(b["id"] == "esp32doit-devkit-v1" for b in boards["boards"])
    assert pio_board_info("esp32doit-devkit-v1")["flash_bytes"] == 4194304
    assert pio_list_devices()["ok"]


def test_build_test_check_on_native(project):
    envs = pio_project_envs(project)
    assert envs["envs"][0]["platform"] == "native"

    ok = pio_build(project)
    assert ok["ok"] and ok["status"] == "success", ok["summary"]

    main = Path(project) / "src" / "main.cpp"
    original = main.read_text()
    main.write_text(original + "\nint broken( { return 1 }\n")
    bad = pio_build(project)
    assert not bad["ok"] and bad["error_count"] >= 1
    assert bad["errors"][0]["file"].endswith("main.cpp") and bad["errors"][0]["line"] == 7
    main.write_text(original)

    tests = pio_test(project)
    assert tests["total"] >= 2 and tests["failed"] == 1
    assert "Expected 6 Was 5" in tests["summary"]

    main.write_text(original + "\nint leak(){ int *p = new int[4]; int x; return p[0] + x; }\n")
    check = pio_check(project, severity="high")
    assert check["by_severity"]["high"] >= 1
    assert any(d["id"] == "memleak" for d in check["defects"])
    main.write_text(original)

    assert pio_clean(project)["ok"]


def test_project_init(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_LOG_DIR", str(tmp_path / "logs"))
    r = pio_project_init(str(tmp_path / "new"), board="uno", framework="arduino", project_options=["monitor_speed=115200"])
    assert r["ok"], r
    assert "board = uno" in r["platformio_ini"] and "monitor_speed = 115200" in r["platformio_ini"]


RISCV_FIXTURE = Path(__file__).parent / "projects" / "riscv-crash"


def _riscv_frames_from_elf(tc, elf):
    """MEPC inside level3 and RA just after level2's call to level3, read from the disassembly."""
    import re
    import subprocess

    dis = subprocess.run([tc.prefix + "objdump", "-d", "-C", str(elf)], capture_output=True, text=True, check=True).stdout
    level3 = re.search(r"^([0-9a-f]{8}) <level3\(int volatile\*\)>:", dis, re.M)
    body = dis[dis.index("<level2(int volatile*)>:") :]
    lines = body.splitlines()[1:]
    call = next(i for i, ln in enumerate(lines) if "<level3(int volatile*)>" in ln)
    ra = re.match(r"\s*([0-9a-f]+):", lines[call + 1]).group(1)
    return int(level3.group(1), 16), int(ra, 16)


def test_riscv_unwind_with_real_gdb(tmp_path, monkeypatch):
    from platformio_mcp.toolchain import find_gdb, load_toolchain, require_elf
    from platformio_mcp.tools.analysis import pio_decode_backtrace

    monkeypatch.setenv("PLATFORMIO_MCP_LOG_DIR", str(tmp_path / "logs"))
    dst = tmp_path / "riscv-crash"
    shutil.copytree(RISCV_FIXTURE, dst, ignore=shutil.ignore_patterns(".pio"))
    built = pio_build(str(dst))
    assert built["ok"], built["summary"]
    tc = load_toolchain(str(dst), "c3")
    if not find_gdb(tc.cc_path):
        pytest.skip("riscv32-esp-elf-gdb not installed")
    mepc, ra = _riscv_frames_from_elf(tc, require_elf(tc))
    stack = "\n".join(f"{0x3FC9F000 + i * 32:08x}: " + " ".join(["0x00000000"] * 8) for i in range(4))
    panic = (
        "Guru Meditation Error: Core  0 panic'ed (Store access fault). Exception was unhandled.\n\n"
        "Core  0 register dump:\n"
        f"MEPC    : 0x{mepc:08x}  RA      : 0x{ra:08x}  SP      : 0x3fc9f000  GP      : 0x3fc8b400\n"
        "A0      : 0x00000000  A1      : 0x0000002a\n\n"
        f"Stack memory:\n{stack}\n"
    )
    r = pio_decode_backtrace(project_dir=str(dst), env="c3", text=panic)
    assert r["method"] == "gdb_unwind", r["summary"]
    names = [f["function"] or "" for f in r["frames"]]
    assert names[0].startswith("level3") and names[1].startswith("level2"), r["summary"]
    assert r["frames"][0]["file"].endswith("main.cpp")
