# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- 11 new tools (40 total):
  - `pio_upload_ota`: flash over Wi-Fi to ArduinoOTA (espota) boards with a reachability pre-check and failures mapped to the fix.
  - `pio_port_diagnose`: explain why a serial port cannot be used (our session, another process via lsof/fuser, permissions, board not answering).
  - `pio_partition_table`: validate the ESP32 partition CSV (alignment, overlaps, flash fit, OTA slots, app fit) and, with `read_device`, diff it against the table on the chip.
  - `pio_coredump`: read the core dump partition over esptool and decode it with the optional `esp-coredump` analyzer.
  - `pio_memory_watch`: heap and stack telemetry parsed from serial output with leak, fragmentation, and stack-headroom verdicts.
  - `pio_power_profile`: current draw from a serial meter or a Nordic PPK2 with sleep/active split, energy, and battery estimate.
  - `pio_deps_check`: library name collisions, unpinned specs, missing and leftover libraries, circular dependencies, and the LDF dependency graph.
  - `pio_debug_start` / `pio_debug_cmd` / `pio_debug_stop` / `pio_debug_list`: live GDB sessions over `pio debug --interface=gdb` with GDB/MI parsed into structured results.
- `stop_open_sessions` on `pio_upload` and `pio_run_target` releases our own monitor sessions before flashing; upload failures now carry `port_error` and `port_diagnosis`.
- Optional extras `coredump` (esp-coredump) and `power` (ppk2-api).
- Community files: CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, issue forms, PR template, Dependabot config.
- Open Plugins layout (`.mcp.json`, `plugin.json`, `skills/platformio/SKILL.md`, `rules/platformio.mdc`) so the repo installs as a Claude Code or Cursor plugin and lists on cursor.directory.
- `bad_regex` structured error when a `wait_for`, `until`, `expect`, `fail_on`, or `filter` pattern is not a valid regular expression.
- Log pruning: the log directory keeps the newest 200 files (`PLATFORMIO_MCP_MAX_LOGS` overrides).
- `server.json` and the README ownership marker for the official MCP Registry.
- GitHub Actions CI on Linux, macOS, and Windows, plus an integration job that builds the native fixture with real PlatformIO.

### Fixed
- Toolchain prefix derivation kept the caller's path separator, fixing `pio_decode_backtrace` and `pio_size_report` on Windows.
- Any unexpected exception inside a tool now returns `ok: false` with the exception text instead of a bare "Error executing tool".

### Security
- Raised the `mcp` floor from `>=1.0` to `>=2.2`. The old floor let scanners resolve to SDK releases affected by CVE-2025-53365, CVE-2025-53366, CVE-2025-66416, CVE-2026-52869, and CVE-2026-59950 (all fixed by mcp 1.28.1). The server already required the 2.x API, so no behaviour changes.

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
