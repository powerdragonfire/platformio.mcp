"""Usage analytics: one event per MCP tool call.

An event holds the tool name, ok/error code, duration, the names of the arguments passed, the server
version, the policy, the OS, the Python version, the MCP client's name and version, and the protocol
version. The `board` and `platform` argument values are kept when they look like PlatformIO registry ids.
Other argument values, summaries, paths, ports, and serial output never enter an event.
Allowlisted host metadata can supply model and effort. Explicit local prompt opt-in also permits a
bounded original prompt from host metadata; no chat history is read.

Every event goes to a local JSONL log (~/.platformio-mcp/analytics/usage.jsonl). When the release carries a
PostHog project key, events also go to PostHog EU Cloud as anonymous events keyed by a random install id,
in `/batch/` POSTs of up to 20 events every 5 seconds. PostHog also gets one `$mcp_initialize` and one
`$mcp_tools_list` (with the listed tool names) per process.

PLATFORMIO_MCP_ANALYTICS=off   no local log, nothing sent
PLATFORMIO_MCP_ANALYTICS=local local log only, nothing sent
DO_NOT_TRACK=1 or CI set       nothing sent to PostHog
PLATFORMIO_MCP_ANALYTICS_URL   also POST each raw event as JSON to this URL (your own endpoint)
"""

from __future__ import annotations

import json
import math
import os
import platform
import queue
import re
import sys
import threading
import time
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__, telemetry
from .core import policy

SCHEMA_VERSION = 1
MAX_BYTES = 5 * 1024 * 1024
POST_TIMEOUT_S = 2.0
BATCH_SIZE = 20
BATCH_INTERVAL_S = 5.0
MCP_SOURCE = "posthog_mcp_analytics"
# Tool arguments whose values are public PlatformIO registry ids. Every other argument sends its name only.
PUBLIC_ID_ARGS = ("board", "platform")
# Needs a letter, so an IP address or a bare number never passes as an id.
PUBLIC_ID = re.compile(r"(?=[^A-Za-z]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")
LOG_NAME = "usage.jsonl"
INSTALL_ID_NAME = "install_id"
# Write-only project key for the maintainers' PostHog EU project. PostHog designs these keys to ship in
# clients: they can send events and cannot read data. Empty means nothing is sent.
POSTHOG_KEY = "phc_rYUPu9XV2gUsvsYXJaHGCqE9TUseFfnPfusBkyVTKwZx"
POSTHOG_HOST = "https://eu.i.posthog.com"
NOTICE = (
    "platformio.mcp sends anonymous usage events to PostHog EU: one per tool call (tool name, ok/error, duration, "
    "argument names, board and platform ids, versions, OS, MCP client, process session id), plus one connect event "
    "and one tool-list event per process. Available model/effort metadata is included. Original prompts from host "
    "metadata are captured only with PLATFORMIO_MCP_CAPTURE_PROMPTS=1 (up to 2000 characters). "
    "No other argument values, paths, ports, or serial output. "
    "Opt out with DO_NOT_TRACK=1 or PLATFORMIO_MCP_ANALYTICS=local. Details: README, 'Usage analytics'."
)


def _mode() -> str:
    value = os.environ.get("PLATFORMIO_MCP_ANALYTICS", "on").strip().lower()
    if value in ("off", "0", "false", "no"):
        return "off"
    return "local" if value == "local" else "on"


def enabled() -> bool:
    return _mode() != "off"


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def remote_allowed() -> bool:
    """PostHog sending: on unless the user opted out or the server runs in CI, where counts would be noise."""
    return _mode() == "on" and not _truthy("DO_NOT_TRACK") and not _truthy("CI")


def posthog_key() -> str:
    return os.environ.get("PLATFORMIO_MCP_POSTHOG_KEY", POSTHOG_KEY).strip()


def install_id(directory: Path) -> tuple[str, bool]:
    """A random id stored beside the log, so PostHog counts installs without knowing who runs them.

    Returns (id, created). A fresh id means this is the first run that sends events.
    """
    path = directory / INSTALL_ID_NAME
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing, False
    except OSError:
        pass
    new = uuid.uuid4().hex
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(new + "\n", encoding="utf-8")
    except OSError:
        pass
    return new, True


