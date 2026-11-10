import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from platformio_mcp import __version__, analytics, cli
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
    rec.flush(timeout=2.0)
    posthog.wait()

    [body] = posthog.received
    assert posthog.paths == ["/batch/"]
    assert body["api_key"] == "phc_test"
    [event] = body["batch"]
    assert event["event"] == "$mcp_tool_call"
    assert event["distinct_id"] == (usage_dir / "install_id").read_text().strip()
    props = event["properties"]
    assert props["$session_id"] == rec.session
    assert props["$mcp_source"] == "posthog_mcp_analytics"
    assert props["$lib_version"] == __version__
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


def test_posthog_batch_sends_at_twenty_events(posthog):
    rec = analytics.Recorder.from_env()
    for _ in range(20):
        rec.record(tool="pio_build", ok=True, error=None, duration_ms=1, arg_keys=[], client=None)
    posthog.wait()
    [body] = posthog.received
    assert len(body["batch"]) == 20


def test_posthog_batch_sends_after_interval(posthog, monkeypatch):
    monkeypatch.setattr(analytics, "BATCH_INTERVAL_S", 0.2)
    rec = analytics.Recorder.from_env()
    rec.record(tool="pio_build", ok=True, error=None, duration_ms=1, arg_keys=[], client=None)
    rec.record(tool="pio_upload", ok=True, error=None, duration_ms=1, arg_keys=[], client=None)
    posthog.wait()
    [body] = posthog.received
    assert [e["properties"]["$mcp_tool_name"] for e in body["batch"]] == ["pio_build", "pio_upload"]


def test_raw_url_sink_gets_one_post_per_event(usage_dir, monkeypatch):
    collector = Collector(expected=2)
    monkeypatch.setenv("PLATFORMIO_MCP_ANALYTICS_URL", collector.url + "/events")
    rec = analytics.Recorder.from_env()
    rec.record(tool="pio_build", ok=True, error=None, duration_ms=1, arg_keys=[], client=None)
    rec.record(tool="pio_upload", ok=True, error=None, duration_ms=1, arg_keys=[], client=None)
    collector.wait()
    assert collector.paths == ["/events", "/events"]
    assert [b["tool"] for b in collector.received] == ["pio_build", "pio_upload"]


async def _session(mode, calls):
    from mcp import Client
    from mcp.types import Implementation

    mcp = create_server(record_usage=True)
    async with Client(mcp, client_info=Implementation(name="verification-test", version="9.9"), mode=mode) as client:
        await client.list_tools()
        await client.list_tools()
        for name, args in calls:
            await client.call_tool(name, args)
    return mcp


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,protocol", [("legacy", "2025-11-25"), ("auto", "2026-07-28")])
async def test_session_sends_initialize_and_tools_list_once_and_flushes_on_shutdown(posthog, monkeypatch, mode, protocol):
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "read_only")
    monkeypatch.setattr(analytics, "BATCH_INTERVAL_S", 60.0)
    mcp = await _session(mode, [("pio_monitor_list", {}), ("pio_monitor_list", {})])
    posthog.wait()  # only the lifespan shutdown flush can send before the 60 s interval

    [body] = posthog.received
    events = body["batch"]
    assert [e["event"] for e in events] == ["$mcp_initialize", "$mcp_tools_list", "$mcp_tool_call", "$mcp_tool_call"]
    for e in events:
        props = e["properties"]
        assert props["$session_id"] == mcp.recorder.session
        assert props["$mcp_protocol_version"] == protocol
        assert props["$mcp_client_name"] == "verification-test" and props["$mcp_client_version"] == "9.9"
        assert props["$mcp_source"] == "posthog_mcp_analytics"
    listed = events[1]["properties"]["$mcp_listed_tool_names"]
    assert "pio_build" in listed and "pio_monitor_list" in listed and len(listed) == len(set(listed))


@pytest.mark.asyncio
async def test_board_and_platform_ids_are_sent_and_other_values_are_not(posthog, monkeypatch, tmp_path):
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "read_only")
    monkeypatch.setattr(analytics, "BATCH_INTERVAL_S", 60.0)
    secret = str(tmp_path / "my-secret-project")
    await _session("legacy", [
        ("pio_project_init", {"project_dir": secret, "board": "esp32dev", "framework": "arduino"}),
        ("pio_list_boards", {"query": "secret-query", "platform": "espressif32"}),
        ("pio_project_init", {"project_dir": secret, "board": secret + "/boards/custom.json"}),
        ("pio_list_boards", {"query": "x", "platform": "https://github.com/me/platform-private.git"}),
        ("pio_project_init", {"project_dir": secret, "board": "192.168.1.20"}),
    ])
    posthog.wait()

    calls = [e["properties"] for e in posthog.received[0]["batch"] if e["event"] == "$mcp_tool_call"]
    assert calls[0]["board"] == "esp32dev" and "platform" not in calls[0]
    assert calls[1]["platform"] == "espressif32" and "board" not in calls[1]
    assert "board" not in calls[2] and "platform" not in calls[3] and "board" not in calls[4]
    raw = json.dumps(posthog.received)
    for value in ("my-secret-project", "secret-query", "arduino", "platform-private", "192.168.1.20", str(tmp_path)):
        assert value not in raw


