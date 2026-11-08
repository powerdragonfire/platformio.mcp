"""Declarative repeatable hardware test plans with firmware-linked evidence."""

from __future__ import annotations

import configparser
import hashlib
import json
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import check_policy, default_envs, env_setting, guard, monitors, project_config, run_json
from ..pio import resolve_project_dir
from .analysis import DEFAULT_FAIL_PATTERN
from .build import pio_upload
from .devices import _devices, _pick_port

SUPPORTED_PLAN_VERSION = 1
MAX_STORED_RUNS = 200


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_plan(path: str) -> tuple[Path, dict[str, Any]]:
    plan_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in plan file {plan_path}: {exc.msg} at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(data, dict):
        raise ValueError("plan must be a JSON object")
    return plan_path, data


def _resolve_environment(project_dir: str, environment: str | None) -> str:
    cfg = _config_for_project(project_dir)
    envs = default_envs(cfg)
    if environment:
        if environment not in envs and f"env:{environment}" not in cfg:
            raise ValueError(f"environment '{environment}' not found in platformio.ini")
        return environment
    if not envs:
        raise ValueError("platformio.ini declares no environments.")
    if len(envs) > 1:
        raise ValueError("plan must set environment (or pass env) when multiple PlatformIO environments are available.")
    return envs[0]


def _resolve_ports(project_dir: str, environment: str, serial: dict[str, Any]) -> tuple[str, str, int | None]:
    cfg = _config_for_project(project_dir)
    configured_monitor_port = env_setting(cfg, environment, "monitor_port")
    configured_upload_port = env_setting(cfg, environment, "upload_port")
    monitor_hint = serial.get("monitor_port") or configured_monitor_port or serial.get("upload_port") or configured_upload_port
    monitor_port, cfg_baud = _pick_port(monitor_hint, project_dir, environment)
    upload_port = serial.get("upload_port") or configured_upload_port or monitor_port
    return monitor_port, upload_port, cfg_baud


def _config_for_project(project_dir: str) -> dict[str, dict[str, Any]]:
    try:
        return project_config(project_dir)
    except Exception:
        path = Path(project_dir) / "platformio.ini"
        if not path.exists():
            return {}
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8")
        out: dict[str, dict[str, Any]] = {}
        for section in parser.sections():
            out[section] = {k: v for k, v in parser.items(section)}
        return out