def posthog_event(event: dict[str, Any], distinct_id: str) -> dict[str, Any]:
    """A `$mcp_tool_call` event with PostHog's MCP property names, so PostHog's MCP analytics views
    break calls down by tool, client, error, duration and session. Our own extras keep plain names."""
    client = event.get("client") or {}
    properties = {
        "$mcp_tool_name": event["tool"],
        "$mcp_is_error": not event["ok"],
        "$mcp_error_type": event["error"],
        "$mcp_duration_ms": event["duration_ms"],
        "$mcp_input_keys": event["arg_keys"],
        **event.get("metadata", {}),
        **_common_properties(event, client),
    }
    return {"event": "$mcp_tool_call", "distinct_id": distinct_id, "properties": properties, "timestamp": event["ts"]}


def _common_properties(event: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
    """Properties every PostHog event carries. `$session_id` is the per-process session: over stdio one
    process serves one client connection, so PostHog's Sessions tab groups calls by MCP session."""
    properties = {
        "$session_id": event["session"],
        "$mcp_source": MCP_SOURCE,
        "$mcp_client_name": client.get("name"),
        "$mcp_client_version": client.get("version"),
        "$mcp_server_name": "platformio",
        "$mcp_server_version": event["server_version"],
        "platformio_mcp_version": event["server_version"],
        "session": event["session"],
        "policy": event["policy"],
        "os": event["os"],
        "python": event["python"],
        "$lib": "platformio.mcp",
        "$lib_version": __version__,
        "$process_person_profile": False,
    }
    if event.get("protocol_version"):
        properties["$mcp_protocol_version"] = event["protocol_version"]
    for key in PUBLIC_ID_ARGS:
        if event.get(key):
            properties[key] = event[key]
    return properties


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def public_ids(arguments: dict[str, Any] | None) -> dict[str, str]:
    """`board` and `platform` arguments shaped like PlatformIO registry ids (`esp32dev`, `espressif32`).

    A path, URL, IP address, or anything with `/`, `:`, `@` or spaces fails the shape check and is dropped.
    """
    ids = {}
    for key in PUBLIC_ID_ARGS:
        value = (arguments or {}).get(key)
        if isinstance(value, str) and PUBLIC_ID.fullmatch(value):
            ids[key] = value
    return ids


def analytics_dir() -> Path:
    return Path(os.environ.get("PLATFORMIO_MCP_ANALYTICS_DIR", Path.home() / ".platformio-mcp" / "analytics"))


def log_path() -> Path:
    return analytics_dir() / LOG_NAME


class Recorder:
    """Appends events to the local log and queues them for each remote sink.

    The raw URL sink gets one POST per event. PostHog gets `/batch/` POSTs of up to BATCH_SIZE events,
    sent when the batch fills or BATCH_INTERVAL_S after its first event, whichever comes first.
    """

    def __init__(self, path: Path, url: str | None = None, posthog: tuple[str, str, str] | None = None) -> None:
        """posthog is (host, key, distinct_id)."""
        self.path = path
        self.url = url
        self.posthog = posthog
        self.session = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._sender_lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, dict[str, Any] | None]] = queue.Queue(maxsize=1000)
        self._sender: threading.Thread | None = None
        self._lifecycle_sent: set[str] = set()

    @classmethod
    def from_env(cls, remote: bool = True) -> Recorder:
        """remote=False keeps the local log and drops PostHog sending (the SDK layer sends instead)."""
        posthog = None
        key = posthog_key()
        if remote and key and remote_allowed():
            distinct_id, created = install_id(analytics_dir())
            if created:
                print(NOTICE, file=sys.stderr)
            posthog = (os.environ.get("PLATFORMIO_MCP_POSTHOG_HOST", POSTHOG_HOST).rstrip("/"), key, distinct_id)
        return cls(log_path(), os.environ.get("PLATFORMIO_MCP_ANALYTICS_URL") or None, posthog)

    def _context(self, client: dict[str, str] | None, protocol_version: str | None) -> dict[str, Any]:
        context = {
            "server_version": __version__,
            "policy": policy(),
            "os": platform.system(),
            "python": platform.python_version(),
            "client": client,
        }
        if protocol_version:
            context["protocol_version"] = protocol_version
        return context

    def record(
        self, *, tool: str, ok: bool, error: str | None, duration_ms: int, arg_keys: list[str], client: dict[str, str] | None,
        protocol_version: str | None = None, ids: dict[str, str] | None = None, metadata: Any = None,
    ) -> dict[str, Any]:
        """Keep public ids and allowlisted host metadata; original prompt text needs explicit local opt-in."""
        event = {
            "v": SCHEMA_VERSION,
            "ts": _now(),
            "session": self.session,
            "tool": tool,
            "ok": ok,
            "error": error,
            "duration_ms": duration_ms,
            "arg_keys": sorted(arg_keys),
            **self._context(client, protocol_version),
            **{k: v for k, v in (ids or {}).items() if k in PUBLIC_ID_ARGS},
        }
        properties = telemetry.metadata_properties(metadata)
        if properties:
            event["metadata"] = properties
        try:
            self._append(event)
        except OSError as exc:  # a full disk or read-only home must never break a tool call
            print(f"platformio.mcp analytics: could not write {self.path}: {exc}", file=sys.stderr)
        if self.url:
            self._enqueue("raw", event)
        if self.posthog:
            self._enqueue("posthog", posthog_event(event, self.posthog[2]))
        return event

    def record_lifecycle(
        self, name: str, *, client: dict[str, str] | None, protocol_version: str | None, tool_names: list[str] | None = None,
    ) -> bool:
        """Send `$mcp_initialize` or `$mcp_tools_list` to PostHog, once per process each.

        These go to PostHog only: the local log and the raw URL sink keep one line per tool call.
        Returns True when the event was queued.
        """
        with self._lock:
            if name in self._lifecycle_sent or not self.posthog:
                return False
            self._lifecycle_sent.add(name)
        event = {"ts": _now(), "session": self.session, **self._context(client, protocol_version)}
        properties = _common_properties(event, client or {})
        if tool_names is not None:
            properties["$mcp_listed_tool_names"] = tool_names
        self._enqueue("posthog", {"event": name, "distinct_id": self.posthog[2], "properties": properties, "timestamp": event["ts"]})
        return True

    def _append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size + len(line) > MAX_BYTES:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def _enqueue(self, kind: str, payload: dict[str, Any] | None) -> None:
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            return
        with self._sender_lock:
            if self._sender is None:
                self._sender = threading.Thread(target=self._send_loop, name="platformio-mcp-analytics", daemon=True)
                self._sender.start()

    def _post(self, url: str, body: dict[str, Any]) -> None:
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": f"platformio.mcp/{__version__}"}, method="POST")
            urllib.request.urlopen(req, timeout=POST_TIMEOUT_S).close()
        except Exception:  # the endpoint is best effort; the local log already has the event
            pass

    def _send_batch(self, batch: list[dict[str, Any]]) -> None:
        assert self.posthog is not None
        host, key, _ = self.posthog
        try:
            self._post(f"{host}/batch/", {"api_key": key, "batch": batch})
        finally:
            for _ in batch:
                self._queue.task_done()  # batched events count as unfinished until their POST ends
            batch.clear()

    def _send_loop(self) -> None:
        batch: list[dict[str, Any]] = []
        due = 0.0
        while True:
            timeout = max(due - time.monotonic(), 0.0) if batch else 30.0
            try:
                kind, payload = self._queue.get(timeout=timeout)
            except queue.Empty:
                if batch:
                    self._send_batch(batch)
                    continue
                with self._sender_lock:
                    if self._queue.empty():
                        self._sender = None
                        return
                continue
            if kind == "raw":
                assert self.url and payload is not None
                self._post(self.url, payload)
                self._queue.task_done()
            elif kind == "posthog":
                assert payload is not None
                if not batch:
                    due = time.monotonic() + BATCH_INTERVAL_S
                batch.append(payload)
            else:  # "flush": send the open batch now
                self._queue.task_done()
            if batch and (kind == "flush" or len(batch) >= BATCH_SIZE or time.monotonic() >= due):
                self._send_batch(batch)

    def flush(self, timeout: float = 5.0) -> None:
        """Send the open PostHog batch now and wait until queued POSTs finish or the timeout passes."""
        if self._queue.unfinished_tasks:
            self._enqueue("flush", None)
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)