class FakePosthog:
    def __init__(self, token, host=None):
        self.token, self.host, self.shut = token, host, False

    def shutdown(self):
        self.shut = True


@pytest.fixture
def sdk(usage_dir, monkeypatch):
    """Record instrument() calls; allow remote sending unless a test opts out."""
    from platformio_mcp import server

    calls = []

    class FakeAnalytics:
        flushed = False

        async def flush(self):
            FakeAnalytics.flushed = True

    def fake_instrument(mcp, client=None, options=None):
        calls.append(client)
        return FakeAnalytics()

    monkeypatch.setattr(server, "instrument", fake_instrument)
    monkeypatch.setattr(server, "Posthog", FakePosthog)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PLATFORMIO_MCP_POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_sdk")
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    return calls


@pytest.mark.parametrize(
    "env",
    [
        {"PLATFORMIO_MCP_ANALYTICS": "off"},
        {"PLATFORMIO_MCP_ANALYTICS": "local"},
        {"DO_NOT_TRACK": "1"},
        {"CI": "true"},
        {"POSTHOG_PROJECT_TOKEN": ""},
    ],
)
def test_sdk_layer_respects_opt_outs(sdk, monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    mcp = create_server(record_usage=True)
    assert sdk == []
    assert mcp.posthog_client is None


def test_sdk_layer_off_without_record_usage(sdk):
    create_server(record_usage=False)
    assert sdk == []


def test_sdk_layer_replaces_recorder_posthog_sending(sdk):
    mcp = create_server(record_usage=True)
    [client] = sdk
    assert client.token == "phc_sdk"
    assert client.host == analytics.POSTHOG_HOST
    assert mcp.recorder is not None
    assert mcp.recorder.posthog is None  # one sender per event: the SDK


def test_recorder_keeps_posthog_without_sdk_token(sdk, monkeypatch):
    monkeypatch.delenv("POSTHOG_PROJECT_TOKEN")
    mcp = create_server(record_usage=True)
    assert sdk == []
    assert mcp.recorder.posthog is not None


@pytest.mark.asyncio
async def test_lifespan_flushes_and_shuts_down_sdk_client(sdk):
    from platformio_mcp.server import lifespan

    mcp = create_server(record_usage=True)
    async with lifespan(mcp):
        pass
    assert mcp.posthog_analytics.flushed
    assert mcp.posthog_client.shut


def test_server_module_has_no_import_side_effects():
    from pathlib import Path

    from platformio_mcp import server

    source = Path(server.__file__).read_text()
    assert "os._exit" not in source
    assert not any(isinstance(v, FakePosthog) or type(v).__name__ == "Posthog" for v in vars(server).values())


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [None, "", "   ", "Inspecting active serial monitors"])
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_sdk_captures_intent_with_empty_context_fallback(usage_dir, monkeypatch, context, mode):
    """Exercise real instrumentation; only replace the outbound PostHog client."""
    from mcp import Client

    from platformio_mcp import server

    events = []

    class CapturingPosthog(FakePosthog):
        def capture(self, event, **payload):
            if event == "$mcp_tool_call":
                events.append(payload["properties"])

    monkeypatch.setattr(server, "Posthog", CapturingPosthog)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_test")
    mcp = create_server(record_usage=True)

    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "read_only")
    arguments = {} if context is None else {"context": context}
    async with Client(mcp, mode=mode) as client:
        listing = await client.list_tools()
        tool = next(tool for tool in listing.tools if tool.name == "pio_monitor_list")
        assert tool.input_schema["properties"]["context"]["type"] == "string"
        await client.call_tool("pio_monitor_list", arguments)
        await client.call_tool("pio_build", {"project_dir": "/private/secret-project", "env": "secret-env"})

    [event, build_event] = events
    assert build_event["$mcp_intent"] == "Invoking pio_build"
    assert build_event["$mcp_intent_source"] == "inferred"
    assert "secret" not in build_event["$mcp_intent"]
    if context and context.strip():
        assert event["$mcp_intent"] == "Inspecting active serial monitors"
        assert event["$mcp_intent_source"] == "context_parameter"
    else:
        assert event["$mcp_intent"] == "Invoking pio_monitor_list"
        assert event["$mcp_intent_source"] == "inferred"
    assert read_events(usage_dir)[0]["ok"] is True
