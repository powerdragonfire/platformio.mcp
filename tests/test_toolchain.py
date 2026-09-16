from platformio_mcp import toolchain as tc

ESP32_PANIC = """
Guru Meditation Error: Core  1 panic'ed (LoadProhibited). Exception was unhandled.
Core  1 register dump:
PC      : 0x400d1ff4  PS      : 0x00060030  A0      : 0x800d2010  A1      : 0x3ffb1f30
A2      : 0x00000000  A3      : 0x3ffb1f50  A4      : 0x00000001  A5      : 0x00000000
EXCVADDR: 0x00000000  LBEG    : 0x4000c2e0  LEND    : 0x4000c2f6  LCOUNT  : 0xffffffff

Backtrace: 0x400d1ff4:0x3ffb1f30 0x400d2010:0x3ffb1f50 0x40088b6d:0x3ffb1f70

Rebooting...
ets Jun  8 2016 00:22:57

rst:0xc (SW_CPU_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)
"""

ESP_IDF_ABORT = """
abort() was called at PC 0x400d5a3c on core 0
Backtrace:0x40081b2a:0x3ffb0c50 0x40085e39:0x3ffb0c70 0x4008a8d2:0x3ffb0c90 |<-CORRUPTED
"""

CORTEX_M = """
*** HardFault ***
r0: 0x00000000  r1: 0x20000abc
r12: 0x00000000  lr: 0x0800abcd  pc: 0x08001234  xpsr: 0x61000000
"""

A2L_OUT = """0x400d1ff4: DisplayTask::run() at /proj/firmware/src/display_task.cpp:22
0x400d2010: DisplayTask::run() at /proj/firmware/src/display_task.cpp:23
 (inlined by) TaskBase::loop() at /proj/firmware/src/task.h:41 (discriminator 3)
0x40088b6d: vPortTaskWrapper at /esp/freertos/port.c:143
0x3ffb1234: ?? ??:0
"""

SIZE_A = """.pio/build/view/firmware.elf  :
section                    size         addr
.rtc.text                     0   1074528256
.iram0.vectors             1024   1074266112
.iram0.text               57180   1074267136
.dram0.data               10108   1073470304
.dram0.bss                 9880   1073480416
.flash.rodata            162216   1061158944
.flash.text              190234   1074593816
.debug_info             5614940            0
.xt.prop._ZTV11DisplayTask   12            0
Total                   6045594
"""

SIZE_B = """   text\t   data\t    bss\t    dec\t    hex\tfilename
 248438\t 172324\t   9880\t 430642\t  69232\t.pio/build/view/firmware.elf
"""

NM_OUT = """3f40064c 00002e66 d Roboto_Light_60Bitmaps
400d1ff4 00000737 T DisplayTask::run()\t/proj/firmware/src/display_task.cpp:22
400e8f38 00003202 T _vfprintf_r\t/newlib/libc/stdio/vfprintf.c:663
3ffc1958 00000800 b s_stub_min_stack
400d0000 00000000 T empty_symbol
400f19a4 00000767 t uart_rx_intr_handler_default\t/esp/driver/uart.c:120
3ffc2000 00000100 B DisplayTask::buffer\t/proj/firmware/src/display_task.cpp:9
"""


def test_derive_prefix():
    assert tc.derive_prefix("/x/bin/xtensa-esp32-elf-gcc") == "/x/bin/xtensa-esp32-elf-"
    assert tc.derive_prefix("C:\\pio\\arm-none-eabi-gcc.exe") == "C:\\pio\\arm-none-eabi-"
    assert tc.derive_prefix("avr-gcc") == "avr-"
    assert tc.derive_prefix("gcc") == ""
    assert tc.derive_prefix("/usr/bin/clang") == ""
    assert tc.derive_prefix("gcc-13") == ""


