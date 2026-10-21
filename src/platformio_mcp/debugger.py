"""Live GDB sessions through `pio debug --interface=gdb`, speaking GDB/MI over pipes.

`pio debug --interface=gdb -- <gdb args>` starts the debug server (OpenOCD, J-Link, ...), writes a
`.pioinit` script (target remote, load, tbreak main) into a temp dir under .pio/, and execs the
toolchain gdb with `-x .pioinit` plus our `--interpreter=mi2`. Once the script finishes PlatformIO
itself resumes the target to the init break, so a fresh session normally reports a `*stopped` at main.
Everything gdb prints is a GDB/MI record; `parse_mi_line` turns one line into a structured record and
is a pure function so it is unit-tested on captured output.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pio import require_pio

INIT_DONE_BANNER = "PlatformIO: Initialization completed"
INIT_SCRIPT_NAME = ".pioinit"
PROMPT = "(gdb)"
GDB_CLIENT_ARGS = ["--interpreter=mi2", "-x", INIT_SCRIPT_NAME]

EXEC_COMMANDS = {
    "continue", "c", "fg", "next", "n", "step", "s", "stepi", "si", "nexti", "ni", "finish", "fin",
    "until", "u", "advance", "jump", "j", "run", "r", "start", "starti", "signal", "reverse-continue",
    "reverse-next", "reverse-step", "reverse-finish", "rc", "rn", "rs", "pio_restart_target",
}
EXEC_MI_COMMANDS = {"-exec-continue", "-exec-next", "-exec-step", "-exec-finish", "-exec-until", "-exec-run", "-exec-jump", "-exec-next-instruction", "-exec-step-instruction", "-exec-return"}

_RECORD_RE = re.compile(r"^(?P<token>\d*)(?P<sigil>[\^*+=~&@])(?P<rest>.*)$")


# --- GDB/MI parsing ---------------------------------------------------------------------------


def unescape_c_string(text: str) -> str:
    """Decode the C-string body gdb emits: \\n, \\t, \\", \\\\ and octal bytes such as \\303\\251 (UTF-8)."""
    out = bytearray()
    i, n = 0, len(text)
    simple = {"n": b"\n", "t": b"\t", "r": b"\r", '"': b'"', "\\": b"\\", "a": b"\x07", "b": b"\b", "f": b"\f", "v": b"\v", "e": b"\x1b"}
    while i < n:
        ch = text[i]
        if ch != "\\" or i + 1 >= n:
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in simple:
            out += simple[nxt]
            i += 2
            continue
        if nxt in "01234567":
            j = i + 1
            digits = ""
            while j < n and len(digits) < 3 and text[j] in "01234567":
                digits += text[j]
                j += 1
            out.append(int(digits, 8) & 0xFF)
            i = j
            continue
        out += nxt.encode("utf-8")
        i += 2
    return out.decode("utf-8", errors="replace")


