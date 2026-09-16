# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `bad_regex` structured error when a `wait_for`, `until`, `expect`, `fail_on`, or `filter` pattern is not a valid regular expression.
- Log pruning: the log directory keeps the newest 200 files (`PLATFORMIO_MCP_MAX_LOGS` overrides).
- GitHub Actions CI on Linux, macOS, and Windows, plus an integration job that builds the native fixture with real PlatformIO.

### Fixed
- Toolchain prefix derivation kept the caller's path separator, fixing `pio_decode_backtrace` and `pio_size_report` on Windows.
- Any unexpected exception inside a tool now returns `ok: false` with the exception text instead of a bare "Error executing tool".

## [0.1.0] - 2026-09-16

First release.

### Added
- 29 MCP tools over the PlatformIO CLI: system info, board search, project init and inspection, build / upload / clean / targets, serial device listing, background serial monitor sessions and one-shot capture, Unity tests, static analysis, and package management.
- `pio_flash_and_verify`: flash, then watch the boot log until an `expect` regex (pass), a crash signature (fail), or a timeout; failures are decoded automatically.
- `pio_decode_backtrace`: resolve ESP32 Guru Meditation backtraces and Cortex-M HardFault dumps to function, file, and line with the toolchain's `addr2line`.
- `pio_size_report`: PlatformIO's RAM/Flash percentages plus GNU `size`/`nm` breakdowns of sections, biggest symbols, and per-file totals.
- Safety policy via `PLATFORMIO_MCP_POLICY` (`full`, `build_only`, `read_only`).
- `install` subcommand that registers the server with Claude Code, Claude Desktop, Cursor, Codex, and Windsurf, or prints the JSON snippet.
- Optional `[platformio]` extra so `uvx "platformio.mcp[platformio]"` works on machines without PlatformIO.