def test_extract_esp32_panic():
    crash = tc.extract_crash(ESP32_PANIC)
    assert crash["causes"][0].startswith("Guru Meditation Error: Core  1 panic'ed (LoadProhibited)")
    assert crash["reset_reasons"] == ["SW_CPU_RESET"]
    assert crash["backtrace_corrupted"] is False
    roles = {(a.address, a.role) for a in crash["addresses"]}
    assert ("0x400d1ff4", "pc") in roles
    assert ("0x400d2010", "lr") in roles  # A0 window bits stripped: 0x800d2010 -> 0x400d2010
    assert ("0x00000000", "register") in roles  # EXCVADDR kept, marked as register
    frames = [a for a in crash["addresses"] if a.role == "backtrace"]
    assert [a.address for a in frames] == ["0x400d1ff4", "0x400d2010", "0x40088b6d"]
    assert [a.frame for a in frames] == [0, 1, 2]
    assert not any(a.address == "0x3ffb1f30" for a in crash["addresses"])  # stack pointers are not decoded


def test_extract_idf_abort_and_corrupted():
    crash = tc.extract_crash(ESP_IDF_ABORT)
    assert crash["backtrace_corrupted"] is True
    assert any(a.address == "0x400d5a3c" and a.role == "pc" for a in crash["addresses"])
    assert len([a for a in crash["addresses"] if a.role == "backtrace"]) == 3


def test_extract_cortex_m():
    crash = tc.extract_crash(CORTEX_M)
    assert "HardFault" in crash["causes"][0]
    roles = {(a.address, a.role) for a in crash["addresses"]}
    assert ("0x08001234", "pc") in roles and ("0x0800abcd", "lr") in roles
    assert not any(a.role == "other" for a in crash["addresses"])


def test_extract_nothing_falls_back_to_all_hex():
    crash = tc.extract_crash("MAC 0x12345678 booted")
    assert [a.role for a in crash["addresses"]] == ["other"]
    assert tc.extract_crash("hello world")["addresses"] == []


def test_parse_addr2line():
    frames = tc.parse_addr2line(A2L_OUT)
    f = frames["0x400d2010"]
    assert f["function"] == "DisplayTask::run()" and f["file"].endswith("display_task.cpp") and f["line"] == 23
    assert f["inlined"] == [{"function": "TaskBase::loop()", "file": "/proj/firmware/src/task.h", "line": 41}]
    assert frames["0x3ffb1234"]["resolved"] is False and frames["0x3ffb1234"]["function"] is None
    assert frames["0x40088b6d"]["resolved"] is True


def test_parse_size_sections_classifies_and_drops_debug():
    rows = tc.parse_size_sections(SIZE_A)
    names = {r["section"]: r for r in rows}
    assert ".debug_info" not in names and ".rtc.text" not in names and ".xt.prop._ZTV11DisplayTask" not in names
    assert names[".flash.text"]["region"] == "flash" and names[".flash.rodata"]["region"] == "flash"
    assert names[".dram0.bss"]["region"] == "ram" and names[".iram0.vectors"]["region"] == "ram"
    assert names[".dram0.data"]["region"] == "both" and names[".iram0.text"]["region"] == "both"
    assert rows[0]["section"] == ".flash.text"
    assert tc.classify_section(".text", 0x8000000) == "flash" and tc.classify_section(".bss", 0x20000000) == "ram" and tc.classify_section(".data", 1) == "both"


def test_parse_size_totals():
    assert tc.parse_size_totals(SIZE_B) == {"text": 248438, "data": 172324, "bss": 9880, "flash_estimate": 420762, "ram_estimate": 182204}
    assert tc.parse_size_totals("garbage") is None


def test_parse_nm_and_group():
    rows = tc.parse_nm(NM_OUT)
    assert rows[0]["name"] == "_vfprintf_r" and rows[0]["size"] == 0x3202 and rows[0]["kind"] == "code"
    assert not any(r["name"] == "empty_symbol" for r in rows)
    run = next(r for r in rows if r["name"] == "DisplayTask::run()")
    assert run["file"] == "/proj/firmware/src/display_task.cpp" and run["line"] == 22
    assert next(r for r in rows if r["name"] == "Roboto_Light_60Bitmaps")["kind"] == "data"
    assert next(r for r in rows if r["name"] == "DisplayTask::buffer")["kind"] == "bss"
    files = tc.group_by_file(rows, "/proj")
    top = files[0]
    assert top["file"] == "<no debug info>" or top["size"] >= files[1]["size"]
    disp = next(f for f in files if f["file"] == "firmware/src/display_task.cpp")
    assert disp["size"] == 0x737 + 0x100 and disp["symbols"] == 2 and disp["code"] == 0x737 and disp["bss"] == 0x100
