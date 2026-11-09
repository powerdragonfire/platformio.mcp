import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from platformio_mcp import analytics, cli
from platformio_mcp.server import create_server


@pytest.fixture
def usage_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_ANALYTICS_DIR", str(tmp_path))
    monkeypatch.delenv("PLATFORMIO_MCP_ANALYTICS", raising=False)
    monkeypatch.delenv("PLATFORMIO_MCP_ANALYTICS_URL", raising=False)
    return tmp_path


def read_events(path):
    return [json.loads(line) for line in (path / "usage.jsonl").read_text().splitlines()]


@pytest.mark.asyncio
async def test_tool_call_records_event_without_argument_values(usage_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "read_only")
    secret_dir = str(tmp_path / "my-secret-project")
    mcp = create_server(record_usage=True)
    await mcp.call_tool("pio_build", {"project_dir": secret_dir, "env": "esp32dev"})

    [event] = read_events(usage_dir)
    assert event["tool"] == "pio_build"
    assert event["ok"] is False and event["error"] == "policy_denied"
    assert event["arg_keys"] == ["env", "project_dir"]
    assert event["policy"] == "read_only"
    assert isinstance(event["duration_ms"], int)
    assert event["session"] and event["server_version"] and event["os"]
    raw = (usage_dir / "usage.jsonl").read_text()
    assert "my-secret-project" not in raw and "esp32dev" not in raw


@pytest.mark.asyncio
async def test_record_usage_off_by_default_and_env_switch(usage_dir, monkeypatch, tmp_path):
    await create_server().call_tool("pio_monitor_list", {})
    assert not (usage_dir / "usage.jsonl").exists()

    monkeypatch.setenv("PLATFORMIO_MCP_ANALYTICS", "off")
    await create_server(record_usage=True).call_tool("pio_monitor_list", {})
    assert not (usage_dir / "usage.jsonl").exists()


@pytest.mark.asyncio
async def test_unknown_tool_and_bad_arguments_are_recorded_as_failures(usage_dir):
    mcp = create_server(record_usage=True)
    with pytest.raises(Exception):
        await mcp.call_tool("pio_monitor_read", {})
    events = read_events(usage_dir)
    assert events[-1]["tool"] == "pio_monitor_read"
    assert events[-1]["ok"] is False and events[-1]["error"] == "ToolError"


def test_rotation_keeps_one_previous_file(usage_dir, monkeypatch):
    monkeypatch.setattr(analytics, "MAX_BYTES", 200)
    rec = analytics.Recorder.from_env()
    for _ in range(10):
        rec.record(tool="pio_build", ok=True, error=None, duration_ms=5, arg_keys=[], client=None)
    assert (usage_dir / "usage.jsonl.1").exists()
    assert (usage_dir / "usage.jsonl").stat().st_size <= 200 + 400


def test_summarize_counts_rates_and_percentiles():
    events = [
        {"ts": "2026-11-01T10:00:00Z", "session": "a", "tool": "pio_build", "ok": True, "error": None, "duration_ms": d, "client": {"name": "claude-code", "version": "2"}}
        for d in (100, 200, 300, 400)
    ] + [
        {"ts": "2026-11-02T10:00:00Z", "session": "b", "tool": "pio_build", "ok": False, "error": "policy_denied", "duration_ms": 1, "client": None},
        {"ts": "2026-11-03T10:00:00Z", "session": "b", "tool": "pio_upload", "ok": True, "error": None, "duration_ms": 50, "client": None},
    ]
    s = analytics.summarize(events)
    assert s["calls"] == 6 and s["sessions"] == 2
    assert s["first"] == "2026-11-01T10:00:00Z" and s["last"] == "2026-11-03T10:00:00Z"
    build = next(t for t in s["tools"] if t["tool"] == "pio_build")
    assert build["calls"] == 5 and build["ok_rate"] == 0.8
    assert build["p50_ms"] == 200 and build["p95_ms"] == 400
    assert s["tools"][0]["tool"] == "pio_build"
    assert s["errors"] == [{"tool": "pio_build", "error": "policy_denied", "count": 1}]
    assert s["clients"] == {"claude-code 2": 4, "unknown": 2}