def _normalize_plan(
    data: dict[str, Any],
    *,
    plan_dir: Path,
    project_dir: str | None,
    env: str | None,
    iterations: int | None,
    report_dir: str | None,
) -> dict[str, Any]:
    plan_version = int(data.get("plan_version", SUPPORTED_PLAN_VERSION))
    if plan_version != SUPPORTED_PLAN_VERSION:
        raise ValueError(f"unsupported plan_version={plan_version}; supported version is {SUPPORTED_PLAN_VERSION}")

    run = data.get("run") or {}
    serial = data.get("serial_assertion") or {}
    board = data.get("board") or {}
    fixture = data.get("fixture") or {}

    project = project_dir or data.get("project_dir")
    if not project:
        raise ValueError("plan must set project_dir or pass project_dir")
    project_path = Path(project).expanduser()
    if not project_path.is_absolute():
        project_path = (plan_dir / project_path).resolve()
    project = str(project_path)
    project = str(resolve_project_dir(project))

    environment = _resolve_environment(project, env or data.get("environment") or board.get("platformio_env"))
    success = serial.get("success_pattern")
    if not success:
        raise ValueError("plan.serial_assertion.success_pattern is required")

    run_iterations = iterations if iterations is not None else int(run.get("iterations", 1))
    if run_iterations < 1:
        raise ValueError("iterations must be >= 1")

    timeout_s = float(serial.get("timeout_s", 30))
    observation_window_s = float(serial.get("observation_window_s", 2))
    reconnect_attempts = int(serial.get("reconnect_attempts", 0))
    reconnect_delay_s = float(serial.get("reconnect_delay_s", 0.2))
    max_lines = int(serial.get("max_lines", 800))
    baud = serial.get("baud")
    if baud is not None:
        baud = int(baud)
    if timeout_s <= 0 or observation_window_s < 0 or reconnect_attempts < 0:
        raise ValueError("timeout_s must be > 0; observation_window_s and reconnect_attempts must be >= 0")
    if reconnect_delay_s < 0:
        raise ValueError("reconnect_delay_s must be >= 0")
    if max_lines < 1:
        raise ValueError("max_lines must be >= 1")
    if baud is not None and baud < 1:
        raise ValueError("baud must be >= 1")

    re.compile(success)
    if serial.get("failure_pattern"):
        re.compile(serial["failure_pattern"])

    out_dir_raw = report_dir or run.get("report_dir") or str(Path(project) / ".pio-hil-reports")
    out_dir = Path(out_dir_raw).expanduser()
    if not out_dir.is_absolute():
        out_dir = (plan_dir / out_dir).resolve()
    else:
        out_dir = out_dir.resolve()

    return {
        "plan_version": plan_version,
        "name": data.get("name") or "hardware-test-plan",
        "project_dir": project,
        "environment": environment,
        "board": board,
        "fixture": fixture,
        "run": {
            "iterations": run_iterations,
            "upload_each_iteration": bool(run.get("upload_each_iteration", True)),
            "stop_open_sessions": bool(run.get("stop_open_sessions", True)),
        },
        "serial_assertion": {
            "success_pattern": success,
            "failure_pattern": serial.get("failure_pattern", DEFAULT_FAIL_PATTERN),
            "timeout_s": timeout_s,
            "observation_window_s": observation_window_s,
            "max_lines": max_lines,
            "baud": baud,
            "monitor_port": serial.get("monitor_port"),
            "upload_port": serial.get("upload_port"),
            "reconnect_attempts": reconnect_attempts,
            "reconnect_delay_s": reconnect_delay_s,
        },
        "report_dir": str(out_dir),
        "device_identity": data.get("device_identity") or board.get("device_identity"),
        "peripherals": data.get("peripherals") or board.get("peripherals") or [],
        "pin_mapping": data.get("pin_mapping") or board.get("pin_mapping") or {},
    }


