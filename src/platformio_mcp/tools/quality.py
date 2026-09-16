"""Unit tests and static analysis."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from ..core import (
    PolicyError,
    check_policy,
    default_envs,
    env_setting,
    guard,
    policy,
    project_config,
)
from ..parsers import parse_diagnostics, summarize_check_report, summarize_test_report
from ..pio import DEFAULT_TIMEOUTS, resolve_project_dir, run_pio, tail

SEVERITY_ORDER = ["low", "medium", "high"]


@guard
def pio_test(
    project_dir: str | None = None,
    env: str | None = None,
    filter: str | None = None,
    ignore: str | None = None,
    without_uploading: bool = False,
    without_building: bool = False,
    upload_port: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    check_policy("build")
    path = resolve_project_dir(project_dir)
    cfg = project_config(str(path))
    envs = [env] if env else default_envs(cfg)
    embedded = [e for e in envs if env_setting(cfg, e, "platform", "") != "native"]
    if embedded and not without_uploading:
        if policy() == "build_only":
            raise PolicyError(f"env(s) {', '.join(embedded)} run tests on hardware, which PLATFORMIO_MCP_POLICY=build_only forbids. Pass without_uploading=true or test a native env.")
    args = ["test", "-d", str(path)]
    if env:
        args += ["-e", env]
    if filter:
        args += ["-f", filter]
    if ignore:
        args += ["-i", ignore]
    if without_uploading:
        args.append("--without-uploading")
    if without_building:
        args.append("--without-building")
    if upload_port:
        args += ["--upload-port", upload_port]
    if verbose:
        args.append("-v")
    fd, report_path = tempfile.mkstemp(prefix="pio-test-", suffix=".json")
    os.close(fd)
    args += ["--json-output-path", report_path]
    res = run_pio(args, cwd=str(path), timeout=DEFAULT_TIMEOUTS["test"], tool="test")
    report: dict[str, Any] | None = None
    try:
        with open(report_path, encoding="utf-8") as fh:
            raw = fh.read().strip()
            report = json.loads(raw) if raw else None
    except (OSError, json.JSONDecodeError):
        report = None
    finally:
        try:
            os.remove(report_path)
        except OSError:
            pass
    build_errors = [d.to_dict() for d in parse_diagnostics(res.output) if d.kind == "error"]
    if report is None:
        return {"ok": False, "status": "error", "summary": f"pio test produced no report (exit {res.returncode}). " + (f"{len(build_errors)} build error(s); first: {build_errors[0]['file']}:{build_errors[0]['line']}: {build_errors[0]['message']}" if build_errors else "See output_tail."), "build_errors": build_errors, "output_tail": tail(res.output, 40), "log_path": res.log_path}
    s = summarize_test_report(report)
    failed_cases = [c for suite in s["suites"] for c in suite["cases"] if c["status"] in ("FAILED", "ERRORED")]
    ok = res.ok and s["failed"] == 0 and s["errored"] == 0
    summary = f"{s['total']} test case(s): {s['total'] - s['failed'] - s['errored'] - s['skipped']} passed, {s['failed']} failed, {s['errored']} errored, {s['skipped']} skipped in {s['duration_s']}s."
    if failed_cases:
        c = failed_cases[0]
        summary += f" First failure: {c['name']}" + (f" at {c['file']}:{c['line']}" if c.get("file") else "") + (f": {c['message']}" if c.get("message") else "")
    if build_errors:
        summary += f" {len(build_errors)} build error(s) in output."
    return {"ok": ok, "status": "passed" if ok else "failed", "summary": summary, **s, "build_errors": build_errors[:20], "exit_code": res.returncode, "log_path": res.log_path, "output_tail": tail(res.output, 30)}


@guard
def pio_check(project_dir: str | None = None, env: str | None = None, severity: str = "medium", pattern: str | None = None, skip_packages: bool = True, tool: str | None = None) -> dict[str, Any]:
    check_policy("build")
    path = resolve_project_dir(project_dir)
    if severity not in SEVERITY_ORDER:
        raise ValueError("severity must be low, medium, or high")
    args = ["check", "-d", str(path), "--json-output"]
    if env:
        args += ["-e", env]
    for sev in SEVERITY_ORDER[SEVERITY_ORDER.index(severity) :]:
        args += ["--severity", sev]
    if pattern:
        args += ["--pattern", pattern]
    if skip_packages:
        args.append("--skip-packages")
    if tool:
        args += ["--tool", tool]
    res = run_pio(args, cwd=str(path), timeout=DEFAULT_TIMEOUTS["build"], tool="check")
    text = res.output.strip()
    start = text.find("[")
    try:
        report = json.loads(text[start:]) if start >= 0 else []
    except json.JSONDecodeError:
        return {"ok": False, "error": "check_failed", "summary": f"pio check did not return JSON (exit {res.returncode}).", "output_tail": tail(res.output, 30), "log_path": res.log_path}
    s = summarize_check_report(report, project_dir=str(path))
    tools_failed = [t for t in s["tools"] if not t["succeeded"]]
    summary = f"{s['defect_count']} defect(s) at severity >= {severity}: {s['by_severity']['high']} high, {s['by_severity']['medium']} medium, {s['by_severity']['low']} low."
    if s["defects"]:
        d = s["defects"][0]
        summary += f" Top: [{d['severity']}] {d['file']}:{d['line']} {d['message']}"
    if tools_failed:
        summary += " Tool(s) failed: " + ", ".join(f"{t['tool']}({t['env']})" for t in tools_failed)
    return {"ok": not tools_failed, "summary": summary, **s, "log_path": res.log_path}


def register(mcp) -> None:
    mcp.tool(name="pio_test", description=(
        "Run PlatformIO unit tests (`pio test`, Unity framework) and return per-case pass/fail with file, line, and message. "
        "Tests live in test/test_<name>/. Native envs run on the host; embedded envs build, flash, and read results over serial "
        "(set without_uploading=true to only build them). filter/ignore take glob patterns like 'test_math*'."
    ))(pio_test)
    mcp.tool(name="pio_check", description=(
        "Static analysis (`pio check`, cppcheck by default; clangtidy/pvs-studio if configured). Returns defects grouped and sorted by "
        "severity with file, line, CWE, and message. severity is the minimum level to report (low|medium|high)."
    ))(pio_check)