class _MiValueParser:
    """Recursive-descent parser for the `name=value,...` part of MI result and async records."""

    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0

    def parse_results(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while self.i < len(self.s):
            if self.s[self.i] == ",":
                self.i += 1
                continue
            name = self._ident()
            if not name:
                break
            if self.i < len(self.s) and self.s[self.i] == "=":
                self.i += 1
                value = self.parse_value()
            else:
                value = None
            if name in out:  # repeated keys (several bkpt= in one record) become a list
                prev = out[name]
                out[name] = prev + [value] if isinstance(prev, list) else [prev, value]
            else:
                out[name] = value
        return out

    def _ident(self) -> str:
        j = self.i
        while j < len(self.s) and (self.s[j].isalnum() or self.s[j] in "-_"):
            j += 1
        name = self.s[self.i : j]
        self.i = j
        return name

    def parse_value(self) -> Any:
        if self.i >= len(self.s):
            return None
        ch = self.s[self.i]
        if ch == '"':
            return self._string()
        if ch == "{":
            self.i += 1
            inner = self._until_close("{", "}")
            return _MiValueParser(inner).parse_results()
        if ch == "[":
            self.i += 1
            inner = self._until_close("[", "]")
            return _MiValueParser(inner).parse_list()
        # bare token (rare); read until comma
        j = self.i
        while j < len(self.s) and self.s[j] != ",":
            j += 1
        val = self.s[self.i : j]
        self.i = j
        return val

    def parse_list(self) -> list[Any]:
        items: list[Any] = []
        while self.i < len(self.s):
            if self.s[self.i] == ",":
                self.i += 1
                continue
            if self.s[self.i] in '"{[':
                items.append(self.parse_value())
            else:
                name = self._ident()
                if self.i < len(self.s) and self.s[self.i] == "=":
                    self.i += 1
                    items.append({name: self.parse_value()})
                else:
                    break
        return items

    def _string(self) -> str:
        assert self.s[self.i] == '"'
        j = self.i + 1
        buf = []
        while j < len(self.s):
            ch = self.s[j]
            if ch == "\\" and j + 1 < len(self.s):
                buf.append(self.s[j : j + 2])
                j += 2
                continue
            if ch == '"':
                break
            buf.append(ch)
            j += 1
        self.i = j + 1
        return unescape_c_string("".join(buf))

    def _until_close(self, open_ch: str, close_ch: str) -> str:
        depth, j, in_str = 1, self.i, False
        while j < len(self.s):
            ch = self.s[j]
            if in_str:
                if ch == "\\":
                    j += 2
                    continue
                if ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    inner = self.s[self.i : j]
                    self.i = j + 1
                    return inner
            j += 1
        inner = self.s[self.i :]
        self.i = len(self.s)
        return inner


def parse_mi_results(text: str) -> dict[str, Any]:
    return _MiValueParser(text).parse_results()


@dataclass
class MiRecord:
    kind: str  # result | exec | status | notify | console | log | target | prompt | other
    raw: str
    token: str | None = None
    cls: str | None = None  # done, running, error, exit, stopped, ...
    payload: dict[str, Any] = field(default_factory=dict)
    text: str | None = None  # unescaped stream text, or the raw line for `other`

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        if self.token:
            d["token"] = self.token
        if self.cls:
            d["class"] = self.cls
        if self.payload:
            d["payload"] = self.payload
        if self.text is not None:
            d["text"] = self.text
        return d


def parse_mi_line(line: str) -> MiRecord:
    line = line.rstrip("\r\n")
    if line.strip() == PROMPT:
        return MiRecord(kind="prompt", raw=line)
    m = _RECORD_RE.match(line)
    if not m:
        return MiRecord(kind="other", raw=line, text=line)
    token = m.group("token") or None
    sigil, rest = m.group("sigil"), m.group("rest")
    if sigil in "~&@":
        kind = {"~": "console", "&": "log", "@": "target"}[sigil]
        body = rest.strip()
        if body.startswith('"') and body.endswith('"') and len(body) >= 2:
            body = body[1:-1]
        return MiRecord(kind=kind, raw=line, token=token, text=unescape_c_string(body))
    kind = {"^": "result", "*": "exec", "+": "status", "=": "notify"}[sigil]
    cls, _, tail = rest.partition(",")
    return MiRecord(kind=kind, raw=line, token=token, cls=cls.strip(), payload=parse_mi_results(tail) if tail else {})


def frame_summary(frame: dict[str, Any] | None) -> dict[str, Any] | None:
    if not frame:
        return None
    line = frame.get("line")
    try:
        line = int(line) if line is not None else None
    except (TypeError, ValueError):
        pass
    return {
        "function": frame.get("func"),
        "file": frame.get("fullname") or frame.get("file"),
        "line": line,
        "address": frame.get("addr"),
        "args": frame.get("args") or [],
    }


def stop_summary(rec: MiRecord) -> dict[str, Any]:
    p = rec.payload
    out = {"reason": p.get("reason"), "frame": frame_summary(p.get("frame")), "thread_id": p.get("thread-id")}
    for key in ("signal-name", "signal-meaning", "bkptno", "exit-code", "disp"):
        if key in p:
            out[key.replace("-", "_")] = p[key]
    return out


# --- errors ---------------------------------------------------------------------------------------

class DebugStartError(RuntimeError):
    def __init__(self, code: str, summary: str, output: str = "") -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.output = output


def classify_start_failure(output: str) -> tuple[str, str]:
    """Map the text a dead `pio debug` left behind to an error code and a one-line explanation."""
    low = output.lower()
    if "error in sourced command file" in low or (INIT_SCRIPT_NAME in low and "error" in low):
        return "init_script_failed", "gdb failed while running the .pioinit script (target connect or `load` failed); the debug server may be up but the target did not answer."
    if re.search(r"unable to open ftdi device|no device found|libusb_error|unable to find|can't find|could not find or open|failed to open|no such device|open failed|unable to open|error: couldn't open|device not found", low):
        return "probe_not_found", "the debug server could not open the debug probe; check the USB cable, the probe drivers (udev rules on Linux), and `debug_tool` in platformio.ini."
    if "debug_tool" in low or "debugging is not supported" in low or "debuginvalidoptionserror" in low or "could not launch debug server" in low:
        return "debug_tool_missing", "PlatformIO could not configure a debugger for this env; set `debug_tool` in platformio.ini to a tool the board supports (`pio boards <board>` lists them) and make sure the debug server package is installed."
    if "unknown environment" in low or "not found in" in low and "platformio.ini" in low:
        return "bad_env", "the environment does not exist in platformio.ini."
    if "error" in low and ("compil" in low or "build" in low or "scons" in low):
        return "build_failed", "the debug build failed before gdb started; run pio_build and fix the errors."
    return "debug_exited", "`pio debug` exited before gdb accepted commands."


# --- session ----------------------------------------------------------------------------------------

Opener = Callable[[list[str], str], subprocess.Popen]


def open_pio_debug(cmd: list[str], cwd: str) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({"PLATFORMIO_NO_ANSI": "true", "PLATFORMIO_DISABLE_PROGRESSBAR": "true", "PYTHONUNBUFFERED": "1"})
    kwargs: dict[str, Any] = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True  # own process group so SIGINT can reach gdb without hitting us
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)


