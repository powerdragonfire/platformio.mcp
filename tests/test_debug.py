"""GDB/MI parsing on captured output, and debug sessions against a scripted fake gdb process."""

import subprocess
import sys
from pathlib import Path

import pytest

from platformio_mcp import debugger
from platformio_mcp.debugger import (
    DebugManager,
    DebugStartError,
    classify_start_failure,
    parse_mi_line,
    parse_mi_results,
    unescape_c_string,
)
from platformio_mcp.tools import debug as debug_tools

FAKE = Path(__file__).parent / "fake_gdb.py"

WINDOWS_FLAKY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="subprocess pipe synchronization against the fake gdb process is flaky on windows-latest CI; see https://github.com/powerdragonfire/platformio.mcp/issues/10",
)


# --- parser ---------------------------------------------------------------------------------------


def test_unescape_c_string_handles_octal_utf8_and_escapes():
    assert unescape_c_string('h\\303\\251\\n') == "hé\n"
    assert unescape_c_string('say \\"hi\\"\\t\\\\') == 'say "hi"\t\\'


def test_parse_console_log_target_streams():
    assert parse_mi_line('~"$1 = 2\\n"').kind == "console"
    assert parse_mi_line('~"$1 = 2\\n"').text == "$1 = 2\n"
    assert parse_mi_line('&"p nosuch\\n"').kind == "log"
    t = parse_mi_line('@"Info : accepting \'gdb\' connection on tcp/3333\\n"')
    assert t.kind == "target" and "tcp/3333" in t.text


def test_parse_result_records():
    done = parse_mi_line("^done")
    assert (done.kind, done.cls, done.payload) == ("result", "done", {})
    err = parse_mi_line('^error,msg="No symbol \\"nosuch\\" in current context."')
    assert err.cls == "error" and err.payload["msg"] == 'No symbol "nosuch" in current context.'
    tok = parse_mi_line("0^running")
    assert tok.token == "0" and tok.cls == "running"
    assert parse_mi_line("^exit").cls == "exit"


def test_parse_stopped_event_with_frame():
    rec = parse_mi_line('*stopped,reason="breakpoint-hit",disp="del",bkptno="1",frame={addr="0x400d1234",func="main",args=[{name="argc",value="1"}],file="src/main.cpp",fullname="/p/src/main.cpp",line="12"},thread-id="1",stopped-threads="all"')
    assert rec.kind == "exec" and rec.cls == "stopped"
    assert rec.payload["reason"] == "breakpoint-hit"
    assert rec.payload["frame"]["func"] == "main" and rec.payload["frame"]["line"] == "12"
    assert rec.payload["frame"]["args"] == [{"name": "argc", "value": "1"}]
    s = debugger.stop_summary(rec)
    assert s["frame"] == {"function": "main", "file": "/p/src/main.cpp", "line": 12, "address": "0x400d1234", "args": [{"name": "argc", "value": "1"}]}
    assert s["bkptno"] == "1"


def test_parse_lists_and_repeated_keys():
    p = parse_mi_results('stack=[frame={level="0",func="main"},frame={level="1",func="app_main"}]')
    assert [f["frame"]["func"] for f in p["stack"]] == ["main", "app_main"]
    p = parse_mi_results('bkpt={number="1"},bkpt={number="2"}')
    assert p["bkpt"] == [{"number": "1"}, {"number": "2"}]
    assert parse_mi_results('threads=[],current-thread-id="1"') == {"threads": [], "current-thread-id": "1"}


def test_parse_prompt_notify_and_plain_lines():
    assert parse_mi_line("(gdb) ").kind == "prompt"
    assert parse_mi_line("(gdb)\r\n").kind == "prompt"
    n = parse_mi_line('=thread-group-added,id="i1"')
    assert n.kind == "notify" and n.payload == {"id": "i1"}
    other = parse_mi_line("Error: unable to open ftdi device with vid 0403")
    assert other.kind == "other" and other.text.startswith("Error: unable")


def test_classify_start_failure():
    assert classify_start_failure("Error: unable to open ftdi device with vid 0403, pid 6010")[0] == "probe_not_found"
    assert classify_start_failure("Error in sourced command file:\nRemote communication error")[0] == "init_script_failed"
    assert classify_start_failure("DebugInvalidOptionsError: Unknown debug tool `foo`. Please use one of `esp-prog`")[0] == "debug_tool_missing"
    assert classify_start_failure("something else")[0] == "debug_exited"


# --- sessions against the fake ---------------------------------------------------------------------


def fake_opener(mode: str = "ok", with_init_script: bool = True):
    calls = []

    def opener(cmd, cwd):
        calls.append(cmd)
        args = [sys.executable, str(FAKE), mode] + ([cwd] if with_init_script else [])
        return subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", bufsize=1)

    opener.calls = calls
    return opener


