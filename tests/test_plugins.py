"""Plugin packages resolve the same stdio server without platform-specific prerequisites."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    return json.loads((ROOT / name).read_text())


def test_portable_and_legacy_mcp_entries_are_identical():
    portable = load("mcp.json")
    legacy = load(".mcp.json")
    assert portable["mcpServers"] == legacy["mcpServers"]
    assert list(portable["mcpServers"]) == ["platformio"]
    entry = portable["mcpServers"]["platformio"]
    assert entry == {"type": "stdio", "command": "uvx", "args": ["platformio.mcp[platformio]"]}


def test_codex_marketplace_points_to_packaged_plugin():
    marketplace = load(".agents/plugins/marketplace.json")
    [plugin] = marketplace["plugins"]
    assert plugin["name"] == "platformio"
    assert plugin["source"] == {"source": "local", "path": "./"}
    assert plugin["policy"]["installation"] == "AVAILABLE"
    manifest = load(".codex-plugin/plugin.json")
    assert manifest["mcpServers"] == "./.mcp.json"
    assert (ROOT / manifest["skills"] / "platformio" / "SKILL.md").is_file()
    assert manifest["version"] == load("plugin.json")["version"]
