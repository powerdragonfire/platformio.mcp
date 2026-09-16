---
name: platformio
description: Use when working in a PlatformIO project (platformio.ini present) or when asked to build, flash, monitor, test, or debug firmware for ESP32, Arduino, STM32, RP2040, or any board PlatformIO supports. Drives the platformio MCP tools in the right order and reads their results correctly.
---

# PlatformIO firmware loop

The `platformio` MCP server gives you hands on the board. Use its tools instead of running `pio` in a shell: they return parsed errors, memory usage, and boot-log verdicts instead of 40 KB of compiler noise.

## Start of session

1. `pio_system_info` once. If `ok` is false, relay the install hint and stop.
2. `pio_project_envs(project_dir)` to learn the envs, board, and monitor speed. Pass an absolute `project_dir` to every later call.
3. New project? `pio_list_boards(query)` to find the board id, then `pio_project_init`. Never hand-write `platformio.ini` for a new project.

## Build

- `pio_build(project_dir, env)`. Read `errors` first: each has `file`, `line`, `column`, `message`. Fix them in source, build again. Ignore `warnings` unless asked.
- `memory.flash.percent` and `memory.ram.percent` tell you how full the part is. Above 90% flash, call `pio_size_report` before adding features.

## Flash and verify (preferred over upload + monitor)

- `pio_flash_and_verify(project_dir, env, expect="<line the firmware prints when healthy>", timeout_s=30)`.
- `verdict` is `pass`, `fail`, `timeout`, or `upload_failed`.
  - `fail`: the boot log matched a crash signature. `decoded.frames` already holds function, file, and line for every address. Open the top resolved frame and fix the cause.
  - `timeout`: nothing matched. Check `lines`; if empty, the baud is wrong or the board prints on another port. Try `pio_list_devices` and pass `monitor_port`.
  - `upload_failed`: read `upload.summary`. Usual causes: wrong port, board in a monitor session, boot button needed.
- Only use `pio_upload` + `pio_monitor_capture` when you need raw output without a verdict.

## Serial sessions

- `pio_monitor_start` returns a `session_id`; `pio_monitor_read(session_id, cursor, wait_for=regex)` blocks until the pattern arrives. Keep the returned `cursor` for the next read.
- Stop the session (`pio_monitor_stop`) before any flash. `pio_upload` refuses while a session holds the port.

## Crashes

- Any output containing `Guru Meditation`, `Backtrace:`, `HardFault`, or `abort() was called`: call `pio_decode_backtrace(project_dir, env, text=<the output>)` or pass `session_id`. Quote the resolved `function (file:line)` chain to the user and fix the first frame that is in project code, not in the framework.
- If no frames resolve, the ELF does not match what is flashed. Rebuild and flash again before debugging.

## Tests and analysis

- `pio_test(project_dir, env)` runs Unity tests. Native envs run on the host; embedded envs run on the board.
- `pio_check(project_dir, env)` returns cppcheck / clang-tidy defects by severity. Fix `high` first.

## Libraries

- `pio_pkg_search(query)` then `pio_pkg_install(spec, project_dir, env)`. This edits `platformio.ini` for you; do not edit `lib_deps` by hand as well.

## Policy

If a tool returns `error: "policy_denied"`, the server runs with `PLATFORMIO_MCP_POLICY=build_only` or `read_only`. Tell the user which action was blocked and do not try to work around it.
