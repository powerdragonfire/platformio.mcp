# PostHog MCP analytics optimisation: platformio.mcp

Date: 2026-11-10. Produced with the `grill-with-skills` skill, auto-agreed. Base: `main` after PR #27 (v0.4.1).

## Baseline (2026-11-10)

| Signal | Value | Source |
|---|---|---|
| Event name | `$mcp_tool_call`, one per tool call | `analytics.py::posthog_payload` |
| Properties sent | `$mcp_tool_name`, `$mcp_is_error`, `$mcp_error_type`, `$mcp_duration_ms`, `$mcp_input_keys`, `$mcp_client_name`, `$mcp_client_version`, `$mcp_server_name`, `$mcp_server_version`, `session`, `policy`, `os`, `python`, `$lib`, `$process_person_profile=false` | same |
| Transport | one urllib POST per event to `/i/v0/e/`, daemon thread, queue 1000 | `Recorder._send_loop` |
| Shutdown | `lifespan` in `server.py` never calls `Recorder.flush`; queued events die with the daemon thread | `server.py::lifespan` |
| Missing for PostHog MCP Analytics views | `$session_id` (Sessions tab and per-tool session counts group by it), `$mcp_source`, `$lib_version`, `$mcp_protocol_version`, `$mcp_initialize`, `$mcp_tools_list` with `$mcp_listed_tool_names` | `posthog:debugging-mcp-analytics` event vocabulary |
| Official SDK | `posthog.mcp.instrument()` supports `mcp` 2.x `MCPServer`; not installed; runtime deps are only `mcp` and `pyserial` | PostHog docs, `pyproject.toml` |
| Live data | 24 `$mcp_tool_call` events since 2026-11-10 12:42 UTC, dashboard 1102201, alert on failed calls | PostHog EU project 341541 |

## Agreed answers

1. Keep the stdlib sender. The `posthog` SDK adds 4 transitive packages, captures `$mcp_parameters` and `$mcp_response` by default, and injects `context`, `conversation_id` and `llm_model` into every tool schema. Each of those breaks the README privacy promise or the zero dependency `uvx` install.
2. No intent capture. `$mcp_intent` is free text written by the agent and can carry project details. Revisit when weekly installs pass 100.
3. Send `$session_id` equal to the per process `Recorder.session`. Over stdio one process serves one client connection, so the process session is the real MCP session. Keep the plain `session` property for local log compatibility.
4. Batch through `POST {host}/batch/` with `{"api_key", "batch": [...]}`. Flush at 20 events or 5 seconds, whichever comes first. `lifespan` calls `Recorder.flush(timeout=2.0)` on shutdown. The local JSONL write stays synchronous.
5. Emit `$mcp_initialize` once per process (client name and version, `$mcp_protocol_version`) and `$mcp_tools_list` once per process with `$mcp_listed_tool_names`. This enables zombie tool detection: tools listed and never called.
6. Add to every event: `$mcp_source="posthog_mcp_analytics"`, `$lib_version`, `$mcp_protocol_version` when the session exposes it. Add `board` (public PlatformIO board id) and `platform` when a tool receives them as arguments. This folds answer 4 of `2026-11-08-growth-and-usage.md` into this row. Environment names, paths, ports and all other argument values stay excluded.
7. IP handling: PostHog project setting "Discard client IP data" (`anonymize_ips`) is on since 2026-11-10 16:10 UTC, maintainer confirmed. PostHog docs: GeoIP enrichment runs before the IP is discarded, so country and city stay.
8. Release 0.5.0 in the same PR (new events and transport change). README "Usage analytics" section and `NOTICE` list the new events and properties.
9. The old `row-usage-analytics` shrinks to `.github/workflows/traffic.yml` and `stats --share` in `cli.py`, and starts after this row merges, because both rows touch `analytics.py`.
10. Dashboard work (sessions per install, tools per session, zombie tools) waits for the release, because PostHog insights need the new properties in ingested data first.

## Rows

| Row | Skills | Source | Scope | Reviewer | Test tier | Estimate |
|---|---|---|---|---|---|---|
| posthog-mcp-parity | tdd, posthog:debugging-mcp-analytics, superpowers:verification-before-completion | `~/.agents/skills`, posthog plugin, superpowers plugin | `src/platformio_mcp/analytics.py`, `src/platformio_mcp/server.py`, `src/platformio_mcp/__init__.py`, `pyproject.toml`, `tests/test_analytics.py`, `README.md` "Usage analytics" section | maintainer plus `security-review` pass | unit plus one live capture to PostHog EU with `client=verification` | 3 to 4 hours |
| usage-analytics (reduced) | tdd, superpowers:test-driven-development | installed | `.github/workflows/traffic.yml`, `stats --share` in `src/platformio_mcp/cli.py` | maintainer | unit | 1 to 2 hours, after parity merges |

Gap skill search: not needed. Installed skills cover both rows.

## Owner tasks (no agent)

- Confirm the "Discard client IP data" project setting.
- Merge the 0.5.0 PR. Merge to `main` auto tags and publishes.
- After 0.5.0 events arrive: add sessions, tools per session and zombie tool insights to dashboard 1102201.
