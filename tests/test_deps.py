"""Dependency audit: spec parsing, manifests, collision/cycle detection, and the LDF graph parser."""

import json

import pytest

from platformio_mcp import parsers
from platformio_mcp.pio import PioResult
from platformio_mcp.tools import deps

BUILD_LOG = """Processing view (platform: espressif32; board: esp32doit-devkit-v1; framework: arduino)
--------------------------------------------------------------------------------
LDF Modes: Finder ~ chain, Compatibility ~ soft
Found 34 compatible libraries
Scanning dependencies...
Dependency Graph
|-- ArduinoJson @ 7.0.4
|-- Adafruit GFX Library @ 1.11.9
|   |-- Adafruit BusIO @ 1.14.5
|   |   |-- Wire @ 2.0.0
|   |   |-- SPI @ 2.0.0
|   |-- Wire @ 2.0.0
|-- PubSubClient @ 2.8.0 (/Users/x/.pio/libdeps/view/PubSubClient)
|-- WiFi @ 2.0.0
Building in release mode
Compiling .pio/build/view/src/main.cpp.o
========================= [SUCCESS] Took 41.02 seconds =========================
"""


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("ArduinoJson", {"owner": None, "name": "ArduinoJson", "version": None, "kind": "registry", "pinned": False}),
        ("bblanchon/ArduinoJson@^7.0", {"owner": "bblanchon", "name": "ArduinoJson", "version": "^7.0", "kind": "registry", "pinned": True}),
        ("PubSubClient@2.8.0", {"owner": None, "name": "PubSubClient", "version": "2.8.0", "kind": "registry", "pinned": True}),
        ("adafruit/Adafruit GFX Library", {"owner": "adafruit", "name": "Adafruit GFX Library", "version": None, "kind": "registry", "pinned": False}),
        ("https://github.com/me/thing.git#v1.2", {"name": "thing", "version": "v1.2", "url": "https://github.com/me/thing.git", "kind": "url", "pinned": True}),
        ("https://github.com/me/thing.git", {"name": "thing", "version": None, "kind": "url", "pinned": False}),
        ("git@github.com:me/thing.git", {"name": "thing", "kind": "url"}),
        ("Thing=https://github.com/me/other/archive/main.zip", {"name": "Thing", "url": "https://github.com/me/other/archive/main.zip", "kind": "url"}),
        ("symlink:///home/me/libs/mylib", {"name": "mylib", "kind": "local", "pinned": True}),
        ("file://../shared/mylib", {"name": "mylib", "kind": "local", "pinned": True}),
        ("64", {"name": "64", "kind": "id", "pinned": False}),
        ("", {"kind": "empty"}),
    ],
)
def test_parse_lib_spec(spec, expected):
    got = parsers.parse_lib_spec(spec)
    for k, v in expected.items():
        assert got[k] == v, (spec, k, got)


def test_parse_library_properties():
    text = "name=Adafruit GFX Library\nversion=1.11.9\n# comment\ndepends=Adafruit BusIO (>=1.14), Wire\nsentence=x\n"
    got = parsers.parse_library_properties(text)
    assert got == {"name": "Adafruit GFX Library", "version": "1.11.9", "dependencies": ["Adafruit BusIO", "Wire"]}


def test_library_json_dependencies_all_shapes():
    assert parsers.library_json_dependencies({"dependencies": {"adafruit/Adafruit BusIO": "^1.14", "Wire": "*"}}) == ["Adafruit BusIO", "Wire"]
    assert parsers.library_json_dependencies({"dependencies": [{"owner": "a", "name": "B", "version": "1"}, "c/D@^2", "E"]}) == ["B", "D", "E"]
    assert parsers.library_json_dependencies({}) == []


def test_parse_dependency_graph_nested():
    graph = parsers.parse_dependency_graph(BUILD_LOG)
    assert [n["name"] for n in graph] == ["ArduinoJson", "Adafruit GFX Library", "PubSubClient", "WiFi"]
    gfx = graph[1]
    assert gfx["version"] == "1.11.9"
    assert [c["name"] for c in gfx["children"]] == ["Adafruit BusIO", "Wire"]
    assert [c["name"] for c in gfx["children"][0]["children"]] == ["Wire", "SPI"]
    assert graph[2]["path"] == "/Users/x/.pio/libdeps/view/PubSubClient"
    assert parsers.parse_dependency_graph("no graph here") == []


