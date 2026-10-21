"""Library dependency audit: declared vs installed, name collisions, unpinned specs, cycles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core import default_envs, env_setting, guard, project_config
from ..parsers import (
    has_recursion_error,
    library_json_dependencies,
    parse_dependency_graph,
    parse_lib_spec,
    parse_library_properties,
)
from ..pio import DEFAULT_TIMEOUTS, resolve_project_dir, run_pio, tail

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
# `pio test` installs the test framework into libdeps without a lib_deps entry.
TEST_FRAMEWORKS = {"unity", "googletest", "doctest", "catch2"}


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def read_library_manifest(directory: Path) -> dict[str, Any] | None:
    """Read `library.json` (preferred) or `library.properties` from a library directory."""
    lj = directory / "library.json"
    if lj.is_file():
        try:
            data = json.loads(lj.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            return {"name": data.get("name"), "version": str(data["version"]) if data.get("version") is not None else None, "dependencies": library_json_dependencies(data), "manifest": "library.json"}
    lp = directory / "library.properties"
    if lp.is_file():
        return {**parse_library_properties(lp.read_text(encoding="utf-8", errors="replace")), "manifest": "library.properties"}
    return None


def _scan_dir(base: Path, source: str, project: Path) -> list[dict[str, Any]]:
    if not base.is_dir():
        return []
    rows = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        manifest = read_library_manifest(d) or {"name": None, "version": None, "dependencies": [], "manifest": None}
        try:
            rel = str(d.relative_to(project))
        except ValueError:
            rel = str(d)
        rows.append({"name": manifest["name"] or d.name, "dir_name": d.name, "version": manifest["version"], "dependencies": manifest["dependencies"], "manifest": manifest["manifest"], "source": source, "path": rel})
    return rows


def installed_libraries(project: Path, env: str, extra_dirs: list[str]) -> list[dict[str, Any]]:
    """Libraries PlatformIO can see, in lookup priority order: lib/, lib_extra_dirs, then .pio/libdeps/<env>."""
    rows = _scan_dir(project / "lib", "lib", project)
    for extra in extra_dirs:
        p = Path(extra)
        rows += _scan_dir(p if p.is_absolute() else project / p, "extra", project)
    rows += _scan_dir(project / ".pio" / "libdeps" / env, "libdeps", project)
    return rows


def _matches(declared: dict[str, Any], lib: dict[str, Any]) -> bool:
    name = (declared["name"] or "").lower()
    return bool(name) and name in {lib["name"].lower(), lib["dir_name"].lower()}


def find_cycles(libs: list[dict[str, Any]]) -> list[list[str]]:
    """Cycles in the manifest-declared dependency graph, restricted to installed libraries."""
    by_name = {lib["name"].lower(): lib for lib in libs}
    graph = {n: [d.lower() for d in lib["dependencies"] if d.lower() in by_name] for n, lib in by_name.items()}
    cycles: list[list[str]] = []
    seen_cycles: set[tuple[str, ...]] = set()
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        path.append(node)
        for nxt in graph[node]:
            if state.get(nxt) == 1:
                cycle = path[path.index(nxt) :]
                key = tuple(sorted(cycle))
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append([by_name[n]["name"] for n in cycle])
            elif nxt not in state:
                visit(nxt)
        path.pop()
        state[node] = 2

    for n in graph:
        if n not in state:
            visit(n)
    return cycles


def audit(declared: list[dict[str, Any]], installed: list[dict[str, Any]], config: dict[str, dict[str, Any]], env: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    by_name: dict[str, list[dict[str, Any]]] = {}
    for lib in installed:
        by_name.setdefault(lib["name"].lower(), []).append(lib)
    for name, libs in by_name.items():
        if len(libs) < 2:
            continue
        sources = {lib["source"] for lib in libs}
        shadowing = "lib" in sources and "libdeps" in sources
        winner = libs[0]
        copies = ", ".join(f"{lib['path']}@{lib['version'] or '?'}" for lib in libs)
        issues.append({
            "severity": "error" if shadowing else "warning",
            "kind": "name_collision",
            "library": winner["name"],
            "paths": [lib["path"] for lib in libs],
            "message": f"{len(libs)} libraries named '{winner['name']}' ({copies}). "
            + ("The copy in lib/ shadows the registry one. " if shadowing else "")
            + f"PlatformIO picks the first match in lookup order (lib/, lib_extra_dirs, then lib_deps order), so {winner['path']} wins and a reorder of lib_deps silently changes the build (platformio-core#3598).",
            "fix": "Keep one copy: pin the registry spec as owner/Name@version in lib_deps and delete the duplicate, or run pio_clean(full=true) and pio_build to reinstall.",
        })
    for spec in declared:
        if spec["kind"] == "registry" and not spec["pinned"]:
            issues.append({
                "severity": "warning",
                "kind": "unpinned",
                "library": spec["name"],
                "spec": spec["spec"],
                "message": f"'{spec['spec']}' has no version constraint, so every fresh install resolves to whatever is newest and name-only lookups can pick a different library with the same name.",
                "fix": f"Pin it: {spec['owner'] + '/' if spec['owner'] else ''}{spec['name']}@^<major.minor> (see pio_pkg_search for the owner and latest version).",
            })
        if spec["kind"] == "empty":
            continue
        if not any(_matches(spec, lib) for lib in installed):
            issues.append({
                "severity": "warning",
                "kind": "not_installed",
                "library": spec["name"],
                "spec": spec["spec"],
                "message": f"'{spec['spec']}' is declared in lib_deps for env {env} but nothing matching is installed under .pio/libdeps/{env} or lib/.",
                "fix": "Run pio_build (the LDF installs lib_deps on first build) or pio_pkg_install(spec=...) to fetch it; check the spelling/owner with pio_pkg_search.",
            })
    depended_on = {d.lower() for lib in installed for d in lib["dependencies"]}
    for lib in installed:
        if lib["source"] != "libdeps" or lib["name"].lower() in TEST_FRAMEWORKS:
            continue
        if any(_matches(spec, lib) for spec in declared) or lib["name"].lower() in depended_on or lib["dir_name"].lower() in depended_on:
            continue
        issues.append({
            "severity": "info",
            "kind": "undeclared",
            "library": lib["name"],
            "path": lib["path"],
            "message": f"{lib['path']} is installed but no lib_deps entry or installed library depends on it; probably left over after a removal. The LDF may still link it in.",
            "fix": f"Remove it with pio_pkg_uninstall(spec='{lib['name']}') or pio_clean(full=true) to rebuild .pio/libdeps from lib_deps.",
        })
    for cycle in find_cycles(installed):
        issues.append({
            "severity": "error",
            "kind": "circular",
            "libraries": cycle,
            "message": "Circular dependency between installed libraries: " + " -> ".join(cycle + [cycle[0]]) + ". PlatformIO's dependency finder recurses on this and can crash with RecursionError (platformio-core#5261).",
            "fix": "Break the cycle: drop the back-reference from one library.json/library.properties, or set lib_ldf_mode = chain (not deep) so headers are not followed transitively.",
        })
    ldf_mode = str(env_setting(config, env, "lib_ldf_mode", "") or "").lower()
    if ldf_mode == "off" and any(i["kind"] == "not_installed" for i in issues):
        issues.append({
            "severity": "info",
            "kind": "ldf_mode",
            "message": "lib_ldf_mode = off disables automatic dependency discovery, so transitive dependencies of lib_deps entries must be listed explicitly.",
            "fix": "List every library the build needs in lib_deps, or remove lib_ldf_mode = off.",
        })
    compat = str(env_setting(config, env, "lib_compat_mode", "") or "").lower()
    if compat == "off" and any(i["kind"] == "name_collision" for i in issues):
        issues.append({
            "severity": "info",
            "kind": "ldf_mode",
            "message": "lib_compat_mode = off lets the LDF pick libraries written for other frameworks or platforms, which makes name collisions more likely to resolve to the wrong copy.",
            "fix": "Use lib_compat_mode = soft (default) or strict so incompatible duplicates are skipped.",
        })
    issues.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
    return issues


@guard
def pio_deps_check(project_dir: str | None = None, env: str | None = None, build: bool = False) -> dict[str, Any]:
    path = resolve_project_dir(project_dir)
    cfg = project_config(str(path))
    env = env or (default_envs(cfg) or [None])[0]
    if not env:
        raise ValueError("platformio.ini declares no [env:*] section; nothing to audit.")
    declared = [parse_lib_spec(s) for s in _as_list(env_setting(cfg, env, "lib_deps"))]
    installed = installed_libraries(path, env, _as_list(env_setting(cfg, env, "lib_extra_dirs")))
    issues = audit(declared, installed, cfg, env)
    graph: list[dict[str, Any]] | None = None
    build_info: dict[str, Any] | None = None
    if build:
        res = run_pio(["run", "-d", str(path), "-e", env], cwd=str(path), timeout=DEFAULT_TIMEOUTS["build"], tool="deps-build")
        graph = parse_dependency_graph(res.output)
        build_info = {"ok": res.ok, "duration_s": res.duration_s, "log_path": res.log_path, "output_tail": tail(res.output, 30)}
        if has_recursion_error(res.output):
            issues.insert(0, {
                "severity": "error",
                "kind": "circular",
                "message": "The build crashed with RecursionError inside PlatformIO's dependency finder, which happens when libraries depend on each other in a loop (platformio-core#5261).",
                "fix": "Inspect the manifests of the libraries named in the log, remove the back-reference, or set lib_ldf_mode = chain.",
            })
    counts = {sev: sum(1 for i in issues if i["severity"] == sev) for sev in SEVERITY_ORDER}
    parts = [f"env {env}: {len(declared)} lib_deps entr{'y' if len(declared) == 1 else 'ies'}, {len(installed)} installed librar{'y' if len(installed) == 1 else 'ies'}."]
    if issues:
        first = issues[0]
        parts.append(f"{counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} note(s); first: [{first['kind']}] {first['message']}")
    else:
        parts.append("No collisions, unpinned specs, missing or leftover libraries, or cycles found.")
    if build_info:
        parts.append(f"Build {'succeeded' if build_info['ok'] else 'failed'}; dependency graph has {len(graph or [])} root librar{'y' if len(graph or []) == 1 else 'ies'}.")
    return {
        "ok": counts["error"] == 0 and (build_info is None or build_info["ok"]),
        "summary": " ".join(parts),
        "env": env,
        "declared": declared,
        "installed": installed,
        "graph": graph,
        "build": build_info,
        "issues": issues,
        "issue_count": len(issues),
        "counts": counts,
    }


def register(mcp) -> None:
    mcp.tool(name="pio_deps_check", description=(
        "Audit the project's library dependencies before they bite: compares lib_deps against what is installed in "
        ".pio/libdeps/<env>, lib/, and lib_extra_dirs, and reports name collisions (two libraries with the same name, where "
        "lib_deps order silently decides which one wins), unpinned specs, declared-but-missing and leftover libraries, and "
        "circular dependencies between manifests. build=true also runs `pio run` and returns the LDF dependency graph as a tree, "
        "flagging a RecursionError as a cycle. Each issue carries a severity, message, and a concrete fix."
    ))(pio_deps_check)