def _git_identity(project_dir: str) -> dict[str, Any]:
    def _run(*args: str) -> str | None:
        try:
            res = subprocess.run(["git", "-C", project_dir, *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return res.stdout.strip() if res.returncode == 0 else None

    commit = _run("rev-parse", "HEAD")
    branch = _run("rev-parse", "--abbrev-ref", "HEAD")
    dirty_out = _run("status", "--porcelain")
    dirty = bool(dirty_out)
    return {"commit": commit, "branch": branch, "dirty": dirty}


def _firmware_identity(project_dir: str, env: str | None) -> dict[str, Any]:
    args = ["project", "metadata", "--json-output"]
    if env:
        args += ["-e", env]
    _, metadata = run_json(args, cwd=project_dir, tool="project-metadata", timeout=120)
    if not metadata:
        return {"environment": env, "program_path": None, "sha256": None, "size_bytes": None}
    chosen_env = env if env in metadata else next(iter(metadata.keys()))
    prog = metadata.get(chosen_env, {}).get("prog_path")
    program_path = Path(prog)
    if not program_path.is_absolute():
        program_path = (Path(project_dir) / program_path).resolve()
    if not program_path.exists():
        return {"environment": chosen_env, "program_path": str(program_path), "sha256": None, "size_bytes": None}
    digest = hashlib.sha256(program_path.read_bytes()).hexdigest()
    return {
        "environment": chosen_env,
        "program_path": str(program_path),
        "sha256": digest,
        "size_bytes": program_path.stat().st_size,
    }


def _device_identity(port: str) -> dict[str, Any]:
    try:
        for d in _devices():
            if d.get("port") == port:
                return {"port": port, "description": d.get("description"), "hwid": d.get("hwid")}
    except Exception:
        pass
    return {"port": port, "description": None, "hwid": None}


@dataclass
class TestPlanRun:
    run_id: str
    plan_path: str
    report_path: str
    normalized_plan: dict[str, Any]
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    status: str = "running"
    cancel_requested: bool = False
    cancel_reason: str | None = None
    summary: str = ""
    iterations: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {"pass": 0, "fail": 0, "timeout": 0, "infra_error": 0})
    source: dict[str, Any] = field(default_factory=dict)
    firmware: dict[str, Any] = field(default_factory=dict)
    device: dict[str, Any] = field(default_factory=dict)
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class TestPlanRunManager:
    def __init__(self, max_runs: int = MAX_STORED_RUNS) -> None:
        self._runs: dict[str, TestPlanRun] = {}
        self._lock = threading.Lock()
        self._max_runs = max(max_runs, 1)

    def start(self, plan_path: str, normalized_plan: dict[str, Any]) -> TestPlanRun:
        with self._lock:
            for active in self._runs.values():
                if active.status != "running":
                    continue
                if active.normalized_plan.get("project_dir") == normalized_plan.get("project_dir"):
                    raise RuntimeError(
                        f"project {normalized_plan['project_dir']} already has running test-plan run {active.run_id}; "
                        "wait for it to finish or cancel it before starting another run."
                    )
        run_id = uuid.uuid4().hex[:8]
        report_dir = Path(normalized_plan["report_dir"])
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{run_id}.json"
        run = TestPlanRun(run_id=run_id, plan_path=plan_path, report_path=str(report_path), normalized_plan=normalized_plan)
        with self._lock:
            self._runs[run_id] = run
            self._prune_completed_locked()
        run.thread = threading.Thread(target=self._execute, args=(run,), name=f"pio-hil-{run_id}", daemon=True)
        run.thread.start()
        return run

    def _prune_completed_locked(self) -> None:
        overflow = len(self._runs) - self._max_runs
        if overflow <= 0:
            return
        removable = sorted(
            [r for r in self._runs.values() if r.status in {"completed", "failed", "cancelled"}],
            key=lambda r: (r.finished_at or "", r.created_at, r.run_id),
        )
        for run in removable[:overflow]:
            self._runs.pop(run.run_id, None)

    def get(self, run_id: str) -> TestPlanRun:
        with self._lock:
            run = self._runs.get(run_id)
        if not run:
            raise KeyError(f"unknown run_id '{run_id}'")
        return run

    def cancel(self, run_id: str, reason: str | None = None) -> TestPlanRun:
        run = self.get(run_id)
        with run.lock:
            run.cancel_requested = True
            run.cancel_reason = reason or "cancelled by user"
        return run

    def stop_all(self, wait_s: float = 10.0, stop_monitors: bool = True) -> None:
        with self._lock:
            run_ids = list(self._runs.keys())
        for run_id in run_ids:
            try:
                self.cancel(run_id, "server shutdown")
            except Exception:
                pass
        if stop_monitors:
            try:
                monitors.stop_all()
            except Exception:
                pass
        deadline = time.monotonic() + max(0.0, wait_s)
        for run_id in run_ids:
            try:
                run = self.get(run_id)
            except KeyError:
                continue
            thread = run.thread
            if thread is None or not thread.is_alive():
                continue
            thread.join(max(0.0, deadline - time.monotonic()))

    def _write_report(self, run: TestPlanRun) -> None:
        with run.lock:
            payload = {
                "run_id": run.run_id,
                "status": run.status,
                "summary": run.summary,
                "plan_path": run.plan_path,
                "plan": run.normalized_plan,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "cancel_requested": run.cancel_requested,
                "cancel_reason": run.cancel_reason,
                "counts": run.counts,
                "source": run.source,
                "firmware": run.firmware,
                "device": run.device,
                "iterations": run.iterations,
            }
        report = Path(run.report_path)
        tmp = report.with_suffix(report.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(report)

    def _make_summary(self, run: TestPlanRun) -> str:
        done = len(run.iterations)
        total = run.normalized_plan["run"]["iterations"]
        c = run.counts
        state = run.status.upper()
        return f"{state}: {done}/{total} iteration(s), pass={c['pass']} fail={c['fail']} timeout={c['timeout']} infra_error={c['infra_error']}. Report: {run.report_path}."

    def _execute(self, run: TestPlanRun) -> None:
        plan = run.normalized_plan
        run.started_at = _now()
        run.source = _git_identity(plan["project_dir"])
        run.firmware = {"environment": plan["environment"], "program_path": None, "sha256": None, "size_bytes": None}
        run.summary = self._make_summary(run)
        self._write_report(run)
        try:
            for idx in range(1, plan["run"]["iterations"] + 1):
                with run.lock:
                    if run.cancel_requested:
                        run.status = "cancelled"
                        break
                result = self._run_iteration(run, idx)
                with run.lock:
                    run.iterations.append(result)
                    run.counts[result["outcome"]] += 1
                self._write_report(run)
            with run.lock:
                if run.status == "running":
                    run.status = "cancelled" if run.cancel_requested else "completed"
        except Exception as exc:
            with run.lock:
                run.status = "failed"
                run.iterations.append(
                    {
                        "index": len(run.iterations) + 1,
                        "outcome": "infra_error",
                        "summary": f"internal runner failure: {type(exc).__name__}: {exc}",
                        "error_code": "runner_exception",
                        "started_at": _now(),
                        "finished_at": _now(),
                    }
                )
                run.counts["infra_error"] += 1
        finally:
            run.finished_at = _now()
            run.summary = self._make_summary(run)
            self._write_report(run)

    def _run_iteration(self, run: TestPlanRun, index: int) -> dict[str, Any]:
        plan = run.normalized_plan
        serial = plan["serial_assertion"]
        max_lines = int(serial["max_lines"])
        timeout_s = float(serial["timeout_s"])
        observation_window_s = float(serial["observation_window_s"])
        reconnect_left = int(serial["reconnect_attempts"])
        reconnect_delay_s = float(serial["reconnect_delay_s"])
        started = time.monotonic()
        started_at = _now()
        evidence_dir = Path(run.report_path).with_suffix("").with_name(Path(run.report_path).stem + "-evidence")
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_dir / f"iter-{index:04d}.log"
        evidence_fh = evidence_path.open("w", encoding="utf-8")
        line_count = 0
        lines_tail: list[str] = []

        def append_lines(new_lines: list[str]) -> None:
            nonlocal line_count, lines_tail
            if not new_lines:
                return
            evidence_fh.write("\n".join(new_lines) + "\n")
            line_count += len(new_lines)
            lines_tail.extend(new_lines)
            if len(lines_tail) > max_lines:
                lines_tail = lines_tail[-max_lines:]

        def finalize(outcome: str, summary: str, **extra: Any) -> dict[str, Any]:
            evidence_fh.flush()
            rec: dict[str, Any] = {
                "index": index,
                "outcome": outcome,
                "summary": summary,
                "started_at": started_at,
                "finished_at": _now(),
                "duration_s": round(time.monotonic() - started, 2),
                "line_count": line_count,
                "lines_tail": lines_tail[-40:],
                **extra,
            }
            if outcome != "pass":
                rec["evidence_path"] = str(evidence_path)
            else:
                try:
                    evidence_path.unlink()
                except OSError:
                    pass
            return rec

        try:
            port, upload_port, cfg_baud = _resolve_ports(plan["project_dir"], plan["environment"], serial)
        except Exception as exc:
            code = "device_ambiguous" if "several candidate ports" in str(exc) else "device_missing"
            return finalize("infra_error", f"Iteration {index}: cannot select device port: {exc}", error_code=code)

        baud = int(serial.get("baud") or cfg_baud or 115200)
        run.device = {**_device_identity(port), "identity": plan.get("device_identity")}

        upload_result = None
        if plan["run"]["upload_each_iteration"] or index == 1:
            with run.lock:
                if run.cancel_requested:
                    return finalize("infra_error", f"Iteration {index}: cancelled before upload.", error_code="cancelled")
            upload_result = pio_upload(
                project_dir=plan["project_dir"],
                env=plan["environment"],
                upload_port=upload_port,
                stop_open_sessions=plan["run"].get("stop_open_sessions", True),
            )
            if not upload_result.get("ok"):
                return finalize(
                    "infra_error",
                    f"Iteration {index}: upload failed ({upload_result.get('error') or 'upload_failed'}): {upload_result.get('summary', '')}",
                    error_code="upload_failed",
                    upload={k: upload_result.get(k) for k in ("ok", "status", "summary", "log_path", "port_error", "port_diagnosis")},
                )
            try:
                run.firmware = _firmware_identity(plan["project_dir"], plan["environment"])
            except Exception:
                run.firmware = {"environment": plan["environment"], "program_path": None, "sha256": None, "size_bytes": None}

        success_re = re.compile(serial["success_pattern"])
        failure_re = re.compile(serial["failure_pattern"]) if serial.get("failure_pattern") else None

        t0 = time.monotonic()
        deadline = t0 + timeout_s
        matched_success = None
        matched_failure = None
        success_seen_at = None
        reconnects = 0
        cursor = 0
        session = None

        def open_session():
            return monitors.start(port, baud)

        try:
            try:
                session = open_session()
            except Exception as exc:
                return finalize(
                    "infra_error",
                    f"Iteration {index}: cannot open serial monitor on {port}: {exc}",
                    error_code="serial_open_failed",
                    upload={"log_path": upload_result.get("log_path") if upload_result else None},
                )
            while True:
                with run.lock:
                    if run.cancel_requested:
                        return finalize("infra_error", f"Iteration {index}: cancelled.", error_code="cancelled", upload={"log_path": upload_result.get("log_path") if upload_result else None})
                now = time.monotonic()
                if now >= deadline:
                    if matched_success:
                        return finalize(
                            "timeout",
                            f"Iteration {index}: success marker seen but observation window ({observation_window_s}s) did not complete before timeout.",
                            matched_success_line=matched_success,
                            matched_failure_line=matched_failure,
                            reconnects=reconnects,
                            upload={"log_path": upload_result.get("log_path") if upload_result else None},
                        )
                    return finalize(
                        "timeout",
                        f"Iteration {index}: timeout waiting for success pattern '{serial['success_pattern']}'.",
                        reconnects=reconnects,
                        upload={"log_path": upload_result.get("log_path") if upload_result else None},
                    )
                if success_seen_at is not None and now - success_seen_at >= observation_window_s:
                    return finalize(
                        "pass",
                        f"Iteration {index}: pass. Saw success marker '{matched_success}' and no failure pattern during {observation_window_s}s observation window.",
                        matched_success_line=matched_success,
                        reconnects=reconnects,
                        upload={"log_path": upload_result.get("log_path") if upload_result else None},
                    )

                read_deadline = deadline if success_seen_at is None else min(deadline, success_seen_at + observation_window_s)
                wait_s = min(0.5, max(0.0, read_deadline - now))
                if wait_s <= 0:
                    continue
                r = session.read(cursor=cursor, max_lines=max_lines, timeout_s=wait_s)
                cursor = r["cursor"]
                new_lines = r["lines"]
                append_lines(new_lines)

                for line in new_lines:
                    if failure_re and failure_re.search(line):
                        matched_failure = line
                        return finalize(
                            "fail",
                            f"Iteration {index}: failure pattern matched: '{line}'.",
                            matched_success_line=matched_success,
                            matched_failure_line=matched_failure,
                            reconnects=reconnects,
                            upload={"log_path": upload_result.get("log_path") if upload_result else None},
                        )
                    if matched_success is None and success_re.search(line):
                        matched_success = line
                        success_seen_at = time.monotonic()

                if r["closed"]:
                    if reconnect_left > 0 and time.monotonic() < deadline:
                        reconnect_left -= 1
                        reconnects += 1
                        if session is not None:
                            try:
                                monitors.stop(session.id)
                            except Exception:
                                pass
                            session = None
                        time.sleep(max(reconnect_delay_s, 0))
                        try:
                            session = open_session()
                        except Exception as exc:
                            return finalize(
                                "infra_error",
                                f"Iteration {index}: cannot reopen serial monitor on {port}: {exc}",
                                error_code="serial_reconnect_failed",
                                matched_success_line=matched_success,
                                reconnects=reconnects,
                                upload={"log_path": upload_result.get("log_path") if upload_result else None},
                            )
                        matched_success = None
                        success_seen_at = None
                        cursor = 0
                        continue
                    return finalize(
                        "infra_error",
                        f"Iteration {index}: serial connection closed ({r['error'] or 'device disconnected'}).",
                        error_code="serial_disconnected",
                        matched_success_line=matched_success,
                        reconnects=reconnects,
                        upload={"log_path": upload_result.get("log_path") if upload_result else None},
                    )
        finally:
            if session is not None:
                try:
                    monitors.stop(session.id)
                except Exception:
                    pass
            evidence_fh.close()


test_plan_runs = TestPlanRunManager()


@guard
def pio_test_plan_validate(plan_path: str, project_dir: str | None = None, env: str | None = None) -> dict[str, Any]:
    plan_file, data = _read_plan(plan_path)
    normalized = _normalize_plan(data, plan_dir=plan_file.parent, project_dir=project_dir, env=env, iterations=None, report_dir=None)
    serial = normalized["serial_assertion"]
    return {
        "ok": True,
        "summary": (
            f"Plan '{normalized['name']}' is valid for env {normalized['environment'] or 'default'} with "
            f"{normalized['run']['iterations']} iteration(s), timeout {serial['timeout_s']}s, "
            f"observation window {serial['observation_window_s']}s, and report dir {normalized['report_dir']}."
        ),
        "plan_path": str(plan_file),
        "plan": normalized,
    }


@guard
def pio_test_plan_start(
    plan_path: str,
    iterations: int | None = None,
    project_dir: str | None = None,
    env: str | None = None,
    report_dir: str | None = None,
) -> dict[str, Any]:
    check_policy("flash")
    plan_file, data = _read_plan(plan_path)
    normalized = _normalize_plan(data, plan_dir=plan_file.parent, project_dir=project_dir, env=env, iterations=iterations, report_dir=report_dir)
    run = test_plan_runs.start(str(plan_file), normalized)
    return {
        "ok": True,
        "summary": (
            f"Started hardware test plan run {run.run_id} for '{normalized['name']}' "
            f"({normalized['run']['iterations']} iteration(s)). Poll with pio_test_plan_status and cancel with pio_test_plan_cancel."
        ),
        "run_id": run.run_id,
        "status": run.status,
        "report_path": run.report_path,
    }


@guard
def pio_test_plan_status(run_id: str, include_iterations: bool = False, tail_iterations: int = 20) -> dict[str, Any]:
    run = test_plan_runs.get(run_id)
    with run.lock:
        iterations = run.iterations[-max(tail_iterations, 0) :] if not include_iterations else list(run.iterations)
        payload = {
            "ok": True,
            "summary": run.summary,
            "run_id": run.run_id,
            "status": run.status,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "cancel_requested": run.cancel_requested,
            "cancel_reason": run.cancel_reason,
            "counts": dict(run.counts),
            "report_path": run.report_path,
            "plan_name": run.normalized_plan["name"],
            "iterations_total": run.normalized_plan["run"]["iterations"],
            "iterations_done": len(run.iterations),
            "source": run.source,
            "firmware": run.firmware,
            "device": run.device,
            "iterations": iterations,
        }
    return payload


@guard
def pio_test_plan_cancel(run_id: str, reason: str | None = None) -> dict[str, Any]:
    run = test_plan_runs.cancel(run_id, reason)
    with run.lock:
        summary = f"Cancellation requested for run {run_id}. Current status: {run.status}."
        if run.status in {"completed", "failed", "cancelled"}:
            summary = f"Run {run_id} is already {run.status}; no cancellation needed."
    return {
        "ok": True,
        "summary": summary,
        "run_id": run.run_id,
        "status": run.status,
        "cancel_requested": run.cancel_requested,
        "cancel_reason": run.cancel_reason,
        "report_path": run.report_path,
    }


def register(mcp) -> None:
    mcp.tool(name="pio_test_plan_validate", description=(
        "Validate a declarative hardware test plan JSON file before execution. Checks required fields for a serial assertion workflow "
        "(project, success/failure patterns, timeout, observation window, iteration count) and returns the normalized plan."
    ))(pio_test_plan_validate)
    mcp.tool(name="pio_test_plan_start", description=(
        "Start a repeatable hardware test-plan run in the background. Each iteration can upload firmware, monitor serial output for pass/fail "
        "assertions, keep observing after success for the configured window, classify outcomes, and write a machine-readable report with evidence paths."
    ))(pio_test_plan_start)
    mcp.tool(name="pio_test_plan_status", description=(
        "Read test-plan run progress and results: run status, pass/fail/timeout/infrastructure-error counts, firmware/source/device identity, "
        "and recent or full per-iteration outcomes with evidence references."
    ))(pio_test_plan_status)
    mcp.tool(name="pio_test_plan_cancel", description=(
        "Request explicit cancellation of an in-progress test-plan run by run_id. The active iteration stops at its next safe check and the run "
        "is finalized as cancelled with completed iteration evidence retained."
    ))(pio_test_plan_cancel)