def test_has_recursion_error():
    assert parsers.has_recursion_error("  File x\nRecursionError: maximum recursion depth exceeded")
    assert not parsers.has_recursion_error(BUILD_LOG)


def _lib(root, source_dir, dir_name, *, json_manifest=None, properties=None):
    d = root / source_dir / dir_name
    d.mkdir(parents=True)
    if json_manifest is not None:
        (d / "library.json").write_text(json.dumps(json_manifest))
    if properties is not None:
        (d / "library.properties").write_text(properties)
    return d


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "platformio.ini").write_text("[env:view]\nplatform = espressif32\nboard = esp32doit-devkit-v1\n")
    state = {"config": {"env:view": {"board": "esp32doit-devkit-v1"}}}
    monkeypatch.setattr(deps, "project_config", lambda p: state["config"])
    return tmp_path, state


def test_read_library_manifest_prefers_json(tmp_path):
    d = _lib(tmp_path, "lib", "Foo", json_manifest={"name": "Foo", "version": 2, "dependencies": {"Bar": "*"}}, properties="name=Old\nversion=1\n")
    assert deps.read_library_manifest(d) == {"name": "Foo", "version": "2", "dependencies": ["Bar"], "manifest": "library.json"}
    p = _lib(tmp_path, "lib", "Props", properties="name=Props\nversion=0.1\ndepends=Foo\n")
    assert deps.read_library_manifest(p)["manifest"] == "library.properties"
    assert deps.read_library_manifest(tmp_path / "lib") is None


def test_clean_project_has_no_issues(project):
    path, state = project
    state["config"]["env:view"]["lib_deps"] = "bblanchon/ArduinoJson@^7.0"
    _lib(path, ".pio/libdeps/view", "ArduinoJson", json_manifest={"name": "ArduinoJson", "version": "7.0.4"})
    r = deps.pio_deps_check(project_dir=str(path))
    assert r["ok"] and r["issues"] == [] and r["issue_count"] == 0
    assert r["declared"][0]["owner"] == "bblanchon"
    assert r["installed"][0]["source"] == "libdeps" and r["installed"][0]["version"] == "7.0.4"
    assert "No collisions" in r["summary"]


def test_collision_unpinned_missing_and_leftover(project):
    path, state = project
    state["config"]["env:view"]["lib_deps"] = ["ArduinoJson", "adafruit/Adafruit GFX Library@^1.11", "Missing@1.0"]
    _lib(path, "lib", "ArduinoJson", json_manifest={"name": "ArduinoJson", "version": "6.21.0"})
    _lib(path, ".pio/libdeps/view", "ArduinoJson", json_manifest={"name": "ArduinoJson", "version": "7.0.4"})
    _lib(path, ".pio/libdeps/view", "Adafruit GFX Library", properties="name=Adafruit GFX Library\nversion=1.11.9\ndepends=Adafruit BusIO\n")
    _lib(path, ".pio/libdeps/view", "Adafruit BusIO", properties="name=Adafruit BusIO\nversion=1.14.5\n")
    _lib(path, ".pio/libdeps/view", "Leftover", json_manifest={"name": "Leftover", "version": "0.1"})
    _lib(path, ".pio/libdeps/view", "Unity", json_manifest={"name": "Unity", "version": "2.6.0"})
    r = deps.pio_deps_check(project_dir=str(path), env="view")
    kinds = {(i["kind"], i.get("library")) for i in r["issues"]}
    assert ("name_collision", "ArduinoJson") in kinds
    assert ("unpinned", "ArduinoJson") in kinds
    assert ("not_installed", "Missing") in kinds
    assert ("undeclared", "Leftover") in kinds
    assert ("undeclared", "Adafruit BusIO") not in kinds  # a dependency of GFX, not a leftover
    assert ("undeclared", "Unity") not in kinds  # installed by `pio test`, never declared
    collision = next(i for i in r["issues"] if i["kind"] == "name_collision")
    assert collision["severity"] == "error" and "lib/ArduinoJson wins" in collision["message"].replace("\\", "/") and "#3598" in collision["message"]
    assert r["issues"][0]["severity"] == "error" and r["ok"] is False
    assert r["counts"] == {"error": 1, "warning": 2, "info": 1}
    assert "first: [name_collision]" in r["summary"]


