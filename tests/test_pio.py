import os
import sys

import pytest

from platformio_mcp import pio

BANNER = """********************************************************************************
Obsolete PIO Core v6.1.18 is used (previous was 6.2.0)
Please remove multiple PIO Cores from a system:
https://docs.platformio.org/en/latest/core/installation/troubleshooting.html
********************************************************************************
\x1b[32mProcessing view (platform: espressif32@3.4)\x1b[0m
Verbose mode can be enabled via `-v, --verbose` option
Compiling .pio/build/view/src/main.cpp.o\r
"""


def test_clean_output_strips_banner_ansi_and_noise():
    out = pio.clean_output(BANNER)
    assert out == "Processing view (platform: espressif32@3.4)\nCompiling .pio/build/view/src/main.cpp.o"


def test_tail():
    text = "\n".join(str(i) for i in range(100))
    assert pio.tail(text, 3) == "97\n98\n99"
    assert pio.tail("a\nb", 5) == "a\nb"


def test_find_pio_prefers_env_override(monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_PIO", "/custom/pio")
    assert pio.find_pio() == ["/custom/pio"]


def test_find_pio_missing_raises_with_hint(monkeypatch, tmp_path):
    monkeypatch.delenv("PLATFORMIO_MCP_PIO", raising=False)
    monkeypatch.setattr(pio.shutil, "which", lambda name: None)
    monkeypatch.setattr(pio.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setitem(sys.modules, "platformio", None)
    with pytest.raises(pio.PioNotFound) as e:
        pio.require_pio()
    assert "uv tool install platformio" in str(e.value)


def test_run_pio_writes_log_and_handles_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORMIO_MCP_LOG_DIR", str(tmp_path))
    fake = tmp_path / "fakepio.py"
    fake.write_text("import sys; print('hello'); print('bad', file=sys.stderr); sys.exit(3)")
    monkeypatch.setenv("PLATFORMIO_MCP_PIO", sys.executable)
    res = pio.run_pio([str(fake)], cwd=tmp_path, tool="unit")
    assert res.returncode == 3 and not res.ok
    assert "hello" in res.output and "bad" in res.output
    assert res.log_path and os.path.exists(res.log_path)
    assert "-unit.log" in res.log_path


def test_run_pio_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORMIO_MCP_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("PLATFORMIO_MCP_PIO", sys.executable)
    res = pio.run_pio(["-c", "import time; time.sleep(5)"], timeout=0.3, tool="unit")
    assert res.timed_out and not res.ok
    assert "timed out" in res.output


def test_resolve_project_dir(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        pio.resolve_project_dir(str(tmp_path))
    (tmp_path / "platformio.ini").write_text("[env:x]\n")
    assert pio.resolve_project_dir(str(tmp_path)) == tmp_path.resolve()
    monkeypatch.setenv("PLATFORMIO_MCP_PROJECT_DIR", str(tmp_path))
    assert pio.resolve_project_dir(None) == tmp_path.resolve()


def test_write_log_prunes_old_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("PLATFORMIO_MCP_MAX_LOGS", "3")
    paths = [pio.write_log("t", f"log {i}") for i in range(5)]
    remaining = sorted(p.name for p in tmp_path.glob("*.log"))
    assert len(remaining) == 3
    assert all(os.path.basename(p) in remaining for p in paths[-3:])
    assert not os.path.exists(paths[0]) and not os.path.exists(paths[1])
