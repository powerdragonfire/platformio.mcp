# platformio.mcp

An MCP server that gives coding agents hands on embedded hardware through the PlatformIO CLI. This glossary covers the words used for crashes and the platforms the server runs against.

## Crashes

**Panic output**:
The text a device prints on the serial port when firmware crashes: the cause line, a register dump, and either a backtrace line (Xtensa) or a stack memory dump (RISC-V).
_Avoid_: Crash log, Guru dump, stack trace

**Core dump**:
A binary snapshot of task stacks and registers that ESP-IDF writes to a flash partition at crash time, read back from the device after reboot.
_Avoid_: Crash dump, coredump file

**Symbolizing**:
Turning one code address into a function name, source file and line, using the firmware ELF.
_Avoid_: Decoding, resolving, addr2line-ing

**Unwinding**:
Rebuilding the chain of calling frames from the registers and stack memory in panic output, before any frame is symbolized.
_Avoid_: Stack walking, backtrace decoding

**Crash decoding**:
The whole path from panic output to a list of symbolized frames: unwinding when the architecture needs it, then symbolizing.
_Avoid_: Exception decoding, trace breaking

## Platforms

**Upstream espressif32**:
The `espressif32` platform published by PlatformIO in its registry.
_Avoid_: Official platform, stock platform

**pioarduino**:
The community `espressif32` platform maintained outside the PlatformIO registry, installed by URL, which supports every Espressif chip and Arduino core 3.x.
_Avoid_: pioarduino platform fork, Tasmota platform
