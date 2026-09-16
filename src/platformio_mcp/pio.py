"""Locate and run the PlatformIO CLI, clean its output, keep full logs on disk."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NOISE_LINES = (
    "Verbose mode can be enabled via `-v, --verbose` option",
    "LDF: Library Dependency Finder -> https://bit.ly/configure-pio-ldf",
)

DEFAULT_TIMEOUTS = {
    "build": 20 * 60,
    "upload": 5 * 60,
    "test": 20 * 60,
    "default": 2 * 60,
}

INSTALL_HINT = (
    "PlatformIO Core was not found. Install it with one of:\n"
    "  uv tool install platformio\n"
    "  pipx install platformio\n"
    "  pip install platformio\n"
    "or run this server as `uvx \"platformio.mcp[platformio]\"` to bundle it. "
    "You can also point PLATFORMIO_MCP_PIO at a pio executable."
)


class PioNotFound(RuntimeError):
    pass


@dataclass
class PioResult:
    args: list[str]
    returncode: int
    output: str
    duration_s: float
    log_path: str | None = None
    timed_out: bool = False
    cwd: str | None = None
    raw_output: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def log_dir() -> Path:
    d = Path(os.environ.get("PLATFORMIO_MCP_LOG_DIR", Path.home() / ".platformio-mcp" / "logs"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_pio() -> list[str] | None:
    """Return the command prefix for PlatformIO, or None if it cannot be found."""
    override = os.environ.get("PLATFORMIO_MCP_PIO")
    if override:
        return [override]
    for name in ("platformio", "pio"):
        found = shutil.which(name)
        if found:
            return [found]
    penv = Path.home() / ".platformio" / "penv"
    for candidate in (penv / "bin" / "platformio", penv / "bin" / "pio", penv / "Scripts" / "platformio.exe", penv / "Scripts" / "pio.exe"):
        if candidate.exists():
            return [str(candidate)]
    try:
        import platformio  # noqa: F401

        return [sys.executable, "-m", "platformio"]
    except ImportError:
        return None


def require_pio() -> list[str]:
    cmd = find_pio()
    if cmd is None:
        raise PioNotFound(INSTALL_HINT)
    return cmd


def clean_output(text: str) -> str:
    """Strip ANSI codes, the obsolete-core banner, progress spinners, and boilerplate lines."""
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out: list[str] = []
    in_banner = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("*****") and len(stripped) > 20 and set(stripped) == {"*"}:
            in_banner = not in_banner
            continue
        if in_banner:
            continue
        if stripped in NOISE_LINES:
            continue
        out.append(line.rstrip())
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def tail(text: str, n: int = 40) -> str:
    lines = text.split("\n")
    return "\n".join(lines[-n:]) if len(lines) > n else text


def max_logs() -> int:
    try:
        return max(int(os.environ.get("PLATFORMIO_MCP_MAX_LOGS", "200")), 1)
    except ValueError:
        return 200


def prune_logs(directory: Path, keep: int) -> int:
    """Delete the oldest *.log files so at most `keep` remain. Returns how many were removed."""
    logs = sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime)
    removed = 0
    for old in logs[: max(len(logs) - keep, 0)]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


_log_counter = 0


def write_log(tool: str, text: str) -> str:
    global _log_counter
    _log_counter += 1
    directory = log_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}-{_log_counter:03d}-{tool}.log"
    path.write_text(text, encoding="utf-8")
    prune_logs(directory, max_logs())
    return str(path)


def run_pio(
    args: list[str],
    cwd: str | Path | None = None,
    timeout: float | None = None,
    tool: str = "pio",
    keep_log: bool = True,
    env_extra: dict[str, str] | None = None,
    stdin_data: str | None = None,
) -> PioResult:
    cmd = require_pio() + list(args)
    env = os.environ.copy()
    env.update({"PLATFORMIO_NO_ANSI": "true", "PLATFORMIO_DISABLE_PROGRESSBAR": "true", "PYTHONUNBUFFERED": "1"})
    if env_extra:
        env.update(env_extra)
    if timeout is None:
        timeout = DEFAULT_TIMEOUTS["default"]
    start = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            input=stdin_data,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        raw = proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = -1
        raw = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")) + f"\n[platformio-mcp] timed out after {timeout:.0f}s"
    duration = time.monotonic() - start
    output = clean_output(raw)
    log_path = write_log(tool, f"$ {' '.join(cmd)}\n(cwd: {cwd})\n\n{output}\n") if keep_log else None
    return PioResult(args=list(args), returncode=rc, output=output, duration_s=round(duration, 2), log_path=log_path, timed_out=timed_out, cwd=str(cwd) if cwd else None, raw_output=raw)


def resolve_project_dir(project_dir: str | None) -> Path:
    """Turn a user-supplied path (or env/cwd default) into an absolute PlatformIO project directory."""
    raw = project_dir or os.environ.get("PLATFORMIO_MCP_PROJECT_DIR") or os.getcwd()
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"Project directory does not exist: {path}")
    if not (path / "platformio.ini").exists():
        raise FileNotFoundError(f"No platformio.ini in {path}. Pass the project root, or call pio_project_init to create one.")
    return path
