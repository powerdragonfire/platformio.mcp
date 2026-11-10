"""MCP server assembly."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, analytics
from .core import monitors, policy
from .debugger import debuggers
from .tools.hardware import test_plan_runs
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
For repeatable hardware checks use pio_test_plan_validate/start/status/cancel with a declarative JSON plan;
it records per-iteration pass/fail/timeout/infra_error outcomes, evidence logs, and firmware/source identity.
"""


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[None]:
    try:
        yield
    finally:
        test_plan_runs.stop_all(wait_s=10.0, stop_monitors=True)
        debuggers.stop_all()
        monitors.stop_all()
        recorder = getattr(server, "recorder", None)
        if recorder is not None:
            # Synchronous on purpose: an await here could be cancelled during shutdown.
            recorder.flush(timeout=2.0)


# The handshake methods: `initialize` is reserved by the SDK runner and cannot be overridden, so the
# middleware records on `notifications/initialized` (handshake clients) and `server/discover` (2026-07-28+).
_INITIALIZE_METHODS = ("notifications/initialized", "server/discover")


def _tool_names(result: Any) -> list[str]:
    tools = result.get("tools") if isinstance(result, dict) else getattr(result, "tools", None)
    names = [t.get("name") if isinstance(t, dict) else getattr(t, "name", None) for t in tools or []]
    return [str(n) for n in names if n]


class PlatformIOServer(MCPServer):
    """MCPServer that writes one analytics event per tool call when a recorder is attached, plus one
    `$mcp_initialize` and one `$mcp_tools_list` per process through the `observe` middleware."""

    recorder: analytics.Recorder | None = None

    async def observe(self, ctx: Any, call_next: Any) -> Any:
        """Server middleware: the SDK's documented hook for observing the handshake and `tools/list`."""
        result = await call_next(ctx)
        if self.recorder is not None and ctx.method in (*_INITIALIZE_METHODS, "tools/list"):
            with suppress(Exception):  # analytics must never break the protocol
                client, protocol = analytics.request_client(ctx), str(ctx.protocol_version or "") or None
                if ctx.method == "tools/list":
                    self.recorder.record_lifecycle("$mcp_tools_list", client=client, protocol_version=protocol, tool_names=_tool_names(result))
                else:
                    self.recorder.record_lifecycle("$mcp_initialize", client=client, protocol_version=protocol)
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        if self.recorder is None:
            return await super().call_tool(name, arguments, context)
        start = time.monotonic()
        ok, error = False, None
        try:
            result = await super().call_tool(name, arguments, context)
            ok, error = analytics.outcome(result)
            return result
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self.recorder.record(
                tool=name, ok=ok, error=error, duration_ms=int((time.monotonic() - start) * 1000),
                arg_keys=list(arguments or {}), client=analytics.client_from_context(context),
                protocol_version=analytics.protocol_from_context(context), ids=analytics.public_ids(arguments),
            )


def create_server(record_usage: bool = False) -> MCPServer:
    """record_usage=True attaches the analytics recorder unless PLATFORMIO_MCP_ANALYTICS=off."""
    mcp = PlatformIOServer(name="platformio", version=__version__, instructions=INSTRUCTIONS + f"\nActive policy: {policy()}.", lifespan=lifespan)
    if record_usage and analytics.enabled():
        mcp.recorder = analytics.Recorder.from_env()
        mcp.middleware.append(mcp.observe)
    register_all(mcp)
    return mcp


def serve() -> None:
    create_server(record_usage=True).run(transport="stdio")