@dataclass
class DebugSession:
    id: str
    project_dir: str
    env: str | None
    proc: subprocess.Popen
    command: list[str]
    max_records: int = 4000
    records: deque[MiRecord] = field(init=False)
    first_index: int = 0
    next_index: int = 0
    prompt_count: int = 0
    running: bool = False
    closed: bool = False
    exit_code: int | None = None
    error: str | None = None
    last_stop: dict[str, Any] | None = None
    stop_count: int = 0
    started_at: float = field(default_factory=time.time)
    init_script_path: str | None = None
    init_script: str | None = None
    debug_tool: str | None = None
    gdb_version: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    changed: threading.Condition = field(init=False)
    thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        self.records = deque(maxlen=self.max_records)
        self.changed = threading.Condition(self.lock)

    # -- reader -----------------------------------------------------------------------------------

    def _append(self, rec: MiRecord) -> None:
        # Must hold self.lock.
        if len(self.records) == self.records.maxlen:
            self.first_index += 1
        self.records.append(rec)
        self.next_index += 1
        if rec.kind == "prompt":
            self.prompt_count += 1
        elif rec.kind == "exec" and rec.cls == "stopped":
            self.running = False
            self.last_stop = stop_summary(rec)
            self.stop_count += 1
        elif rec.cls == "running" and rec.kind in ("exec", "result"):
            self.running = True
        elif rec.kind == "result" and rec.cls == "exit":
            self.closed = True
        elif rec.kind == "console" and rec.text:
            m = re.search(r"PlatformIO: debug_tool = (\S+)", rec.text)
            if m:
                self.debug_tool = m.group(1)
            if self.gdb_version is None and rec.text.startswith("GNU gdb"):
                self.gdb_version = rec.text.strip().splitlines()[0]

    def feed_line(self, line: str) -> None:
        rec = parse_mi_line(line)
        with self.changed:
            self._append(rec)
            self.changed.notify_all()

    def _reader(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self.feed_line(line)
        except Exception as exc:
            with self.changed:
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            code = None
            try:
                code = self.proc.wait(timeout=5)
            except Exception:
                pass
            with self.changed:
                self.exit_code = code
                self.closed = True
                self.running = False
                self.changed.notify_all()

    def start_reader(self) -> None:
        self.thread = threading.Thread(target=self._reader, name=f"pio-debug-{self.id}", daemon=True)
        self.thread.start()

    # -- waiting ------------------------------------------------------------------------------------

    def _slice(self, start: int, end: int | None = None) -> list[MiRecord]:
        # Must hold self.lock.
        end = self.next_index if end is None else end
        lo = max(start, self.first_index)
        if end <= lo:
            return []
        items = list(self.records)
        return items[lo - self.first_index : end - self.first_index]

    def wait_for_prompt(self, since_index: int, timeout_s: float) -> int | None:
        """Block until a prompt record at index >= since_index exists; return its index or None on timeout/exit."""
        deadline = time.monotonic() + max(timeout_s, 0)
        with self.changed:
            while True:
                for i, rec in enumerate(self._slice(since_index), start=max(since_index, self.first_index)):
                    if rec.kind == "prompt":
                        return i
                if self.closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.changed.wait(timeout=remaining)

    def wait_for_stop(self, since_index: int, timeout_s: float) -> int | None:
        deadline = time.monotonic() + max(timeout_s, 0)
        with self.changed:
            while True:
                for i, rec in enumerate(self._slice(since_index), start=max(since_index, self.first_index)):
                    if rec.kind == "exec" and rec.cls == "stopped":
                        return i
                if self.closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.changed.wait(timeout=remaining)

    def text_since(self, start: int = 0, limit: int = 4000) -> str:
        with self.lock:
            recs = self._slice(start)
        parts = []
        for r in recs:
            if r.kind in ("console", "log", "target", "other") and r.text:
                parts.append(r.text if r.text.endswith("\n") else r.text + "\n")
            elif r.kind == "result" and r.cls == "error":
                parts.append(f"error: {r.payload.get('msg')}\n")
        return "".join(parts)[-limit:]

    # -- commands ------------------------------------------------------------------------------------

    def _write(self, line: str) -> None:
        if self.closed or self.proc.stdin is None:
            raise RuntimeError(f"debug session {self.id} is closed")
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise RuntimeError(f"debug session {self.id}: gdb is gone ({exc})") from exc

    def run(self, command: str, timeout_s: float = 30) -> dict[str, Any]:
        """Send one gdb command and collect its records up to the next prompt; for execution commands also wait for `*stopped`."""
        command = command.strip()
        if not command:
            raise ValueError("command is empty")
        if command in ("interrupt", "-exec-interrupt"):
            return self.interrupt(timeout_s)
        if self.running:
            raise RuntimeError(f"the target is running; send 'interrupt' first (session {self.id}).")
        first_word = command.split()[0]
        is_exec = first_word in EXEC_COMMANDS or first_word in EXEC_MI_COMMANDS
        started = time.monotonic()
        with self.lock:
            start = self.next_index
        self._write(command)
        prompt_at = self.wait_for_prompt(start, timeout_s)
        timed_out = prompt_at is None and not self.closed
        end = prompt_at if prompt_at is not None else None
        with self.lock:
            recs = self._slice(start, end)
        result = next((r for r in reversed(recs) if r.kind == "result"), None)
        went_running = (result is not None and result.cls == "running") or any(r.kind == "exec" and r.cls == "running" for r in recs)
        stopped_rec = next((r for r in recs if r.kind == "exec" and r.cls == "stopped"), None)
        if (is_exec or went_running) and stopped_rec is None and prompt_at is not None and (result is None or result.cls != "error"):
            remaining = timeout_s - (time.monotonic() - started)
            stop_at = self.wait_for_stop(prompt_at, max(remaining, 0))
            if stop_at is not None:
                # include the stop event and the prompt that follows it, if it has arrived
                after = self.wait_for_prompt(stop_at, min(2.0, max(remaining, 0.2)))
                with self.lock:
                    recs = self._slice(start, (after + 1) if after is not None else None)
                stopped_rec = next((r for r in recs if r.kind == "exec" and r.cls == "stopped"), None)
            else:
                timed_out = not self.closed
        return self._summarize(command, recs, result, stopped_rec, timed_out, round(time.monotonic() - started, 2))

    def interrupt(self, timeout_s: float = 10) -> dict[str, Any]:
        started = time.monotonic()
        with self.lock:
            start = self.next_index
        if not self.running:
            return self._summarize("interrupt", [], None, None, False, 0.0, note="target was not running")
        self._write("-exec-interrupt")
        stop_at = self.wait_for_stop(start, min(2.0, timeout_s))
        if stop_at is None and os.name != "nt" and not self.closed:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            stop_at = self.wait_for_stop(start, max(timeout_s - 2.0, 0.5))
        after = self.wait_for_prompt(stop_at, 2.0) if stop_at is not None else None
        with self.lock:
            recs = self._slice(start, (after + 1) if after is not None else None)
        stopped_rec = next((r for r in recs if r.kind == "exec" and r.cls == "stopped"), None)
        result = next((r for r in reversed(recs) if r.kind == "result"), None)
        return self._summarize("interrupt", recs, result, stopped_rec, stopped_rec is None and not self.closed, round(time.monotonic() - started, 2))

    def _summarize(self, command: str, recs: list[MiRecord], result: MiRecord | None, stopped_rec: MiRecord | None, timed_out: bool, duration_s: float, note: str | None = None) -> dict[str, Any]:
        console = "".join(r.text or "" for r in recs if r.kind == "console")
        log = "".join(r.text or "" for r in recs if r.kind == "log")
        target = "".join(r.text or "" for r in recs if r.kind == "target")
        other = "\n".join(r.text or "" for r in recs if r.kind == "other")
        error = result.payload.get("msg") if result is not None and result.cls == "error" else None
        return {
            "session_id": self.id,
            "command": command,
            "result_class": result.cls if result is not None else None,
            "result": result.payload if result is not None else {},
            "console": console.splitlines(),
            "log": log.splitlines(),
            "target_output": target.splitlines(),
            "other_output": other.splitlines() if other else [],
            "error": error,
            "stopped": stop_summary(stopped_rec) if stopped_rec is not None else None,
            "running": self.running,
            "timed_out": timed_out,
            "closed": self.closed,
            "exit_code": self.exit_code,
            "duration_s": duration_s,
            "records": [r.to_dict() for r in recs][-200:],
            "note": note,
        }

    # -- lifecycle -------------------------------------------------------------------------------------

    def stop(self, grace_s: float = 3.0) -> None:
        if not self.closed and self.proc.poll() is None:
            try:
                self._write("-gdb-exit")  # PlatformIO intercepts this and resumes the target first
            except RuntimeError:
                pass
            try:
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=grace_s)
                    except subprocess.TimeoutExpired:
                        pass
        with self.changed:
            self.closed = True
            self.running = False
            self.changed.notify_all()
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=grace_s)

    def info(self) -> dict[str, Any]:
        with self.lock:
            return {
                "session_id": self.id,
                "project_dir": self.project_dir,
                "env": self.env,
                "debug_tool": self.debug_tool,
                "gdb_version": self.gdb_version,
                "running": self.running,
                "closed": self.closed,
                "exit_code": self.exit_code,
                "error": self.error,
                "last_stop": self.last_stop,
                "stop_count": self.stop_count,
                "records_buffered": len(self.records),
                "uptime_s": round(time.time() - self.started_at, 1),
                "init_script_path": self.init_script_path,
                "pid": self.proc.pid,
            }


