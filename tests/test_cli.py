import json
from pathlib import Path

import pytest

from platformio_mcp import cli


def test_print_snippet(capsys):
    cli.main(["install", "--print", "--policy", "build_only", "--with-platformio"])
    data = json.loads(capsys.readouterr().out)
    entry = data["mcpServers"]["platformio"]
    assert entry["command"] == "uvx"
    assert entry["args"] == ["platformio.mcp[platformio]"]
    assert entry["env"] == {"PLATFORMIO_MCP_POLICY": "build_only"}


def test_claude_desktop_merge_keeps_other_servers(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    monkeypatch.setattr(cli, "claude_desktop_config", lambda: cfg)
    cli.main(["install", "--claude-desktop"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["platformio"]["args"] == ["platformio.mcp"]
    assert data["theme"] == "dark"


def test_cursor_project_scope_and_codex(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.main(["install", "--cursor", "--scope", "project"])
    data = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert "platformio" in data["mcpServers"]

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    codex = tmp_path / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text('model = "gpt"\n')
    cli.main(["install", "--codex", "--policy", "read_only"])
    text = codex.read_text()
    assert text.startswith('model = "gpt"\n')
    assert '[mcp_servers.platformio]' in text and 'PLATFORMIO_MCP_POLICY = "read_only"' in text
    # second run does not duplicate the block
    cli.main(["install", "--codex"])
    assert codex.read_text().count("[mcp_servers.platformio]") == 1


def test_install_requires_client():
    with pytest.raises(SystemExit):
        cli.main(["install"])
