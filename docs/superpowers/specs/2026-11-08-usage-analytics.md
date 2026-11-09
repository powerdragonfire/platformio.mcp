# Usage analytics for platformio.mcp

Settled 2026-11-08 with `grill-with-skills`, answers auto-agreed.

## Parts and skills

| Part | Skill | Source | Reviewer | Test tier |
|---|---|---|---|---|
| Recorder and server hook (`analytics.py`, `server.py`) | superpowers:test-driven-development | plugin | maintainer | unit + stdio e2e |
| `stats` CLI (`cli.py`) | superpowers:test-driven-development | plugin | maintainer | unit |
| Docs (README, CHANGELOG, CONTRIBUTING) | terse writing rules | output style | maintainer | none |

No gap skills. One row, implemented inline.

## Decisions

1. Scope: one event per MCP tool call. Adoption (PyPI downloads, GitHub clones and views) comes from public counters, documented in CONTRIBUTING.
2. Hook: `PlatformIOServer(MCPServer)` overrides `call_tool`. Internal tool-to-tool calls are not counted. Argument validation failures and unknown tools are counted with the exception class as `error`.
3. Sink: local JSONL at `~/.platformio-mcp/analytics/usage.jsonl`, on by default. `PLATFORMIO_MCP_ANALYTICS=off` disables it. `PLATFORMIO_MCP_ANALYTICS_DIR` moves it.
4. Remote (revised 2026-11-09): PostHog EU Cloud, on by default, chosen over Sentry, Logfire and local-only for exact counts, visible `ok:false` failures, no new dependency, and the smallest payload. Anonymous events (`$process_person_profile: false`) keyed by a random install id. Off under `DO_NOT_TRACK=1`, `PLATFORMIO_MCP_ANALYTICS=local`, or `CI`. One stderr notice on the first sending run. `PLATFORMIO_MCP_ANALYTICS_URL` stays as an extra sink for a user's own endpoint. One POST per event from a daemon thread, 2 s timeout, queue capped at 1000, failures dropped.
5. Fields: `v, ts, session, tool, ok, error, duration_ms, arg_keys, server_version, policy, os, python, client{name,version}`. Argument values, summaries, paths, ports and serial output are never recorded.
6. Recording starts only in `serve()`. `create_server()` defaults to `record_usage=False`, so tests and embedders write nothing.
7. Rotation: at 5 MB the file moves to `usage.jsonl.1`, one previous file kept.
8. `platformio.mcp stats [--days N] [--json]`: calls, sessions, date range, clients, per-tool calls, ok rate, nearest-rank p50/p95 ms, top 10 errors.
9. No new MCP tool. The tool count stays at 44, so manifests and README tool tables are unchanged.
