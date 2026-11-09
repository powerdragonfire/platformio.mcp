"""PostHog client initialization for the MCP server."""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
from typing import Any

from posthog import Posthog


posthog_logger = logging.getLogger("platformio_mcp.posthog")
posthog_logger.propagate = False


def _missing_configuration_warning(variable_name: str) -> None:
    print(
        f"{variable_name} variable required by PostHog is missing or un-configured, "
        f"this causes events to be silently missed. This error stops appearing once "
        f"{variable_name} is configured",
        file=sys.stderr,
    )


def _initialize_posthog() -> Posthog | None:
    project_token = os.getenv("POSTHOG_PROJECT_TOKEN")
    host = os.getenv("POSTHOG_HOST")

    if not project_token:
        _missing_configuration_warning("POSTHOG_PROJECT_TOKEN")
    if not host:
        _missing_configuration_warning("POSTHOG_HOST")
    if not project_token or not host:
        return None

    return Posthog(
        project_token,
        host=host,
        enable_exception_autocapture=True,
    )


posthog_client = _initialize_posthog()


def _initialize_posthog_logs() -> Any | None:
    """Export only records emitted through the dedicated PostHog logger."""
    project_token = os.getenv("POSTHOG_PROJECT_TOKEN")
    host = os.getenv("POSTHOG_HOST")
    if not project_token or not host:
        return None

    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    except ModuleNotFoundError:
        print(
            "PostHog log capture is unavailable because OpenTelemetry log dependencies are not installed.",
            file=sys.stderr,
        )
        return None

    logger_provider = LoggerProvider()
    exporter = OTLPLogExporter(
        endpoint=f"{host.rstrip('/')}/i/v1/logs",
        headers={"Authorization": f"Bearer {project_token}"},
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    posthog_logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    posthog_logger.setLevel(logging.INFO)
    return logger_provider


posthog_log_provider = _initialize_posthog_logs()


def capture_log(message: str, **attributes: str | int | float | bool | None) -> None:
    """Emit a structured lifecycle log without affecting MCP execution."""
    if posthog_log_provider:
        posthog_logger.info(message, extra=attributes)


def capture_event(event: str, properties: dict[str, object]) -> None:
    """Send a personless product event without affecting MCP tool execution."""
    if not posthog_client:
        return
    try:
        posthog_client.capture(event=event, properties=properties)
    except Exception:
        pass


def _shutdown_analytics() -> None:
    if posthog_log_provider:
        try:
            posthog_log_provider.force_flush(timeout_millis=3_000)
        except Exception:
            pass
        try:
            posthog_log_provider.shutdown()
        except Exception:
            pass
    if posthog_client:
        posthog_client.shutdown()


def _shutdown_on_sigterm(*_: object) -> None:
    thread = threading.Thread(target=_shutdown_analytics, daemon=True)
    thread.start()
    thread.join(3)
    os._exit(0)


if posthog_client or posthog_log_provider:
    atexit.register(_shutdown_analytics)
    signal.signal(signal.SIGTERM, _shutdown_on_sigterm)
