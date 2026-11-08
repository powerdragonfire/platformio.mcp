"""scripts/release.py: version sync, bump arithmetic, changelog parsing."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import platformio_mcp

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release.py"
spec = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize(
    ("current", "level", "expected"),
    [
        ("0.2.0", "patch", "0.2.1"),
        ("0.2.5", "minor", "0.3.0"),
        ("0.9.9", "major", "1.0.0"),
        ("0.2.0", "1.4.2", "1.4.2"),
    ],
)
def test_next_version(current, level, expected):
    assert release.next_version(current, level) == expected


def test_next_version_rejects_unknown_level():
    with pytest.raises(SystemExit):
        release.next_version("0.2.0", "huge")


def test_changelog_sections_split_by_heading():
    text = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-09-18\n\n### Added\n- x\n\n## [0.1.0] - 2026-09-16\n\nFirst.\n"
    sections = release.changelog_sections(text)
    assert sections == {"Unreleased": "", "0.2.0": "### Added\n- x", "0.1.0": "First."}


def test_repo_versions_are_in_sync():
    versions = {v for vs in release.read_versions().values() for v in vs}
    assert versions == {platformio_mcp.__version__}


def test_check_command_passes_on_this_repo():
    result = subprocess.run([sys.executable, str(SCRIPT), "check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_check_rejects_mismatched_tag():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check", "--tag", "v99.0.0"], capture_output=True, text=True
    )
    assert result.returncode == 1 and "does not match" in result.stderr