def client_from_context(context: Any) -> dict[str, str] | None:
    """The MCP client's name and version from a tool Context, when the client sent them."""
    return request_client(getattr(context, "request_context", None))


def request_client(request_context: Any) -> dict[str, str] | None:
    """The MCP client's name and version from a lowlevel ServerRequestContext (the initialize handshake,
    or the request envelope on 2026-07-28+)."""
    try:
        info = request_context.session.client_params.client_info
    except Exception:
        return None
    if info is None:
        return None
    return {"name": str(info.name), "version": str(info.version)}


def protocol_from_context(context: Any) -> str | None:
    """The protocol version the connection negotiated (handshake) or the request declared (2026-07-28+)."""
    version = getattr(getattr(context, "request_context", None), "protocol_version", None)
    return str(version) if version else None


def outcome(result: Any) -> tuple[bool, str | None]:
    """Read ok and the error code from a CallToolResult built from one of our {ok, error, ...} dicts."""
    payload = getattr(result, "structured_content", None)
    if payload is None:
        content = getattr(result, "content", None) or []
        try:
            payload = json.loads(content[0].text)
        except (IndexError, AttributeError, TypeError, ValueError):
            payload = None
    if isinstance(payload, dict) and "ok" in payload:
        ok = bool(payload["ok"])
        return ok, None if ok else str(payload.get("error") or "unknown")
    if getattr(result, "is_error", False):
        return False, "tool_error"
    return True, None


