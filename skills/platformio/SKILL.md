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

## Port problems

- `upload_failed` with `port_error` set: read `port_diagnosis.hint`. If `held_by_session`, retry with `stop_open_sessions=true`; if `held_by_processes`, ask the user to close that program; `no_response` means the board is not in bootloader mode.
- `pio_port_diagnose(port)` answers the same question before flashing.

## Over-the-air

- Board on Wi-Fi with ArduinoOTA in the sketch: `pio_upload_ota(host="<ip or name.local>", auth="<password>")`. `no_response` means `ArduinoOTA.handle()` is not running; `device_rejected` means the partition table has no OTA slot, so run `pio_partition_table`.

## ESP32 flash layout and core dumps

- Before changing `board_build.partitions`, or when a flash succeeds but the device misbehaves: `pio_partition_table(project_dir, env, read_device=true)`. Fix `issues` in severity order; `device_table_mismatch` is fixed by a full `pio_upload`, never by `pio_run_target("program")`.
- Crash with no backtrace on serial: `pio_coredump(project_dir, env)`; with the `coredump` extra installed the result already holds the crashed task and backtrace.

## Runtime memory and power

- Suspected leak, fragmentation, or stack overflow: keep a monitor session open and call `pio_memory_watch(session_id, seconds=30)`. `leak_suspected` and the per-task `stack` table tell you what to fix; if `formats` is empty, add the `instrumentation_hint` snippet to the firmware and flash again.
- Battery budget: `pio_power_profile(source="serial" | "ppk2", seconds=30, voltage_mv=3300)`; use `trigger` with the firmware's monitor `session_id` to start at a known point.

## Dependencies

- Odd link errors, wrong library version picked, or LDF recursion: `pio_deps_check(project_dir, env, build=true)`. `name_collision` is resolved by pinning `owner/Name@version` or deleting the duplicate; never by reordering `lib_deps` alone.

## Live debugging

- When prints are not enough and a debug probe is attached: `pio_debug_start(project_dir, env)` (flashes and halts at the init break), then `pio_debug_cmd(session_id, "break src/main.cpp:42")`, `"continue"`, `"bt"`, `"p variable"`, `"next"`. `continue` blocks until a stop or `timeout_s`; send `"interrupt"` to halt a running target. `pio_debug_stop` before any flash.

## Policy

If a tool returns `error: "policy_denied"`, the server runs with `PLATFORMIO_MCP_POLICY=build_only` or `read_only`. Tell the user which action was blocked and do not try to work around it.
