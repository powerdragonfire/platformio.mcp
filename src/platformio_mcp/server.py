"""MCP server assembly."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from . import __version__
from .core import monitors, policy
from .tools import register_all

INSTRUCTIONS = """PlatformIO MCP gives you hands on embedded hardware through the PlatformIO CLI.

Typical loop: pio_system_info -> pio_project_envs (or pio_list_boards + pio_project_init for a new project)
-> edit code -> pio_build (fix the returned errors) -> pio_list_devices -> pio_flash_and_verify(expect="<healthy line>")
which flashes, watches the boot log, and returns pass/fail with any crash decoded to file:line. For finer control use
pio_upload then pio_monitor_capture or pio_monitor_start/pio_monitor_read with wait_for. When the device prints a
Guru Meditation / HardFault dump, call pio_decode_backtrace. When flash or RAM is tight, call pio_size_report to see
the biggest symbols and files. Use pio_test for Unity tests and pio_check for static analysis. Every tool returns ok, a one-paragraph summary, structured fields, and a
log_path with the full command output when it was long. Stop monitor sessions before flashing.
"""


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[None]:
    try:
        yield
    finally:
        monitors.stop_all()


def create_server() -> MCPServer:
    mcp = MCPServer(name="platformio", version=__version__, instructions=INSTRUCTIONS + f"\nActive policy: {policy()}.", lifespan=lifespan)
    register_all(mcp)
    return mcp


def serve() -> None:
    create_server().run(transport="stdio")
