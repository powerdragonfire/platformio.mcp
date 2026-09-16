"""Background serial monitor sessions with a bounded ring buffer and cursor-based reads.

`pio device monitor` refuses to run without an interactive terminal, so sessions talk to the
port directly with pyserial. Lines are numbered from 0; a cursor is "the next line index I have
not seen", so `read(cursor=0)` returns everything still in the buffer.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol


class ByteSource(Protocol):
    """What a session needs from a port: pyserial's Serial satisfies it, so do test fakes."""

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...
    def close(self) -> None: ...


@dataclass
class Session:
    id: str
    port: str
    baud: int
    source: ByteSource
    max_lines: int = 5000
    lines: deque[str] = field(init=False)
    first_index: int = 0  # absolute index of lines[0]
    next_index: int = 0  # absolute index the next appended line will get
    partial: str = ""
    error: str | None = None
    closed: bool = False
    started_at: float = field(default_factory=time.time)
    bytes_received: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    changed: threading.Condition = field(init=False)
    thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        self.lines = deque(maxlen=self.max_lines)
        self.changed = threading.Condition(self.lock)

    def _append(self, line: str) -> None:
        # Must hold self.lock.
        if len(self.lines) == self.lines.maxlen:
            self.first_index += 1
        self.lines.append(line)
        self.next_index += 1

    def feed(self, data: bytes) -> None:
        text = data.decode("utf-8", errors="replace")
        with self.changed:
            self.bytes_received += len(data)
            buf = self.partial + text
            parts = buf.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            self.partial = parts.pop()
            for p in parts:
                self._append(p)
            self.changed.notify_all()

    def _reader(self) -> None:
        try:
            while not self.closed:
                data = self.source.read(4096)
                if data:
                    self.feed(data)
                else:
                    time.sleep(0.01)
        except Exception as exc:  # port unplugged, permission lost, fake exhausted
            with self.changed:
                self.error = f"{type(exc).__name__}: {exc}"
                self.closed = True
                self.changed.notify_all()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._reader, name=f"pio-monitor-{self.id}", daemon=True)
        self.thread.start()

    def read(self, cursor: int = 0, max_lines: int = 200, wait_for: str | None = None, timeout_s: float = 0) -> dict:
        pattern = re.compile(wait_for) if wait_for else None
        deadline = time.monotonic() + max(timeout_s, 0)
        with self.changed:
            while True:
                start = max(cursor, self.first_index)
                available = self.next_index - start
                new = list(self.lines)[start - self.first_index : start - self.first_index + max(available, 0)] if available > 0 else []
                matched = None
                if pattern:
                    for i, line in enumerate(new):
                        if pattern.search(line):
                            matched = start + i
                            new = new[: i + 1]
                            break
                if pattern and matched is None and not self.closed:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self.changed.wait(timeout=remaining)
                        continue
                if not pattern and not new and timeout_s > 0 and not self.closed:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self.changed.wait(timeout=remaining)
                        continue
                truncated = len(new) > max_lines
                new = new[:max_lines]
                next_cursor = start + len(new)
                return {
                    "session_id": self.id,
                    "lines": new,
                    "cursor": next_cursor,
                    "dropped_before_cursor": max(0, self.first_index - cursor) if cursor < self.first_index else 0,
                    "more_available": truncated or next_cursor < self.next_index,
                    "matched": matched is not None,
                    "matched_line": matched,
                    "partial_line": self.partial,
                    "closed": self.closed,
                    "error": self.error,
                }

    def write(self, text: str, newline: bool = True) -> int:
        if self.closed:
            raise RuntimeError(f"session {self.id} is closed")
        data = (text + ("\n" if newline else "")).encode("utf-8")
        self.source.write(data)
        return len(data)

    def stop(self) -> None:
        with self.changed:
            self.closed = True
            self.changed.notify_all()
        try:
            self.source.close()
        except Exception:
            pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def info(self) -> dict:
        with self.lock:
            return {
                "session_id": self.id,
                "port": self.port,
                "baud": self.baud,
                "lines_buffered": len(self.lines),
                "next_cursor": self.next_index,
                "bytes_received": self.bytes_received,
                "uptime_s": round(time.time() - self.started_at, 1),
                "closed": self.closed,
                "error": self.error,
            }


def open_serial(port: str, baud: int) -> ByteSource:
    import serial  # pyserial

    return serial.Serial(port, baudrate=baud, timeout=0.2)


class MonitorManager:
    def __init__(self, opener=open_serial, max_lines: int = 5000) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._opener = opener
        self._max_lines = max_lines

    def start(self, port: str, baud: int) -> Session:
        with self._lock:
            for s in self._sessions.values():
                if s.port == port and not s.closed:
                    raise RuntimeError(f"port {port} is already open in session {s.id}; stop it first")
            source = self._opener(port, baud)
            session = Session(id=uuid.uuid4().hex[:8], port=port, baud=baud, source=source, max_lines=self._max_lines)
            self._sessions[session.id] = session
            session.start()
            return session

    def get(self, session_id: str) -> Session:
        s = self._sessions.get(session_id)
        if s is None:
            raise KeyError(f"unknown session {session_id}; call pio_monitor_list")
        return s

    def stop(self, session_id: str) -> dict:
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

    def list(self) -> list[dict]:
        return [s.info() for s in self._sessions.values()]

    def session_on_port(self, port: str) -> Session | None:
        for s in self._sessions.values():
            if s.port == port and not s.closed:
                return s
        return None
