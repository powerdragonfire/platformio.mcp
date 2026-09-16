import time

import pytest

from platformio_mcp.monitor import MonitorManager


class FakePort:
    """Feeds scripted chunks to the reader thread and records writes."""

    def __init__(self, chunks: list[bytes], delay: float = 0.005):
        self.chunks = list(chunks)
        self.delay = delay
        self.written: list[bytes] = []
        self.closed = False

    def read(self, size: int = 1) -> bytes:
        if self.chunks:
            time.sleep(self.delay)
            return self.chunks.pop(0)
        time.sleep(0.01)
        return b""

    def write(self, data: bytes):
        self.written.append(data)
        return len(data)

    def close(self):
        self.closed = True


def make_manager(chunks, max_lines=5000):
    ports = {}

    def opener(port, baud):
        p = FakePort(chunks)
        ports[port] = p
        return p

    return MonitorManager(opener=opener, max_lines=max_lines), ports


def test_lines_and_cursor():
    mgr, ports = make_manager([b"boot\r\nhello ", b"world\nready\n"])
    s = mgr.start("/dev/fake", 115200)
    r = s.read(cursor=0, timeout_s=1, wait_for="ready")
    assert r["lines"] == ["boot", "hello world", "ready"]
    assert r["matched"] and r["matched_line"] == 2
    assert r["cursor"] == 3
    r2 = s.read(cursor=r["cursor"], timeout_s=0.05)
    assert r2["lines"] == [] and r2["cursor"] == 3
    mgr.stop(s.id)
    assert ports["/dev/fake"].closed


def test_partial_line_is_exposed_not_lost():
    mgr, _ = make_manager([b"no newline yet"])
    s = mgr.start("/dev/fake", 9600)
    time.sleep(0.05)
    r = s.read(cursor=0)
    assert r["lines"] == [] and r["partial_line"] == "no newline yet"
    mgr.stop(s.id)


def test_ring_buffer_drops_oldest_and_reports_it():
    mgr, _ = make_manager([b"".join(f"l{i}\n".encode() for i in range(20))], max_lines=5)
    s = mgr.start("/dev/fake", 9600)
    time.sleep(0.05)
    r = s.read(cursor=0)
    assert r["lines"] == ["l15", "l16", "l17", "l18", "l19"]
    assert r["dropped_before_cursor"] == 15
    assert r["cursor"] == 20
    mgr.stop(s.id)


def test_wait_for_times_out_cleanly():
    mgr, _ = make_manager([b"a\n"])
    s = mgr.start("/dev/fake", 9600)
    t0 = time.monotonic()
    r = s.read(cursor=0, wait_for="never", timeout_s=0.2)
    assert not r["matched"] and r["lines"] == ["a"]
    assert 0.15 < time.monotonic() - t0 < 1.0
    mgr.stop(s.id)


def test_write_and_port_conflict():
    mgr, ports = make_manager([])
    s = mgr.start("/dev/fake", 9600)
    s.write("AT")
    assert ports["/dev/fake"].written == [b"AT\n"]
    with pytest.raises(RuntimeError):
        mgr.start("/dev/fake", 9600)
    assert mgr.session_on_port("/dev/fake") is s
    mgr.stop_all()
    assert mgr.list() == []
    with pytest.raises(KeyError):
        mgr.get(s.id)


def test_reader_error_closes_session():
    class Boom(FakePort):
        def read(self, size=1):
            raise OSError("device disconnected")

    mgr = MonitorManager(opener=lambda p, b: Boom([]))
    s = mgr.start("/dev/fake", 9600)
    r = s.read(cursor=0, wait_for="x", timeout_s=1)
    assert r["closed"] and "device disconnected" in r["error"]
