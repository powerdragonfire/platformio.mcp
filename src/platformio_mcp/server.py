"""MCP server assembly."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from . import __version__
from .core import monitors, policy
from .debugger import debuggers
from .tools import register_all

INSTRUCTIONS = """PlatformIO MCP gives you hands on embedded hardware through the PlatformIO CLI.

Typical loop: pio_system_info -> pio_project_envs (or pio_list_boards + pio_project_init for a new project)
-> edit code -> pio_build (fix the returned errors) -> pio_list_devices -> pio_flash_and_verify(expect="<healthy line>")
which flashes, watches the boot log, and returns pass/fail with any crash decoded to file:line. For finer control use
pio_upload then pio_monitor_capture or pio_monitor_start/pio_monitor_read with wait_for. When the device prints a
Guru Meditation / HardFault dump, call pio_decode_backtrace. When flash or RAM is tight, call pio_size_report to see
the biggest symbols and files. Use pio_test for Unity tests and pio_check for static analysis. Every tool returns ok, a one-paragraph summary, structured fields, and a
log_path with the full command output when it was long. Stop monitor sessions before flashing (or pass stop_open_sessions=true).
When an upload cannot open the port, the result carries port_diagnosis; pio_port_diagnose gives the same answer on demand.
On ESP32 call pio_partition_table before changing partitions or when a flash looks fine but the device misbehaves, and
pio_coredump after a crash to pull and decode the core dump. pio_memory_watch turns heap/stack prints into leak and
stack-headroom verdicts; pio_power_profile measures current from a serial meter or a PPK2. pio_deps_check audits lib_deps
for name collisions and cycles. pio_upload_ota flashes over Wi-Fi. pio_debug_start opens a GDB session through the
debug probe; drive it with pio_debug_cmd (bt, break, next, continue, p var) and stop it before flashing.
"""


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[None]:
    try:
        yield
    finally:
        monitors.stop_all()
        debuggers.stop_all()


def create_server() -> MCPServer:
    mcp = MCPServer(name="platformio", version=__version__, instructions=INSTRUCTIONS + f"\nActive policy: {policy()}.", lifespan=lifespan)
    register_all(mcp)
    return mcp


def serve() -> None:
    create_server().run(transport="stdio")
