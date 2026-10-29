# CH32H4 SPI loopback fixture example

This fixture demonstrates a single-board serial assertion workflow for repeatable hardware checks.

## Required wiring

- `PB15 (MOSI)` ↔ `PB14 (MISO)`
- Board powered over USB with a data-capable cable

## Firmware under test

- `src/main.cpp` prints `BOOT_OK` after setup and then continuously prints:
  - `SPI_LOOPBACK_OK` when loopback succeeds
  - `test-failure: ...` when loopback fails

## Run plan

Use this MCP flow:

1. `pio_test_plan_validate(plan_path="/abs/path/examples/ch32h4-spi-loopback/hardware-test-plan.json")`
2. `pio_test_plan_start(plan_path="/abs/path/examples/ch32h4-spi-loopback/hardware-test-plan.json")`
3. Poll `pio_test_plan_status(run_id="...")`
4. Optional cancellation: `pio_test_plan_cancel(run_id="...")`

The JSON report includes per-iteration outcome (`pass`, `fail`, `timeout`, `infra_error`), source revision, dirty state, firmware hash, and evidence log paths for non-pass iterations.