@pytest.fixture
def project(tmp_path):
    (tmp_path / "platformio.ini").write_text("[env:dbg]\nplatform = espressif32\nboard = esp32dev\nbuild_type = debug\ndebug_tool = esp-prog\n\n[env:rel]\nplatform = espressif32\nboard = esp32dev\n")
    return tmp_path


@pytest.fixture
def manager(monkeypatch, project):
    mgr = DebugManager(opener=fake_opener())
    monkeypatch.setattr(debug_tools, "debuggers", mgr)
    monkeypatch.setattr(debugger, "require_pio", lambda: ["pio"])
    monkeypatch.setattr(debug_tools, "project_config", lambda p: {"platformio": {"default_envs": "rel, dbg"}, "env:dbg": {"build_type": "debug", "debug_tool": "esp-prog"}, "env:rel": {}})
    yield mgr
    mgr.stop_all()


def test_build_command_matches_pio_debug_cli(monkeypatch):
    monkeypatch.setattr(debugger, "require_pio", lambda: ["/usr/bin/pio"])
    mgr = DebugManager()
    assert mgr.build_command("/p", "dbg", load=True) == ["/usr/bin/pio", "debug", "-d", "/p", "-e", "dbg", "--load-mode", "always", "--interface=gdb", "--", "--interpreter=mi2", "-x", ".pioinit"]
    assert mgr.build_command("/p", None, load=False)[4:8] == ["--load-mode", "manual", "--interface=gdb", "--"]


@WINDOWS_FLAKY
def test_start_picks_debug_env_and_reports_initial_stop(manager, project):
    r = debug_tools.pio_debug_start(project_dir=str(project))
    assert r["ok"] is True, r
    assert r["env"] == "dbg"  # build_type = debug wins over the first default env
    assert r["debug_tool"] == "esp-prog"
    assert r["gdb_version"].startswith("GNU gdb")
    assert r["stopped"]["reason"] == "breakpoint-hit" and r["stopped"]["frame"]["function"] == "main" and r["stopped"]["frame"]["line"] == 12
    assert "breakpoint-hit at main (main.cpp:12)" in r["summary"]
    assert r["init_script_path"].endswith(".pioinit") and "tbreak main" in r["init_script"]
    cmd = manager._opener.calls[0]
    assert cmd[:4] == ["pio", "debug", "-d", str(project)] and "--interpreter=mi2" in cmd and cmd[cmd.index("-e") + 1] == "dbg"
    sid = r["session_id"]
    assert debug_tools.pio_debug_list()["sessions"][0]["session_id"] == sid
    # a second session for the same project is refused
    again = debug_tools.pio_debug_start(project_dir=str(project))
    assert again["ok"] is False and "already open" in again["summary"]
    debug_tools.pio_debug_stop(sid)


@WINDOWS_FLAKY
def test_cmd_console_error_and_mi_results(manager, project):
    sid = debug_tools.pio_debug_start(project_dir=str(project), env="dbg")["session_id"]
    bt = debug_tools.pio_debug_cmd(sid, "bt")
    assert bt["ok"] and bt["result_class"] == "done"
    assert bt["console"] == ["#0  main () at src/main.cpp:12", "#1  0x400d5f00 in app_main () at /fw/main.c:33"]
    assert bt["log"] == ["bt"]
    assert "2 console line(s)" in bt["summary"]

    uni = debug_tools.pio_debug_cmd(sid, "echo unicode")
    assert uni["console"] == ["hé"]

    bad = debug_tools.pio_debug_cmd(sid, "p nosuch")
    assert bad["ok"] is False and bad["error"] == 'No symbol "nosuch" in current context.' and bad["summary"].startswith("gdb error")

    mi = debug_tools.pio_debug_cmd(sid, "-stack-list-frames")
    assert mi["ok"] and [f["frame"]["func"] for f in mi["result"]["stack"]] == ["main", "app_main"]

    bp = debug_tools.pio_debug_cmd(sid, "break src/main.cpp:42")
    assert bp["result"]["bkpt"]["number"] == "2"
    debug_tools.pio_debug_stop(sid)


@WINDOWS_FLAKY
def test_continue_waits_for_stop_and_next_steps(manager, project):
    sid = debug_tools.pio_debug_start(project_dir=str(project), env="dbg")["session_id"]
    r = debug_tools.pio_debug_cmd(sid, "continue", timeout_s=5)
    assert r["ok"] and r["result_class"] == "running"
    assert r["stopped"]["reason"] == "breakpoint-hit" and r["stopped"]["frame"]["function"] == "loop" and r["stopped"]["frame"]["line"] == 42
    assert r["running"] is False and "target breakpoint-hit at loop (main.cpp:42)" in r["summary"]
    n = debug_tools.pio_debug_cmd(sid, "next", timeout_s=5)
    assert n["stopped"]["reason"] == "end-stepping-range" and n["stopped"]["frame"]["line"] == 13
    debug_tools.pio_debug_stop(sid)