def read_events(path: Path | None = None, days: int | None = None) -> list[dict[str, Any]]:
    """Events from the rotated file and the current file, oldest first, skipping unreadable lines."""
    path = path or log_path()
    cutoff = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events: list[dict[str, Any]] = []
    for p in (path.with_name(path.name + ".1"), path):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and "tool" in event and (cutoff is None or event.get("ts", "") >= cutoff):
                events.append(event)
    return events


def _percentile(sorted_values: list[int], pct: float) -> int:
    """Nearest-rank percentile: the smallest value with at least pct of the samples at or below it."""
    rank = max(math.ceil(pct * len(sorted_values)), 1)
    return sorted_values[rank - 1]


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_tool[e["tool"]].append(e)
    tools = []
    for name, rows in by_tool.items():
        durations = sorted(int(r.get("duration_ms") or 0) for r in rows)
        ok_count = sum(1 for r in rows if r.get("ok"))
        tools.append({
            "tool": name,
            "calls": len(rows),
            "ok_rate": round(ok_count / len(rows), 3),
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
        })
    tools.sort(key=lambda t: (-t["calls"], t["tool"]))
    errors = Counter((e["tool"], e.get("error") or "unknown") for e in events if not e.get("ok"))
    clients = Counter(f"{e['client']['name']} {e['client']['version']}" if e.get("client") else "unknown" for e in events)
    stamps = sorted(e.get("ts", "") for e in events)
    return {
        "calls": len(events),
        "sessions": len({e.get("session") for e in events}),
        "first": stamps[0] if stamps else None,
        "last": stamps[-1] if stamps else None,
        "clients": dict(clients.most_common()),
        "tools": tools,
        "errors": [{"tool": t, "error": err, "count": n} for (t, err), n in errors.most_common(10)],
    }


def format_summary(s: dict[str, Any], path: Path) -> str:
    lines = [
        f"{s['calls']} tool calls in {s['sessions']} sessions, {s['first']} to {s['last']}.",
        "Clients: " + ", ".join(f"{name} ({n})" for name, n in s["clients"].items()),
        "",
        f"{'tool':<26} {'calls':>6} {'ok %':>6} {'p50 ms':>8} {'p95 ms':>8}",
    ]
    for t in s["tools"]:
        lines.append(f"{t['tool']:<26} {t['calls']:>6} {t['ok_rate'] * 100:>6.1f} {t['p50_ms']:>8} {t['p95_ms']:>8}")
    if s["errors"]:
        lines += ["", "Top errors:"]
        lines += [f"  {e['count']:>4}  {e['tool']}: {e['error']}" for e in s["errors"]]
    lines += ["", f"Log: {path}"]
    return "\n".join(lines)
