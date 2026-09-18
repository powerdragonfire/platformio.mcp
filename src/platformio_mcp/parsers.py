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


# --- library dependencies ---------------------------------------------------------------

DEP_GRAPH_LINE_RE = re.compile(r"^(?P<indent>(?:\|   |    )*)[|+\\]-- (?P<name>.+?)(?: @ (?P<version>[^\s(]+))?(?: \((?P<path>.*)\))?\s*$")
URL_SCHEME_RE = re.compile(r"^(?:git\+)?(?:https?|ssh|git|file|symlink)://|^git@[^:]+:", re.I)
RECURSION_RE = re.compile(r"RecursionError|maximum recursion depth exceeded", re.I)


def parse_lib_spec(spec: str) -> dict:
    """Split one `lib_deps` entry into owner, name, version/ref, url, and kind.

    Handles `Name`, `owner/Name@^1.2`, `Name@1.2.3`, `Name=<url>`, `https://.../repo.git#tag`,
    `git@host:owner/repo.git`, `file://` and `symlink://` paths, and bare registry ids.
    """
    raw = spec.strip()
    out = {"spec": raw, "owner": None, "name": None, "version": None, "url": None, "kind": "registry", "pinned": False}
    if not raw:
        out["kind"] = "empty"
        return out
    alias = None
    body = raw
    if "=" in raw and URL_SCHEME_RE.search(raw.split("=", 1)[1].strip()):
        alias, body = (p.strip() for p in raw.split("=", 1))
    if URL_SCHEME_RE.search(body):
        url, ref = body, None
        if "#" in url:
            url, ref = url.split("#", 1)
        elif re.search(r"\.git@[^/]+$", url):
            url, ref = url.rsplit("@", 1)
        base = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        base = re.sub(r"\.(git|zip|tar\.gz|tgz)$", "", base, flags=re.I)
        local = url.lower().startswith(("file://", "symlink://"))
        out.update(name=alias or base or None, version=ref, url=url, kind="local" if local else "url", pinned=bool(ref) or local)
        return out
    if body.isdigit():
        out.update(name=body, kind="id")
        return out
    version = None
    if "@" in body:
        body, version = body.split("@", 1)
        version = version.strip() or None
    owner = None
    if "/" in body:
        owner, body = body.rsplit("/", 1)
    out.update(owner=owner.strip() if owner else None, name=body.strip() or None, version=version, pinned=version is not None)
    return out


