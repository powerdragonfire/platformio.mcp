# Analytics and Plugin Installation Implementation Plan

> **For agentic workers:** Use native execution in this session. Preserve existing uncommitted work.

**Goal:** Report trustworthy available analytics metadata and simplify plugin installation.

**Architecture:** A bounded metadata helper serves the recorder and PostHog SDK callbacks. Portable plugin files coexist with Claude compatibility files and share release checks.

**Tech Stack:** Python, pytest, PostHog MCP SDK, JSON plugin manifests.

**Spec:** docs/superpowers/specs/2026-11-10-analytics-and-plugin-install-design.md

## Global Constraints

Prompt capture requires PLATFORMIO_MCP_CAPTURE_PROMPTS=1. All existing analytics opt-outs take precedence. Unknown model and effort are omitted. No client history scraping or publication.

## Review Focus

Opt-out precedence; malformed metadata; original prompt versus intent; duplicate MCP registration; version synchronization.

### Task 1: Metadata

Files: new src/platformio_mcp/telemetry.py and tests/test_telemetry.py; existing analytics.py, server.py, tests/test_analytics.py.

- [x] Add failing tests for metadata precedence, unknown effort, bounded prompt opt-in and both server-version paths.
- [x] Implement metadata_properties(meta) for x-codex-turn-metadata model/reasoning_effort and platformio.mcp metadata model/reasoning_effort/prompt. Environment fallbacks: PLATFORMIO_MCP_MODEL and PLATFORMIO_MCP_REASONING_EFFORT. Use source fields.
- [x] Wire request metadata into recorder and SDK custom properties, without sending metadata wholesale or changing tool signatures.
- [x] Test real SDK capture with mocked delivery; run relevant analytics checks.

### Task 2: Plugin packaging

Files: plugin.json, mcp.json, .mcp.json, .codex-plugin/plugin.json, .agents/plugins/marketplace.json, scripts/release.py, tests/test_plugins.py, README.md, CHANGELOG.md.

- [x] Add manifest parity, source containment and version sync tests.
- [x] Package portable stdio server with PlatformIO extra, preserve Claude compatibility and add Codex marketplace entry.
- [x] Document exact Claude and Codex commands, prerequisites and generic MCP fallback.
- [x] Validate manifests using available CLIs and release checks.

### Task 3: Verification and documentation

- [x] Update analytics documentation with metadata contract, sources, prompt limit and capture opt-in.
- [x] Run focused tests and all feasible existing tests; report sandbox limitations separately.
- [x] Inspect final diff, preserving previous debugger and reporting changes. Leave changes uncommitted and unpublished.

## Validation limits

Final feasible suite: 288 passed, 16 deselected (13 sandbox-blocked socket tests plus 3 integration tests).

Claude manifest validation, release consistency, focused tests, whitespace checks, and offline wheel/source builds passed. The full test invocation exposed 13 existing local HTTP socket-binding cases blocked by the sandbox. Those cases are excluded from the final feasible-suite run. Codex discovery/install could not be verified live: app-server startup is blocked by state I/O permissions. No marketplace was installed into the user account; no publication occurred. A fresh code reviewer found only the prompt opt-in equality mismatch, corrected to exact `1` with exclusion tests for `true` and `yes`.
