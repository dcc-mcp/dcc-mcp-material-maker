import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_SOURCES = {
    "src/dcc_mcp_material_maker/__version__.py",
    "src/dcc_mcp_material_maker/skills/material-maker-materials/SKILL.md",
}


def test_release_please_tracks_every_version_source() -> None:
    config = json.loads((ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    extra_files = config["packages"]["."]["extra-files"]
    configured_paths = {entry if isinstance(entry, str) else entry["path"] for entry in extra_files}
    assert VERSION_SOURCES <= configured_paths

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert project_version is not None
    for relative_path in VERSION_SOURCES:
        lines = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
        marker_lines = [line for line in lines if "x-release-please-version" in line]
        assert len(marker_lines) == 1, relative_path
        assert project_version.group(1) in marker_lines[0], relative_path
