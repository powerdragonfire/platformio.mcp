"""Shared state and helpers used by every tool module."""

from __future__ import annotations

import functools
import json
import os
import re
from collections.abc import Callable
from typing import Any

from .monitor import MonitorManager
from .pio import PioNotFound, PioResult, resolve_project_dir, run_pio

POLICIES = ("full", "build_only", "read_only")

monitors = MonitorManager()

ToolFn = Callable[..., dict[str, Any]]


class PolicyError(PermissionError):
    pass


def policy() -> str:
    value = os.environ.get("PLATFORMIO_MCP_POLICY", "full").strip().lower()
    return value if value in POLICIES else "full"


def check_policy(action: str) -> None:
    """action is 'build' (compiles, installs packages, edits project) or 'flash' (touches hardware state)."""
    current = policy()
    if current == "read_only" and action in ("build", "flash"):
        raise PolicyError(f"PLATFORMIO_MCP_POLICY=read_only blocks '{action}' actions. Only listing and inspection tools are allowed.")
    if current == "build_only" and action == "flash":
        raise PolicyError("PLATFORMIO_MCP_POLICY=build_only blocks flashing, erasing, and writing to devices. Builds and tests without upload still work.")


def guard(fn: ToolFn) -> ToolFn:
    """Turn expected failures into a structured {ok: false} result instead of a protocol error."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except PioNotFound as exc:
            return {"ok": False, "error": "pio_not_found", "summary": str(exc)}
        except PolicyError as exc:
            return {"ok": False, "error": "policy_denied", "summary": str(exc), "policy": policy()}
        except FileNotFoundError as exc:
            return {"ok": False, "error": "not_found", "summary": str(exc)}
        except re.error as exc:
            return {"ok": False, "error": "bad_regex", "summary": f"invalid regular expression '{exc.pattern}': {exc.msg} (position {exc.pos})."}
        except (KeyError, ValueError, RuntimeError, OSError) as exc:
            return {"ok": False, "error": type(exc).__name__, "summary": str(exc)}
        except Exception as exc:  # last resort: a structured failure beats a bare "Error executing tool"
            return {"ok": False, "error": type(exc).__name__, "summary": f"{type(exc).__name__}: {exc}"}

    return wrapper


def run_json(args: list[str], cwd: str | None = None, timeout: float | None = None, tool: str = "pio") -> tuple[PioResult, Any]:
    """Run a pio command that supports --json-output and parse it."""
    res = run_pio(args, cwd=cwd, timeout=timeout, tool=tool)
    if not res.ok:
        raise RuntimeError(f"`pio {' '.join(args)}` failed (exit {res.returncode}). Output:\n{res.output[-2000:]}")
    text = res.output.strip()
    # pio sometimes prints warnings before the JSON payload; find the first bracket/brace.
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=-1)
    if start < 0:
        raise RuntimeError(f"`pio {' '.join(args)}` returned no JSON. Output:\n{text[-1000:]}")
    try:
        return res, json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse JSON from `pio {' '.join(args)}`: {exc}. See {res.log_path}") from exc


def project_config(project_dir: str | None) -> dict[str, dict[str, Any]]:
    """`pio project config --json-output` as {section: {key: value}}."""
    path = resolve_project_dir(project_dir)
    _, data = run_json(["project", "config", "--json-output"], cwd=str(path), tool="project-config")
    out: dict[str, dict[str, Any]] = {}
    for section, items in data:
        out[section] = {k: v for k, v in items}
    return out


def env_names(config: dict[str, dict[str, Any]]) -> list[str]:
    return [s[4:] for s in config if s.startswith("env:")]


def default_envs(config: dict[str, dict[str, Any]]) -> list[str]:
    raw = config.get("platformio", {}).get("default_envs")
    if not raw:
        return env_names(config)
    if isinstance(raw, str):
        return [e.strip() for e in raw.replace("\n", ",").split(",") if e.strip()]
    return list(raw)


def env_setting(config: dict[str, dict[str, Any]], env: str, key: str, default: Any = None) -> Any:
    """Look a key up in [env:x], following one level of `extends`, then [env]."""
    section = config.get(f"env:{env}", {})
    if key in section:
        return section[key]
    bases = section.get("extends") or []
    if isinstance(bases, str):
        bases = [b.strip() for b in bases.replace("\n", ",").split(",") if b.strip()]
    for base in bases:
        if key in config.get(base, {}):
            return config[base][key]
    return config.get("env", {}).get(key, default)
