#!/usr/bin/env python3
"""Release helper: keep every version string in sync and the changelog honest.

    python scripts/release.py check [--tag v0.3.0]   verify versions, changelog, tag
    python scripts/release.py bump minor|major|patch|X.Y.Z   update files, roll changelog
    python scripts/release.py notes [X.Y.Z]          print the changelog section

Standard library only, so CI runs it before any dependency is installed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
SEMVER = r"\d+\.\d+\.\d+"

# (file, regex whose group 1 is the text before the version and group 3 the text after).
# Every match in a file must carry the same version; `count` is how many are expected.
VERSION_FILES: list[tuple[str, str, int]] = [
    ("pyproject.toml", rf'(?m)^(version = ")({SEMVER})(")', 1),
    ("src/platformio_mcp/__init__.py", rf'(?m)^(__version__ = ")({SEMVER})(")', 1),
    ("server.json", rf'(?m)^(\s*"version": ")({SEMVER})(")', 2),
    ("plugin.json", rf'(?m)^(\s*"version": ")({SEMVER})(")', 1),
    ("lhm.plugin.json", rf'(?m)^(\s*"version": ")({SEMVER})(")', 1),
    (".claude-plugin/plugin.json", rf'(?m)^(\s*"version": ")({SEMVER})(")', 1),
    (".claude-plugin/marketplace.json", rf'(?m)^(\s*"version": ")({SEMVER})(")', 2),
    ("uv.lock", rf'(?m)^(name = "platformio-mcp"\nversion = ")({SEMVER})(")', 1),
]


def read_versions() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for name, pattern, count in VERSION_FILES:
        matches = [m.group(2) for m in re.finditer(pattern, (ROOT / name).read_text())]
        if len(matches) != count:
            raise SystemExit(f"{name}: expected {count} version string(s), found {len(matches)}")
        found[name] = matches
    return found


def changelog_sections(text: str) -> dict[str, str]:
    """Map heading ('Unreleased' or '0.2.0') to the body under it."""
    parts = re.split(r"(?m)^## \[([^\]]+)\][^\n]*\n", text)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def current_version() -> str:
    versions = {v for vs in read_versions().values() for v in vs}
    if len(versions) != 1:
        raise SystemExit(f"versions disagree: {read_versions()}")
    return versions.pop()


def cmd_check(args: argparse.Namespace) -> int:
    errors: list[str] = []
    versions = read_versions()
    distinct = {v for vs in versions.values() for v in vs}
    if len(distinct) != 1:
        errors += [f"{name}: {', '.join(vs)}" for name, vs in versions.items()]
        errors.insert(0, "version strings disagree:")
    version = sorted(distinct)[0]
    sections = changelog_sections(CHANGELOG.read_text())
    if "Unreleased" not in sections:
        errors.append("CHANGELOG.md has no '## [Unreleased]' section")
    if version not in sections:
        errors.append(f"CHANGELOG.md has no '## [{version}]' section")
    elif not sections[version]:
        errors.append(f"CHANGELOG.md section [{version}] is empty")
    if args.tag and args.tag.removeprefix("v") != version:
        errors.append(f"tag {args.tag} does not match version {version}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"ok: {version}")
    return 0


def next_version(current: str, level: str) -> str:
    if re.fullmatch(SEMVER, level):
        return level
    major, minor, patch = map(int, current.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"unknown bump '{level}': use major, minor, patch or X.Y.Z")


def cmd_bump(args: argparse.Namespace) -> int:
    old = current_version()
    new = next_version(old, args.level)
    text = CHANGELOG.read_text()
    sections = changelog_sections(text)
    if not sections.get("Unreleased"):
        raise SystemExit("CHANGELOG.md [Unreleased] is empty: nothing to release")
    if new in sections:
        raise SystemExit(f"CHANGELOG.md already has a [{new}] section")
    today = args.date or dt.date.today().isoformat()
    CHANGELOG.write_text(text.replace("## [Unreleased]\n", f"## [Unreleased]\n\n## [{new}] - {today}\n", 1))
    for name, pattern, _ in VERSION_FILES:
        path = ROOT / name
        path.write_text(re.sub(pattern, rf"\g<1>{new}\g<3>", path.read_text()))
    print(f"{old} -> {new}")
    return cmd_check(argparse.Namespace(tag=None))


def cmd_notes(args: argparse.Namespace) -> int:
    version = (args.version or current_version()).removeprefix("v")
    body = changelog_sections(CHANGELOG.read_text()).get(version)
    if not body:
        raise SystemExit(f"no changelog section for {version}")
    print(body)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--tag")
    check.set_defaults(func=cmd_check)
    bump = sub.add_parser("bump")
    bump.add_argument("level")
    bump.add_argument("--date")
    bump.set_defaults(func=cmd_bump)
    notes = sub.add_parser("notes")
    notes.add_argument("version", nargs="?")
    notes.set_defaults(func=cmd_notes)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
