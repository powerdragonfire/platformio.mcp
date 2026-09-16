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
