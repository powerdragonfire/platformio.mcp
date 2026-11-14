# RISC-V unwinding uses Espressif's esp-idf-panic-decoder

RISC-V Espressif chips (C2, C3, C5, C6, C61, H2, H4, P4, S31) print registers and a raw stack dump on a panic, with no backtrace line, so crash decoding needs unwinding before symbolizing. Jason2866's ESP-Decoder VS Code extension already unwinds these dumps, but it and the trbr code it vendors are GPL-3.0, and copying them would force platformio.mcp off the MIT license. trbr's RISC-V unwinder is itself a port of `gdb_panic_server.py` from Espressif's `esp-idf-panic-decoder` (Apache-2.0, depends only on pyelftools and pyparsing), so platformio.mcp depends on that package directly and drives the toolchain's `riscv32-esp-elf-gdb` against it, the way `idf.py monitor` does.

## Considered Options

- Port ESP-Decoder's TypeScript: rejected, GPL-3.0 relicensing.
- Write a GDB stub from scratch in Python: rejected, duplicates Espressif's maintained code for no gain.
- Ask Jason2866 and dankeboy36 to relicense under MIT: rejected, blocks on two third parties for code Espressif already publishes.

## Consequences

- `esp-idf-panic-decoder` is a hard dependency, so a plain `uvx platformio.mcp` install can unwind.
- Unwinding needs the GDB package (`tool-riscv32-esp-elf-gdb`). Without it the decoder falls back to symbolizing stack words that point into code, marked `stack_scan`, and tells the agent how to install GDB.
- The package parses one register dump only, so multi-core panic output is cut down to the crashed core before it is handed over.
