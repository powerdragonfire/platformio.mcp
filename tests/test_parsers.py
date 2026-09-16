import json
from pathlib import Path

from platformio_mcp import parsers

FIX = Path(__file__).parent / "fixtures"

ESP32_TAIL = """
Linking .pio/build/view/firmware.elf
Retrieving maximum program size .pio/build/view/firmware.elf
Checking size .pio/build/view/firmware.elf
Advanced Memory Usage is available via "PlatformIO Home > Project Inspect"
RAM:   [=         ]  12.3% (used 40388 bytes from 327680 bytes)
Flash: [====      ]  35.6% (used 466977 bytes from 1310720 bytes)
Building .pio/build/view/firmware.bin
========================= [SUCCESS] Took 41.02 seconds =========================
"""

LINKER_FAIL = """
Linking .pio/build/view/firmware.elf
/Users/x/.platformio/packages/toolchain-xtensa32/bin/../lib/gcc/xtensa-esp32-elf/5.2.0/../../../../xtensa-esp32-elf/bin/ld: .pio/build/view/src/main.cpp.o:(.literal._Z5setupv+0x4): undefined reference to `Foo::bar()'
collect2: error: ld returned 1 exit status
*** [.pio/build/view/firmware.elf] Error 1
========================== [FAILED] Took 12.10 seconds ==========================
"""


def test_parse_diagnostics_from_real_clang_log():
    text = (FIX / "build_err.log").read_text()
    diags = parsers.parse_diagnostics(text)
    errors = [d for d in diags if d.kind == "error" and d.file == "src/main.cpp"]
    assert len(errors) == 2
    assert errors[0].line == 7 and errors[0].column == 15
    assert "expected expression" in errors[0].message
    scons = [d for d in diags if d.message.startswith("build step failed")]
    assert scons and scons[0].file == ".pio/build/native/src/main.o"


def test_parse_diagnostics_linker():
    diags = parsers.parse_diagnostics(LINKER_FAIL)
    msgs = [d.message for d in diags]
    assert any("undefined reference to `Foo::bar()'" in m for m in msgs)
    assert parsers.parse_build_result(LINKER_FAIL)["status"] == "failed"


def test_parse_memory():
    mem = parsers.parse_memory(ESP32_TAIL)
    assert mem["ram"]["percent"] == 12.3
    assert mem["flash"]["used_bytes"] == 466977
    assert mem["flash"]["total_bytes"] == 1310720
    assert parsers.parse_memory((FIX / "build_ok.log").read_text()) == {}


def test_parse_build_result():
    ok = parsers.parse_build_result((FIX / "build_ok.log").read_text())
    assert ok["status"] == "success" and ok["took_s"] == 6.57
    bad = parsers.parse_build_result((FIX / "build_err.log").read_text())
    assert bad["status"] == "failed" and bad["environments"] == ["native"]


PKG_SEARCH = """Found 406 packages (page 1 of 41)

bblanchon/ArduinoJson
Library • 7.4.3 • Published on Mon Mar  2 17:23:45 2026
A simple and efficient JSON library for embedded C++. ⭐ 7124 stars on GitHub!

ottowinter/ArduinoJson-esphomelib
Library • 6.15.2 • Published on Thu Jul 16 08:25:30 2020
Fork of the excellent ArduinoJson for easier building in esphomelib
"""


def test_parse_pkg_search():
    res = parsers.parse_pkg_search(PKG_SEARCH)
    assert res["total"] == 406 and res["pages"] == 41
    assert res["packages"][0]["spec"] == "bblanchon/ArduinoJson"
    assert res["packages"][0]["version"] == "7.4.3"
    assert res["packages"][0]["type"] == "library"
    assert res["packages"][1]["owner"] == "ottowinter"


PKG_LIST = """Resolving view dependencies...
Platform espressif32 @ 3.4.0 (required: espressif32 @ 3.4)
├── framework-arduinoespressif32 @ 3.10006.210326 (required: platformio/framework-arduinoespressif32 @ ~3.10006.0)
└── toolchain-xtensa32 @ 2.50200.97 (required: platformio/toolchain-xtensa32 @ ~2.50200.0)

Libraries
├── Simple FOC @ 2.2.0 (required: askuric/Simple FOC @ 2.2.0)
└── TFT_eSPI @ 2.4.25 (required: bodmer/TFT_eSPI @ 2.4.25)
"""


def test_parse_pkg_list():
    rows = parsers.parse_pkg_list(PKG_LIST)
    specs = {r["spec"]: r for r in rows}
    assert specs["espressif32"]["type"] == "platform" and specs["espressif32"]["version"] == "3.4.0"
    assert specs["Simple FOC"]["version"] == "2.2.0"
    assert specs["TFT_eSPI"]["required"] == "bodmer/TFT_eSPI @ 2.4.25"
    assert all(r["env"] == "view" for r in rows)


TARGETS = """Environment    Group     Name         Title                        Description
-------------  --------  -----------  ---------------------------  ----------------------
view           Platform  buildfs      Build Filesystem Image
view           Platform  erase        Erase Flash
view           Platform  size         Program Size                 Calculate program size
"""


def test_parse_targets():
    rows = parsers.parse_targets(TARGETS)
    assert [r["name"] for r in rows] == ["buildfs", "erase", "size"]
    assert rows[2]["description"] == "Calculate program size"


def test_summarize_test_report():
    report = json.loads((FIX / "test.json").read_text())
    s = parsers.summarize_test_report(report)
    assert s["suites"][0]["env"] == "native"
    cases = s["suites"][0]["cases"]
    assert cases[0]["status"] == "PASSED" and cases[0]["line"] == 7
    assert cases[1]["status"] == "FAILED" and cases[1]["message"] == "Expected 6 Was 5"
    assert s["total"] == 3 and s["failed"] == 1


def test_summarize_check_report():
    report = [
        {
            "env": "native",
            "tool": "cppcheck",
            "duration": 0.02,
            "succeeded": True,
            "defects": [
                {"severity": "low", "category": "style", "message": "x", "file": "/p/src/a.cpp", "line": 3, "column": 1, "id": "s1", "cwe": "1"},
                {"severity": "high", "category": "error", "message": "Memory leak: p", "file": "/p/src/main.cpp", "line": 7, "column": 41, "id": "memleak", "cwe": "401"},
            ],
        }
    ]
    s = parsers.summarize_check_report(report, project_dir="/p")
    assert s["defect_count"] == 2 and s["by_severity"]["high"] == 1
    assert s["defects"][0]["id"] == "memleak" and s["defects"][0]["file"] == "src/main.cpp"
