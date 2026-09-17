"""Parsers for the parts of PlatformIO output that have no --json-output."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

DIAG_RE = re.compile(
    r"^(?P<file>[^\s:][^:\n]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<kind>fatal error|error|warning|note):\s*(?P<msg>.*)$",
    re.MULTILINE,
)
LINKER_RE = re.compile(r"^.*?(undefined reference to .*|symbol\(s\) not found.*|multiple definition of .*|region .* overflowed by .*)$", re.MULTILINE)
SCONS_ERR_RE = re.compile(r"^\*\*\* \[(?P<target>[^\]]+)\] (?P<msg>.*)$", re.MULTILINE)
MEM_RE = re.compile(r"^(?P<kind>RAM|Flash):\s+\[[=\s]*\]\s+(?P<pct>[\d.]+)%\s+\(used (?P<used>\d+) bytes from (?P<total>\d+) bytes\)", re.MULTILINE)
RESULT_RE = re.compile(r"^=+ \[(?P<status>SUCCESS|FAILED|ERROR)\] Took (?P<secs>[\d.]+) seconds =+$", re.MULTILINE)
ENV_RE = re.compile(r"^Processing (?P<env>\S+) \(", re.MULTILINE)
PKG_SEARCH_HEAD_RE = re.compile(r"^Found (?P<total>\d+) packages \(page (?P<page>\d+) of (?P<pages>\d+)\)", re.MULTILINE)
PKG_LINE_RE = re.compile(r"^(?P<indent>[\s│├└─]*)(?P<kind>Platform|Tool|Library)?\s*(?P<spec>[\w.@:/ -]+?) @ (?P<version>[^\s(]+)(?: \((?P<req>[^)]*)\))?\s*$")

# Serial-port failures during upload, most specific first. `port_missing` is the generic "could not open" bucket.
PORT_ERROR_PATTERNS = (
    ("port_permission", re.compile(r"PermissionError\(13|Access is denied|Permission denied|Errno 13", re.I)),
    ("port_busy", re.compile(r"Device or resource busy|Resource busy|Errno 16|port is busy|already in use", re.I)),
    ("no_response", re.compile(
        r"Timed out waiting for packet header|Failed to connect to ESP|No serial data received|Wrong boot mode|Invalid head of packet|"
        r"programmer is not responding|not in sync|stk500_recv\(\)|stk500_getsync\(\)|Failed to open the debug port|No device found on",
        re.I,
    )),
    ("port_missing", re.compile(r"could not open port|A fatal error occurred: Could not open|SerialException|No such file or directory: '?/dev|Errno 2\b.*(?:tty|cu\.|COM)|FileNotFoundError.*(?:tty|cu\.|COM)|Could not find a port|No serial ports found", re.I)),
)


def classify_port_error(text: str) -> str | None:
    """Map an upload log to port_permission / port_busy / no_response / port_missing, or None when the failure is not port-related."""
    for code, rx in PORT_ERROR_PATTERNS:
        if rx.search(text):
            return code
    return None


@dataclass
class Diagnostic:
    kind: str
    file: str
    line: int
    column: int | None
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def parse_diagnostics(text: str) -> list[Diagnostic]:
    """Extract compiler errors/warnings/notes, linker errors, and SCons failure lines."""
    diags: list[Diagnostic] = []
    seen: set[tuple] = set()
    for m in DIAG_RE.finditer(text):
        key = (m["file"], m["line"], m["col"], m["kind"], m["msg"])
        if key in seen:
            continue
        seen.add(key)
        diags.append(Diagnostic(kind=m["kind"].replace("fatal ", ""), file=m["file"], line=int(m["line"]), column=int(m["col"]) if m["col"] else None, message=m["msg"].strip()))
    for m in LINKER_RE.finditer(text):
        msg = m.group(1).strip()
        key = ("linker", msg)
        if key in seen:
            continue
        seen.add(key)
        diags.append(Diagnostic(kind="error", file="<linker>", line=0, column=None, message=msg))
    for m in SCONS_ERR_RE.finditer(text):
        key = ("scons", m["target"], m["msg"])
        if key in seen:
            continue
        seen.add(key)
        diags.append(Diagnostic(kind="error", file=m["target"], line=0, column=None, message=f"build step failed: {m['msg']}"))
    return diags


def parse_memory(text: str) -> dict[str, dict]:
    """Return {'ram': {...}, 'flash': {...}} from the size report lines, if present."""
    out: dict[str, dict] = {}
    for m in MEM_RE.finditer(text):
        out[m["kind"].lower()] = {"percent": float(m["pct"]), "used_bytes": int(m["used"]), "total_bytes": int(m["total"])}
    return out


def parse_build_result(text: str) -> dict:
    """Overall status, duration, and environments seen in a `pio run` log."""
    statuses = [(m["status"], float(m["secs"])) for m in RESULT_RE.finditer(text)]
    envs = ENV_RE.findall(text)
    status = "unknown"
    if statuses:
        status = "failed" if any(s != "SUCCESS" for s, _ in statuses) else "success"
    return {"status": status, "environments": envs, "took_s": round(sum(t for _, t in statuses), 2) if statuses else None}


def parse_pkg_search(text: str) -> dict:
    """Parse the human-readable `pio pkg search` output into a list of packages."""
    head = PKG_SEARCH_HEAD_RE.search(text)
    packages: list[dict] = []
    block: list[str] = []

    def flush() -> None:
        if len(block) >= 2:
            spec = block[0].strip()
            meta = [p.strip() for p in block[1].split("•")]
            packages.append(
                {
                    "spec": spec,
                    "owner": spec.split("/")[0] if "/" in spec else None,
                    "name": spec.split("/")[-1],
                    "type": meta[0].lower() if meta else None,
                    "version": meta[1] if len(meta) > 1 else None,
                    "published": meta[2].replace("Published on ", "") if len(meta) > 2 else None,
                    "description": " ".join(l.strip() for l in block[2:]).strip() or None,
                }
            )
        block.clear()

    for line in text.split("\n"):
        if PKG_SEARCH_HEAD_RE.match(line):
            continue
        if not line.strip():
            flush()
            continue
        block.append(line)
    flush()
    return {
        "total": int(head["total"]) if head else len(packages),
        "page": int(head["page"]) if head else 1,
        "pages": int(head["pages"]) if head else 1,
        "packages": packages,
    }


def parse_pkg_list(text: str) -> list[dict]:
    """Parse `pio pkg list` / `pio pkg outdated` tree output into flat rows."""
    rows: list[dict] = []
    env = None
    for line in text.split("\n"):
        m = re.match(r"^Resolving (?P<env>\S+) dependencies", line)
        if m:
            env = m["env"]
            continue
        m = PKG_LINE_RE.match(line)
        if not m or not m["spec"].strip():
            continue
        rows.append({"env": env, "type": (m["kind"] or "Library").lower(), "spec": m["spec"].strip(), "version": m["version"], "required": (m["req"] or "").removeprefix("required: ").strip() or None})
    return rows


def parse_targets(text: str) -> list[dict]:
    """Parse the table printed by `pio run --list-targets`."""
    rows: list[dict] = []
    for line in text.split("\n"):
        if not line.strip() or line.startswith("Environment") or set(line.strip()) <= {"-", " "}:
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 3:
            continue
        rows.append({"env": parts[0], "group": parts[1], "name": parts[2], "title": parts[3] if len(parts) > 3 else None, "description": parts[4] if len(parts) > 4 else None})
    return rows


def summarize_test_report(report: dict) -> dict:
    """Reduce the `pio test --json-output-path` report to what an agent needs."""
    suites = []
    for s in report.get("test_suites", []):
        cases = [
            {
                "name": c.get("name"),
                "status": c.get("status"),
                "message": c.get("message") or c.get("exception"),
                "file": (c.get("source") or {}).get("file"),
                "line": (c.get("source") or {}).get("line"),
            }
            for c in s.get("test_cases", [])
        ]
        suites.append({"env": s.get("env_name"), "test": s.get("test_name"), "status": s.get("status"), "duration_s": round(s.get("duration", 0), 2), "cases": cases})
    return {
        "total": report.get("testcase_nums", 0),
        "failed": report.get("failure_nums", 0),
        "errored": report.get("error_nums", 0),
        "skipped": report.get("skipped_nums", 0),
        "duration_s": round(report.get("duration", 0), 2),
        "suites": suites,
    }


def summarize_check_report(report: list[dict], project_dir: str | None = None) -> dict:
    """Group `pio check --json-output` defects by file and severity."""
    by_severity = {"high": 0, "medium": 0, "low": 0}
    defects = []
    tools = []
    for entry in report:
        tools.append({"env": entry.get("env"), "tool": entry.get("tool"), "succeeded": entry.get("succeeded"), "duration_s": round(entry.get("duration", 0), 2)})
        for d in entry.get("defects", []):
            sev = d.get("severity", "low")
            by_severity[sev] = by_severity.get(sev, 0) + 1
            f = d.get("file") or ""
            if project_dir and f.startswith(project_dir):
                f = f[len(project_dir) :].lstrip("/\\")
            defects.append({"severity": sev, "category": d.get("category"), "id": d.get("id"), "message": d.get("message"), "file": f, "line": d.get("line"), "column": d.get("column"), "cwe": d.get("cwe"), "tool": entry.get("tool"), "env": entry.get("env")})
    order = {"high": 0, "medium": 1, "low": 2}
    defects.sort(key=lambda d: (order.get(d["severity"], 3), d["file"], d["line"] or 0))
    return {"defect_count": len(defects), "by_severity": by_severity, "tools": tools, "defects": defects}