def test_collision_between_two_registry_copies_is_warning(project):
    path, state = project
    state["config"]["env:view"]["lib_deps"] = "a/Foo@1.0"
    _lib(path, ".pio/libdeps/view", "Foo", json_manifest={"name": "Foo", "version": "1.0"})
    _lib(path, ".pio/libdeps/view", "foo-fork", json_manifest={"name": "foo", "version": "2.0"})
    r = deps.pio_deps_check(project_dir=str(path))
    c = [i for i in r["issues"] if i["kind"] == "name_collision"]
    assert len(c) == 1 and c[0]["severity"] == "warning" and sorted(p.replace("\\", "/") for p in c[0]["paths"]) == [".pio/libdeps/view/Foo", ".pio/libdeps/view/foo-fork"]
    assert r["ok"] is True


def test_circular_dependency_detected(project):
    path, state = project
    state["config"]["env:view"]["lib_deps"] = "a/A@1\nb/B@1"
    _lib(path, ".pio/libdeps/view", "A", json_manifest={"name": "A", "version": "1", "dependencies": {"b/B": "*"}})
    _lib(path, ".pio/libdeps/view", "B", json_manifest={"name": "B", "version": "1", "dependencies": [{"name": "C"}]})
    _lib(path, ".pio/libdeps/view", "C", properties="name=C\nversion=1\ndepends=A\n")
    r = deps.pio_deps_check(project_dir=str(path))
    cyc = [i for i in r["issues"] if i["kind"] == "circular"]
    assert len(cyc) == 1 and cyc[0]["libraries"] == ["A", "B", "C"] and cyc[0]["severity"] == "error"
    assert "#5261" in cyc[0]["message"]


def test_lib_extra_dirs_and_ldf_off_note(project):
    path, state = project
    state["config"]["env:view"].update({"lib_deps": "Gone@1", "lib_extra_dirs": "../shared", "lib_ldf_mode": "off"})
    _lib(path.parent, "shared", "Extra", json_manifest={"name": "Extra", "version": "1"})
    r = deps.pio_deps_check(project_dir=str(path))
    assert [lib["source"] for lib in r["installed"]] == ["extra"]
    assert {i["kind"] for i in r["issues"]} == {"not_installed", "ldf_mode"}


def test_build_true_parses_graph_and_recursion(project, monkeypatch):
    path, state = project
    state["config"]["env:view"]["lib_deps"] = "bblanchon/ArduinoJson@^7"
    _lib(path, ".pio/libdeps/view", "ArduinoJson", json_manifest={"name": "ArduinoJson", "version": "7.0.4"})
    calls = []

    def fake_run(args, cwd=None, timeout=None, tool="pio", **kw):
        calls.append(args)
        return PioResult(args=args, returncode=0, output=BUILD_LOG, duration_s=1.5, log_path="/tmp/x.log")

    monkeypatch.setattr(deps, "run_pio", fake_run)
    r = deps.pio_deps_check(project_dir=str(path), build=True)
    assert calls == [["run", "-d", str(path), "-e", "view"]]
    assert r["ok"] and r["build"]["ok"] and r["build"]["log_path"] == "/tmp/x.log"
    assert [n["name"] for n in r["graph"]] == ["ArduinoJson", "Adafruit GFX Library", "PubSubClient", "WiFi"]
    assert "dependency graph has 4 root libraries" in r["summary"]

    monkeypatch.setattr(deps, "run_pio", lambda *a, **kw: PioResult(args=[], returncode=1, output="Traceback\nRecursionError: maximum recursion depth exceeded\n", duration_s=0.3))
    r = deps.pio_deps_check(project_dir=str(path), build=True)
    assert r["ok"] is False and r["issues"][0]["kind"] == "circular" and "RecursionError" in r["issues"][0]["message"]
    assert r["graph"] == [] and r["build"]["ok"] is False


def test_no_env_errors_structurally(project):
    path, state = project
    state["config"] = {"platformio": {}}
    r = deps.pio_deps_check(project_dir=str(path))
    assert r["ok"] is False and r["error"] == "ValueError"