@WINDOWS_FLAKY
def test_timeout_then_interrupt(manager, project):
    sid = debug_tools.pio_debug_start(project_dir=str(project), env="dbg")["session_id"]
    r = debug_tools.pio_debug_cmd(sid, "hang", timeout_s=3)
    assert r["ok"] is False and r["timed_out"] is True and r["running"] is True and r["stopped"] is None
    assert "still running" in r["summary"] and "interrupt" in r["summary"]
    busy = debug_tools.pio_debug_cmd(sid, "bt")
    assert busy["ok"] is False and "target is running" in busy["summary"]
    i = debug_tools.pio_debug_cmd(sid, "interrupt", timeout_s=5)
    assert i["stopped"]["reason"] == "signal-received" and i["stopped"]["signal_name"] == "SIGINT" and i["stopped"]["frame"]["function"] == "delay"
    assert i["running"] is False
    ok = debug_tools.pio_debug_cmd(sid, "p 1+1")
    assert ok["ok"] and ok["console"] == ["$1 = 2"]
    debug_tools.pio_debug_stop(sid)


@WINDOWS_FLAKY
def test_stop_sends_gdb_exit_and_clears_session(manager, project):
    sid = debug_tools.pio_debug_start(project_dir=str(project), env="dbg")["session_id"]
    r = debug_tools.pio_debug_stop(sid)
    assert r["ok"] and r["closed"] is True and r["exit_code"] == 0
    assert debug_tools.pio_debug_list()["sessions"] == []
    gone = debug_tools.pio_debug_cmd(sid, "bt")
    assert gone["ok"] is False and gone["error"] == "KeyError" and "unknown debug session" in gone["summary"]


@WINDOWS_FLAKY
def test_gdb_dying_mid_session_is_reported(manager, project):
    sid = debug_tools.pio_debug_start(project_dir=str(project), env="dbg")["session_id"]
    r = debug_tools.pio_debug_cmd(sid, "crash-now", timeout_s=5)
    assert r["ok"] is False and r["closed"] is True and r["exit_code"] == 3
    assert "gdb exited" in r["summary"]
    debug_tools.pio_debug_stop(sid)


@WINDOWS_FLAKY
def test_no_init_break_start_reports_running_target(monkeypatch, project):
    mgr = DebugManager(opener=fake_opener("no_stop"))
    monkeypatch.setattr(debug_tools, "debuggers", mgr)
    monkeypatch.setattr(debugger, "require_pio", lambda: ["pio"])
    monkeypatch.setattr(debug_tools, "project_config", lambda p: {"env:dbg": {}})
    r = debug_tools.pio_debug_start(project_dir=str(project), env="dbg", timeout_s=5)
    assert r["ok"] and r["stopped"] is None and "no stop event yet" in r["summary"]
    mgr.stop_all()


@pytest.mark.parametrize("mode,code,needle", [("probe_missing", "probe_not_found", "debug probe"), ("init_error", "init_script_failed", ".pioinit")])
@WINDOWS_FLAKY
def test_start_failures_are_classified(monkeypatch, project, mode, code, needle):
    mgr = DebugManager(opener=fake_opener(mode, with_init_script=False))
    monkeypatch.setattr(debug_tools, "debuggers", mgr)
    monkeypatch.setattr(debugger, "require_pio", lambda: ["pio"])
    monkeypatch.setattr(debug_tools, "project_config", lambda p: {"env:dbg": {"debug_tool": "esp-prog"}})
    r = debug_tools.pio_debug_start(project_dir=str(project), env="dbg", timeout_s=5)
    assert r["ok"] is False and r["error"] == code, r
    assert needle in r["summary"] and r["debug_tool"] == "esp-prog"
    assert mgr.list() == []
    with pytest.raises(DebugStartError):
        mgr.start(str(project), "dbg", timeout_s=5)
    mgr.stop_all()


@WINDOWS_FLAKY
def test_start_timeout_kills_process(monkeypatch, project):
    mgr = DebugManager(opener=fake_opener("never_prompt", with_init_script=False))
    monkeypatch.setattr(debug_tools, "debuggers", mgr)
    monkeypatch.setattr(debugger, "require_pio", lambda: ["pio"])
    monkeypatch.setattr(debug_tools, "project_config", lambda p: {"env:dbg": {}})
    r = debug_tools.pio_debug_start(project_dir=str(project), env="dbg", timeout_s=3)
    assert r["ok"] is False and r["error"] == "start_timeout" and "Initializing remote target" in r["output_tail"]
    assert mgr.list() == []


def test_policy_blocks_start(monkeypatch, project):
    monkeypatch.setenv("PLATFORMIO_MCP_POLICY", "build_only")
    r = debug_tools.pio_debug_start(project_dir=str(project))
    assert r["ok"] is False and r["error"] == "policy_denied"


def test_missing_project_dir(tmp_path):
    r = debug_tools.pio_debug_start(project_dir=str(tmp_path))
    assert r["ok"] is False and r["error"] == "not_found"