def find_init_script(project_dir: str) -> Path | None:
    """PlatformIO writes .pioinit into a fresh `.pio/.piodebug-*/` directory for each session; newest wins."""
    candidates = sorted(Path(project_dir).glob(f".pio/.piodebug-*/{INIT_SCRIPT_NAME}"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


class DebugManager:
    def __init__(self, opener: Opener = open_pio_debug, max_records: int = 4000) -> None:
        self._sessions: dict[str, DebugSession] = {}
        self._lock = threading.Lock()
        self._opener = opener
        self._max_records = max_records

    def build_command(self, project_dir: str, env: str | None, load: bool, extra_gdb_args: list[str] | None = None) -> list[str]:
        cmd = require_pio() + ["debug", "-d", project_dir]
        if env:
            cmd += ["-e", env]
        cmd += ["--load-mode", "always" if load else "manual", "--interface=gdb", "--"]
        cmd += GDB_CLIENT_ARGS + list(extra_gdb_args or [])
        return cmd

    def start(self, project_dir: str, env: str | None = None, load: bool = True, timeout_s: float = 90, extra_gdb_args: list[str] | None = None, init_break_wait_s: float = 15) -> DebugSession:
        with self._lock:
            for s in self._sessions.values():
                if s.project_dir == project_dir and not s.closed:
                    raise RuntimeError(f"a debug session ({s.id}) is already open for {project_dir}; stop it first or use pio_debug_cmd.")
            cmd = self.build_command(project_dir, env, load, extra_gdb_args)
            proc = self._opener(cmd, project_dir)
            session = DebugSession(id=uuid.uuid4().hex[:8], project_dir=project_dir, env=env, proc=proc, command=cmd, max_records=self._max_records)
            self._sessions[session.id] = session
            session.start_reader()
        try:
            self._await_ready(session, timeout_s, init_break_wait_s)
        except DebugStartError:
            self.stop(session.id)
            raise
        return session

    def _await_ready(self, session: DebugSession, timeout_s: float, init_break_wait_s: float) -> None:
        t0 = time.monotonic()
        prompt_at = session.wait_for_prompt(0, timeout_s)
        script = find_init_script(session.project_dir)
        if script is not None:
            session.init_script_path = str(script)
            try:
                session.init_script = script.read_text(encoding="utf-8", errors="replace")
            except OSError:
                session.init_script = None
        if prompt_at is None:
            output = session.text_since(0)
            if session.closed:
                code, why = classify_start_failure(output)
                raise DebugStartError(code, f"{why} (pio debug exit code {session.exit_code}).", output)
            raise DebugStartError("start_timeout", f"gdb did not reach a prompt within {timeout_s:.0f}s; the debug server may be waiting on the probe or `load` is still flashing. Raise timeout_s or check the probe.", output)
        # PlatformIO resumes the target to `debug_init_break` (default: tbreak main) once .pioinit finishes.
        remaining = max(0.0, min(init_break_wait_s, timeout_s - (time.monotonic() - t0)))
        session.wait_for_stop(prompt_at, remaining)
        if session.closed:
            output = session.text_since(0)
            code, why = classify_start_failure(output)
            raise DebugStartError(code, f"{why} (pio debug exit code {session.exit_code}).", output)

    def get(self, session_id: str) -> DebugSession:
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(f"unknown debug session {session_id}; call pio_debug_list")
        return s

    def stop(self, session_id: str) -> dict[str, Any]:
        s = self.get(session_id)
        s.stop()
        with self._lock:
            self._sessions.pop(session_id, None)
        return s.info()

    def stop_all(self) -> None:
        for sid in list(self._sessions):
            try:
                self.stop(sid)
            except Exception:
                pass

    def list(self) -> list[dict[str, Any]]:
        return [s.info() for s in self._sessions.values()]

    def session_for(self, project_dir: str) -> DebugSession | None:
        for s in self._sessions.values():
            if s.project_dir == project_dir and not s.closed:
                return s
        return None


debuggers = DebugManager()

