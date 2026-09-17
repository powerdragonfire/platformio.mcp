import json

import pytest

from platformio_mcp.server import create_server

EXPECTED_TOOLS = {
    "pio_system_info", "pio_list_boards", "pio_board_info", "pio_project_init", "pio_project_envs", "pio_project_metadata",
    "pio_build", "pio_upload", "pio_clean", "pio_list_targets", "pio_run_target",
    "pio_list_devices", "pio_port_diagnose", "pio_monitor_start", "pio_monitor_read", "pio_monitor_write", "pio_monitor_stop", "pio_monitor_list", "pio_monitor_capture",
    "pio_test", "pio_check",
    "pio_pkg_search", "pio_pkg_install", "pio_pkg_uninstall", "pio_pkg_list", "pio_pkg_outdated", "pio_pkg_update",
    "pio_decode_backtrace", "pio_size_report", "pio_flash_and_verify",
    "pio_upload_ota",
    "pio_partition_table", "pio_coredump",
    "pio_power_profile",
}


@pytest.mark.asyncio
async def test_all_tools_registered_with_descriptions():
    mcp = create_server()
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS
    for t in tools:
        assert t.description and len(t.description) > 40, t.name
    read = next(t for t in tools if t.name == "pio_monitor_read")
    assert set(read.input_schema["properties"]) == {"session_id", "cursor", "max_lines", "wait_for", "timeout_s"}
    assert read.input_schema["required"] == ["session_id"]


@pytest.mark.asyncio
async def test_call_tool_returns_structured_result(monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "read_only")
    mcp = create_server()
    result = await mcp.call_tool("pio_build", {"project_dir": str(tmp_path)})
    payload = result.structured_content or json.loads(result.content[0].text)
    assert payload["ok"] is False and payload["error"] == "policy_denied"
