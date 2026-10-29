import json
import time
from pathlib import Path

import pytest
from test_monitor import FakePort

from platformio_mcp import core
from platformio_mcp.tools import hardware


@pytest.fixture(autouse=True)
def isolated_runs(monkeypatch):
    mgr = hardware.TestPlanRunManager()
    monkeypatch.setattr(hardware, "test_plan_runs", mgr)
    yield
    mgr.stop_all()


@pytest.fixture
def project(tmp_path):
    (tmp_path / "platformio.ini").write_text("[env:test]\nplatform = native\nboard = native\n")
    return tmp_path


def _write_plan(path: Path, **overrides):
    plan = {
        "plan_version": 1,
        "name": "serial-check",
        "project_dir": str(path),
        "environment": "test",
        "run": {"iterations": 1, "upload_each_iteration": True},
        "serial_assertion": {
            "success_pattern": "BOOT_OK",
            "failure_pattern": "panic|test-failure",
            "timeout_s": 0.4,
            "observation_window_s": 0.15,
            "max_lines": 200,
            "reconnect_attempts": 0,
        },
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(plan.get(k), dict):
            plan[k].update(v)
        else:
            plan[k] = v
    p = path / "plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")
    return p


def _wait_done(run_id: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while True:
        st = hardware.pio_test_plan_status(run_id)
        if st["status"] in {"completed", "cancelled", "failed"}:
            return st
        if time.monotonic() > deadline:
            pytest.fail(f"run {run_id} did not finish")
        time.sleep(0.05)


def _common_mocks(monkeypatch):
    monkeypatch.setattr(hardware, "_git_identity", lambda project_dir: {"commit": "abc", "branch": "main", "dirty": True})
    monkeypatch.setattr(hardware, "_firmware_identity", lambda project_dir, env: {"environment": env, "program_path": "/tmp/fw.bin", "sha256": "f" * 64, "size_bytes": 1234})
    monkeypatch.setattr(
        hardware,
        "pio_upload",
        lambda project_dir, env, upload_port, stop_open_sessions: {
            "ok": True,
            "summary": "upload ok",
            "status": "success",
            "log_path": "/tmp/upload.log",
            "port_error": None,
            "port_diagnosis": None,
        },
    )
    monkeypatch.setattr(hardware, "_pick_port", lambda port, project_dir, env: ("/dev/fake", 115200))
    monkeypatch.setattr(hardware, "_device_identity", lambda port: {"port": port, "description": "fake", "hwid": "USB VID:PID=1:2"})


def test_plan_timeout(monkeypatch, project):
    _common_mocks(monkeypatch)
    mgr = core.MonitorManager(opener=lambda port, baud: FakePort([b"boot\n"]))
    monkeypatch.setattr(hardware, "monitors", mgr)
    plan = _write_plan(project)

    started = hardware.pio_test_plan_start(str(plan))
    done = _wait_done(started["run_id"])

    assert done["status"] == "completed"
    assert done["counts"]["timeout"] == 1
    assert done["iterations"][-1]["outcome"] == "timeout"


def test_success_marker_does_not_end_window(monkeypatch, project):
    _common_mocks(monkeypatch)
    mgr = core.MonitorManager(opener=lambda port, baud: FakePort([b"BOOT_OK\n", b"panic now\n"], delay=0.03))
    monkeypatch.setattr(hardware, "monitors", mgr)
    plan = _write_plan(project, serial_assertion={"timeout_s": 1.0, "observation_window_s": 0.2})

    started = hardware.pio_test_plan_start(str(plan))
    done = _wait_done(started["run_id"])

    assert done["counts"]["fail"] == 1
    rec = done["iterations"][-1]
    assert rec["outcome"] == "fail"
    assert "panic now" in rec["matched_failure_line"]


def test_reconnect_and_pass(monkeypatch, project):
    _common_mocks(monkeypatch)

    class BoomAfterRead(FakePort):
        def __init__(self):
            super().__init__([b"boot\n"], delay=0.01)
            self.boom = False

        def read(self, size=1):
            if self.chunks:
                return super().read(size)
            if not self.boom:
                self.boom = True
                raise OSError("disconnect")
            return b""

    calls = {"n": 0}

    def opener(port, baud):
        calls["n"] += 1
        if calls["n"] == 1:
            return BoomAfterRead()
        return FakePort([b"BOOT_OK\n"], delay=0.01)

    mgr = core.MonitorManager(opener=opener)
    monkeypatch.setattr(hardware, "monitors", mgr)
    plan = _write_plan(project, serial_assertion={"reconnect_attempts": 1, "timeout_s": 1.0, "observation_window_s": 0.1})

    started = hardware.pio_test_plan_start(str(plan))
    done = _wait_done(started["run_id"])

    rec = done["iterations"][-1]
    assert rec["outcome"] == "pass"
    assert rec["reconnects"] == 1


def test_cancel_run(monkeypatch, project):
    _common_mocks(monkeypatch)
    mgr = core.MonitorManager(opener=lambda port, baud: FakePort([], delay=0.02))
    monkeypatch.setattr(hardware, "monitors", mgr)
    plan = _write_plan(
        project,
        run={"iterations": 5, "upload_each_iteration": False},
        serial_assertion={"timeout_s": 0.8, "observation_window_s": 0.2},
    )

    started = hardware.pio_test_plan_start(str(plan))
    run_id = started["run_id"]
    time.sleep(0.15)
    hardware.pio_test_plan_cancel(run_id, "stop now")
    done = _wait_done(run_id)

    assert done["status"] == "cancelled"
    assert done["cancel_requested"] is True
    assert done["iterations_done"] < done["iterations_total"]


def test_ambiguous_device_selection_is_infra_error(monkeypatch, project):
    _common_mocks(monkeypatch)
    monkeypatch.setattr(hardware, "_pick_port", lambda port, project_dir, env: (_ for _ in ()).throw(RuntimeError("several candidate ports: /dev/ttyUSB0, /dev/ttyUSB1")))
    plan = _write_plan(project)

    started = hardware.pio_test_plan_start(str(plan))
    done = _wait_done(started["run_id"])

    rec = done["iterations"][-1]
    assert rec["outcome"] == "infra_error"
    assert rec["error_code"] == "device_ambiguous"