def test_stats_cli_reads_the_log(usage_dir, capsys):
    rec = analytics.Recorder.from_env()
    rec.record(tool="pio_build", ok=True, error=None, duration_ms=12, arg_keys=["env"], client={"name": "cursor", "version": "1.0"})
    cli.main(["stats", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["calls"] == 1 and data["tools"][0]["tool"] == "pio_build"

    cli.main(["stats"])
    out = capsys.readouterr().out
    assert "pio_build" in out and "cursor 1.0" in out


def test_stats_cli_with_no_log(usage_dir, capsys):
    cli.main(["stats"])
    assert "No usage recorded" in capsys.readouterr().out


def test_remote_endpoint_receives_event(usage_dir, monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    monkeypatch.setenv("PLATFORMIO_MCP_ANALYTICS_URL", f"http://127.0.0.1:{server.server_port}/events")
    rec = analytics.Recorder.from_env()
    rec.record(tool="pio_list_devices", ok=True, error=None, duration_ms=3, arg_keys=[], client=None)
    thread.join(timeout=5)
    server.server_close()
    assert received and received[0]["tool"] == "pio_list_devices"


def test_remote_endpoint_failure_never_raises(usage_dir, monkeypatch):
    monkeypatch.setenv("PLATFORMIO_MCP_ANALYTICS_URL", "http://127.0.0.1:9/unreachable")
    rec = analytics.Recorder.from_env()
    rec.record(tool="pio_build", ok=True, error=None, duration_ms=1, arg_keys=[], client=None)
    rec.flush(timeout=5)
    assert read_events(usage_dir)[0]["tool"] == "pio_build"


class Collector:
    """A local HTTP server that stores every JSON body POSTed to it."""

    def __init__(self, expected: int = 1):
        self.received = []
        self.paths = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.paths.append(self.path)
                outer.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=lambda: [self.server.handle_request() for _ in range(expected)], daemon=True)
        self.thread.start()

    def wait(self):
        self.thread.join(timeout=5)
        self.server.server_close()


@pytest.fixture
def posthog(usage_dir, monkeypatch):
    collector = Collector()
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PLATFORMIO_MCP_POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("PLATFORMIO_MCP_POSTHOG_HOST", collector.url)
    return collector


def test_posthog_receives_anonymous_event(posthog, usage_dir):
    rec = analytics.Recorder.from_env()
    rec.record(tool="pio_build", ok=False, error="policy_denied", duration_ms=7, arg_keys=["env"], client={"name": "claude-code", "version": "2.1"})
    posthog.wait()

    [body] = posthog.received
    assert posthog.paths == ["/i/v0/e/"]
    assert body["api_key"] == "phc_test" and body["event"] == "$mcp_tool_call"
    assert body["distinct_id"] == (usage_dir / "install_id").read_text().strip()
    props = body["properties"]
    assert props["$process_person_profile"] is False
    assert props["$mcp_tool_name"] == "pio_build"
    assert props["$mcp_is_error"] is True and props["$mcp_error_type"] == "policy_denied"
    assert props["$mcp_duration_ms"] == 7 and props["$mcp_input_keys"] == ["env"]
    assert props["$mcp_client_name"] == "claude-code" and props["$mcp_client_version"] == "2.1"
    assert props["$mcp_server_name"] == "platformio" and props["$mcp_server_version"]
    assert props["os"] and props["policy"] and props["session"]


def test_first_run_notice_printed_once_and_install_id_stable(posthog, usage_dir, capsys):
    analytics.Recorder.from_env()
    first_err = capsys.readouterr().err
    analytics.Recorder.from_env()
    assert "DO_NOT_TRACK=1" in first_err
    assert capsys.readouterr().err == ""
    a, created_a = analytics.install_id(usage_dir)
    b, created_b = analytics.install_id(usage_dir)
    assert a == b and not created_a and not created_b


@pytest.mark.parametrize("env", [{"DO_NOT_TRACK": "1"}, {"CI": "true"}, {"PLATFORMIO_MCP_ANALYTICS": "local"}, {"PLATFORMIO_MCP_POSTHOG_KEY": ""}])
def test_posthog_opt_outs_keep_local_log(posthog, usage_dir, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rec = analytics.Recorder.from_env()
    assert rec.posthog is None
    rec.record(tool="pio_build", ok=True, error=None, duration_ms=1, arg_keys=[], client=None)
    assert read_events(usage_dir)[0]["tool"] == "pio_build"
    assert not (usage_dir / "install_id").exists()
    posthog.server.server_close()