def parse_library_properties(text: str) -> dict:
    """Parse an Arduino `library.properties` file into name, version, and dependency names."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        fields[k.strip().lower()] = v.strip()
    deps = []
    for item in fields.get("depends", "").split(","):
        name = re.sub(r"\s*\(.*\)\s*$", "", item).strip()
        if name:
            deps.append(name)
    return {"name": fields.get("name"), "version": fields.get("version"), "dependencies": deps}


def library_json_dependencies(manifest: dict) -> list[str]:
    """Names from a `library.json` `dependencies` field in any of its three shapes."""
    deps = manifest.get("dependencies")
    names: list[str] = []
    if isinstance(deps, dict):
        names = [re.sub(r"^[^/]+/", "", k) for k in deps]
    elif isinstance(deps, list):
        for d in deps:
            if isinstance(d, str):
                names.append(parse_lib_spec(d)["name"] or d)
            elif isinstance(d, dict) and d.get("name"):
                names.append(str(d["name"]))
    return [n for n in names if n]


def parse_dependency_graph(text: str) -> list[dict]:
    """Parse the LDF 'Dependency Graph' tree printed by `pio run` into nested nodes."""
    roots: list[dict] = []
    stack: list[tuple[int, dict]] = []
    in_graph = False
    for line in text.split("\n"):
        if line.strip() == "Dependency Graph":
            in_graph = True
            continue
        if not in_graph:
            continue
        m = DEP_GRAPH_LINE_RE.match(line)
        if not m:
            if line.strip():
                in_graph = False
            continue
        depth = len(m["indent"]) // 4
        node = {"name": m["name"].strip(), "version": m["version"], "path": m["path"], "children": []}
        while stack and stack[-1][0] >= depth:
            stack.pop()
        (stack[-1][1]["children"] if stack else roots).append(node)
        stack.append((depth, node))
    return roots


def has_recursion_error(text: str) -> bool:
    return bool(RECURSION_RE.search(text))
# --- runtime memory telemetry (free heap, stack high-water marks) ------------------------------

_NUM = r"(?P<value>\d+)\s*(?P<unit>KiB|KB|kB|MB|bytes?|B|words?)?\b"
_SEP = r"\s*(?:[:=]|\bis\b)?\s*"
HEAP_METRIC_RES: list[tuple[str, re.Pattern[str]]] = [
    ("min_free_heap", re.compile(r"(?:min(?:imum)?[\s_.]*free[\s_]*(?:internal[\s_]*)?heap(?:[\s_]*size)?|free[\s_]*heap[\s_]*min(?:imum)?|lowest[\s_]*free[\s_]*heap|ESP\.getMinFreeHeap\(\)|esp_get_minimum_free_heap_size\(\)|minFreeHeap|min_free(?![\s_]*block))" + _SEP + _NUM, re.I)),
    ("largest_free_block", re.compile(r"(?:largest[\s_]*free[\s_]*block|largest[\s_]*(?:free[\s_]*)?(?:block|alloc(?:atable)?)|max(?:imum)?[\s_]*alloc(?:atable)?(?:[\s_]*heap|[\s_]*block|[\s_]*size)?|ESP\.getMaxAllocHeap\(\)|maxAllocHeap|biggest[\s_]*free[\s_]*block)" + _SEP + _NUM, re.I)),
    ("psram_free", re.compile(r"(?:free[\s_]*psram|psram[\s_]*free|ESP\.getFreePsram\(\)|freePsram|free_psram)" + _SEP + _NUM, re.I)),
    ("allocated", re.compile(r"(?:allocated(?:[\s_]*heap)?|heap[\s_]*used|used[\s_]*heap)" + _SEP + _NUM, re.I)),
    ("free_heap", re.compile(r"(?:free[\s_]*(?:internal[\s_]*|dram[\s_]*)?heap(?:[\s_]*size)?|heap[\s_]*free|ESP\.getFreeHeap\(\)|esp_get_free_heap_size\(\)|freeHeap|free_heap|\bheap)" + _SEP + _NUM, re.I)),
]
# Trailing "min: N largest: N" on a line that already reported free heap (the Arduino snippet in the hint).
HEAP_TRAILER_RES: list[tuple[str, re.Pattern[str]]] = [
    ("min_free_heap", re.compile(r"\bmin" + _SEP + _NUM, re.I)),
    ("largest_free_block", re.compile(r"\blargest" + _SEP + _NUM, re.I)),
]
STACK_RES: list[re.Pattern[str]] = [
    # "loopTask: stack hwm 1234", "Stack HWM for loopTask: 1234 bytes", "uxTaskGetStackHighWaterMark(NULL) = 812"
    re.compile(r"(?:(?P<task>[\w.-]+)\s*[:\-]\s*)?(?:stack[\s_]*(?:hwm|high[\s_-]*water[\s_-]*mark|free|headroom|remaining|left)|high[\s_-]*water[\s_-]*mark|uxTaskGetStackHighWaterMark(?:\((?P<arg>[^)]*)\))?)(?:\s*(?:for|of)\s+(?P<task2>[\w.-]+))?(?:\s*\((?P<task3>[^)]+)\))?" + _SEP + _NUM, re.I),
    # "loopTask: 1234 bytes free"
    re.compile(r"^\s*(?P<task>[\w.-]+)\s*[:=]\s*" + _NUM + r"\s*(?:free|left|remaining)\b", re.I),
]
VTASKLIST_HEADER_RE = re.compile(r"\bname\s+state\s+prio(?:rity)?\s+stack\s+(?:num|#|task\s*num(?:ber)?)", re.I)
VTASKLIST_ROW_RE = re.compile(r"^\s*(?P<name>\S+)\s+(?P<state>[XRBSD])\s+(?P<prio>\d+)\s+(?P<stack>\d+)\s+(?P<num>\d+)\b")
HEAP_SUMMARY_RE = re.compile(r"heap summary for capabilities", re.I)
HEAP_TOTALS_RE = re.compile(r"^\s*totals\s*:", re.I)
HEAP_TOTALS_LINE_RE = re.compile(r"\bfree\s+(?P<free>\d+)\s+allocated\s+(?P<allocated>\d+)(?:\s+min_free\s+(?P<min_free>\d+))?(?:\s+largest_free_block\s+(?P<largest>\d+))?", re.I)
HEAP_REGION_LINE_RE = re.compile(r"^(?:at 0x|largest_free_block\b|alloc_blocks\b)", re.I)
GENERIC_MEM_RE = re.compile(r"(?P<name>(?:[A-Za-z_][\w .-]{0,40}?)?(?:heap|stack|psram)[\w .-]{0,30}?)\s*[:=]\s*" + _NUM, re.I)
UNIT_FACTORS = {"kib": 1024, "kb": 1024, "mb": 1024 * 1024, "word": 4, "words": 4}
NON_STACK_TASK_RE = re.compile(r"heap|psram|dram|iram", re.I)


def _to_bytes(value: str, unit: str | None) -> tuple[int, str | None]:
    n = int(value)
    u = (unit or "").lower()
    factor = UNIT_FACTORS.get(u)
    if factor:
        return n * factor, u
    return n, (u or None)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "value"


def parse_memory_telemetry(lines: list[str], pattern: str | None = None) -> dict:
    """Pull heap / stack telemetry out of serial output.

    Returns {"samples": [...], "series": {metric: [values]}, "tasks": {task: [stack_free_bytes]},
    "formats": [names of the formats seen], "recognized": bool}. Sample order follows line order;
    every sample carries the index of the line it came from.
    """
    custom = re.compile(pattern) if pattern else None
    if custom is not None and "value" not in custom.groupindex:
        raise ValueError("pattern must define a named group (?P<value>...) and may define (?P<name>...)")
    samples: list[dict] = []
    series: dict[str, list[int]] = {}
    tasks: dict[str, list[int]] = {}
    formats: set[str] = set()
    in_summary = False
    after_totals = False
    in_tasklist = False

    def add(metric: str, value: int, idx: int, raw: str, fmt: str, unit: str | None = None) -> None:
        samples.append({"metric": metric, "value": value, "line": idx, "task": None, "unit": unit, "raw": raw.strip()[:160]})
        series.setdefault(metric, []).append(value)
        formats.add(fmt)

    def add_stack(task: str | None, value: int, idx: int, raw: str, fmt: str, unit: str | None) -> None:
        task = (task or "unknown").strip()
        samples.append({"metric": "stack_free", "value": value, "line": idx, "task": task, "unit": unit, "raw": raw.strip()[:160]})
        tasks.setdefault(task, []).append(value)
        formats.add(fmt)

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            in_tasklist = False
            continue
        # ESP-IDF heap_caps_print_heap_info block: only the Totals line counts, per-region lines are skipped.
        if HEAP_SUMMARY_RE.search(stripped):
            in_summary, after_totals = True, False
            continue
        if in_summary:
            if HEAP_TOTALS_RE.match(stripped):
                after_totals = True
                continue
            m = HEAP_TOTALS_LINE_RE.search(stripped)
            if m and after_totals:
                add("free_heap", int(m["free"]), idx, line, "esp_idf_heap_info")
                add("allocated", int(m["allocated"]), idx, line, "esp_idf_heap_info")
                if m["min_free"]:
                    add("min_free_heap", int(m["min_free"]), idx, line, "esp_idf_heap_info")
                if m["largest"]:
                    add("largest_free_block", int(m["largest"]), idx, line, "esp_idf_heap_info")
                in_summary, after_totals = False, False
                continue
            if m or HEAP_REGION_LINE_RE.match(stripped):
                continue
            in_summary, after_totals = False, False
        # FreeRTOS vTaskList table: rows are only trusted after a header line.
        if VTASKLIST_HEADER_RE.search(stripped):
            in_tasklist = True
            continue
        if in_tasklist:
            m = VTASKLIST_ROW_RE.match(line)
            if m:
                add_stack(m["name"], int(m["stack"]), idx, line, "vtasklist", None)
                samples[-1].update({"state": m["state"], "priority": int(m["prio"]), "task_number": int(m["num"])})
                continue
            if set(stripped) <= {"-", "=", " "}:
                continue
            in_tasklist = False
        consumed: list[tuple[int, int]] = []

        def unclaimed(span: tuple[int, int], taken: list[tuple[int, int]] = consumed) -> bool:
            return all(span[1] <= a or span[0] >= b for a, b in taken)

        if custom is not None:
            for m in custom.finditer(line):
                name = _slug(m.groupdict().get("name") or "custom")
                value, unit = _to_bytes(m["value"], m.groupdict().get("unit"))
                add(name, value, idx, line, "custom", unit)
                consumed.append(m.span())
        matched_stack = False
        for rx in STACK_RES:
            for m in rx.finditer(line):
                if not unclaimed(m.span()):
                    continue
                gd = m.groupdict()
                arg = gd.get("arg")
                task = gd.get("task") or gd.get("task2") or gd.get("task3") or (arg if arg and arg.strip().upper() != "NULL" else None)
                if task and NON_STACK_TASK_RE.search(task):
                    continue
                value, unit = _to_bytes(m["value"], m["unit"])
                add_stack(task, value, idx, line, "stack_hwm", unit)
                consumed.append(m.span())
                matched_stack = True
        matched_heap = False
        for metric, rx in HEAP_METRIC_RES:
            for m in rx.finditer(line):
                if not unclaimed(m.span()):
                    continue
                value, unit = _to_bytes(m["value"], m["unit"])
                add(metric, value, idx, line, "heap_line", unit)
                consumed.append(m.span())
                matched_heap = True
        if matched_heap:
            for metric, rx in HEAP_TRAILER_RES:
                for m in rx.finditer(line):
                    if unclaimed(m.span()):
                        value, unit = _to_bytes(m["value"], m["unit"])
                        add(metric, value, idx, line, "heap_line", unit)
                        consumed.append(m.span())
        if matched_heap or matched_stack:
            continue
        for m in GENERIC_MEM_RE.finditer(line):
            if not unclaimed(m.span()):
                continue
            value, unit = _to_bytes(m["value"], m["unit"])
            add(_slug(m["name"]), value, idx, line, "generic", unit)
            consumed.append(m.span())
    return {"samples": samples, "series": series, "tasks": tasks, "formats": sorted(formats), "recognized": bool(samples)}
