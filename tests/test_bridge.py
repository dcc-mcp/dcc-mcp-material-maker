from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dcc_mcp_material_maker.bridge import MaterialMakerCli, MaterialMakerError


def project_data() -> dict:
    return {
        "name": "Bricks",
        "label": "Bricks",
        "type": "graph",
        "nodes": [
            {"name": "Noise", "type": "perlin", "parameters": {}},
            {"name": "Material", "type": "material", "parameters": {}},
        ],
        "connections": [{"from": "Noise", "from_port": 0, "to": "Material", "to_port": 0}],
        "parameters": {},
    }


def write_project(path: Path, data=None) -> Path:
    path.write_text(json.dumps(data or project_data()), encoding="utf-8")
    return path


class FakeMaterialMakerCli(MaterialMakerCli):
    def __init__(self, root: Path, *, failure: bool = False, file_count: int = 2) -> None:
        super().__init__(executable=sys.executable, allowed_roots=[root])
        self.failure = failure
        self.file_count = file_count
        self.calls = []

    def _run(self, args, timeout_secs):
        self.calls.append((tuple(args), timeout_secs))
        if "--output-dir" in args and any(str(item).endswith(".ptex") for item in args):
            output = Path(args[args.index("--output-dir") + 1])
            for index in range(self.file_count):
                (output / ("material_%d.png" % index)).write_bytes(b"png" + bytes([index]))
        return {
            "returncode": 0,
            "duration_secs": 0.01,
            "stdout": "ERROR: rejected" if self.failure else "Exporting...\nDone\n",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }


def test_status_reports_missing_executable(tmp_path):
    cli = MaterialMakerCli(executable=str(tmp_path / "missing"), allowed_roots=[tmp_path])
    status = cli.status()
    assert status["ready"] is False
    assert status["reason"] == "material_maker_not_found"
    assert status["arbitrary_script_input"] is False


def test_status_uses_bounded_native_probe(tmp_path):
    cli = FakeMaterialMakerCli(tmp_path)
    project = write_project(tmp_path / "probe.ptex")
    status = cli.status(probe_project=str(project))
    assert status["ready"] is True
    assert status["driver"] == "official_material_maker_cli"
    assert cli.calls[0][0][0] == "--export-material"
    assert status["readiness_evidence"]["output_file_count"] == 2


def test_status_rejects_exit_zero_without_material_maker_artifacts(tmp_path):
    project = write_project(tmp_path / "probe.ptex")
    cli = FakeMaterialMakerCli(tmp_path, file_count=0)
    with pytest.raises(MaterialMakerError, match="no export files"):
        cli.status(probe_project=str(project))


def test_status_without_probe_project_fails_closed(tmp_path):
    cli = FakeMaterialMakerCli(tmp_path)
    status = cli.status()
    assert status["ready"] is False
    assert status["reason"] == "probe_project_required"
    assert not cli.calls


def test_inspect_and_validate_project(tmp_path):
    project = write_project(tmp_path / "bricks.ptex")
    cli = MaterialMakerCli(executable=sys.executable, allowed_roots=[tmp_path])
    inspected = cli.inspect_project(str(project))
    assert inspected["valid"] is True
    assert inspected["node_count"] == 2
    assert inspected["connection_count"] == 1
    assert inspected["material_node_count"] == 1
    assert inspected["node_types"] == {"material": 1, "perlin": 1}
    assert len(inspected["sha256"]) == 64


def test_validate_rejects_unknown_connection_endpoint(tmp_path):
    data = project_data()
    data["connections"][0]["to"] = "Missing"
    project = write_project(tmp_path / "invalid.ptex", data)
    cli = MaterialMakerCli(executable=sys.executable, allowed_roots=[tmp_path])
    result = cli.validate_project(str(project))
    assert result["valid"] is False
    assert any("unknown to node" in error for error in result["errors"])


def test_validate_rejects_duplicate_node_names(tmp_path):
    data = project_data()
    data["nodes"][1]["name"] = "Noise"
    project = write_project(tmp_path / "duplicate.ptex", data)
    cli = MaterialMakerCli(executable=sys.executable, allowed_roots=[tmp_path])
    result = cli.validate_project(str(project))
    assert result["valid"] is False
    assert any("duplicate node name" in error for error in result["errors"])


def test_inspect_rejects_malformed_json(tmp_path):
    project = tmp_path / "malformed.ptex"
    project.write_text("{", encoding="utf-8")
    cli = MaterialMakerCli(executable=sys.executable, allowed_roots=[tmp_path])
    with pytest.raises(MaterialMakerError, match="valid UTF-8 JSON"):
        cli.inspect_project(str(project))


def test_validate_counts_nested_graphs(tmp_path):
    data = project_data()
    data["nodes"].append(
        {
            "name": "Nested",
            "type": "graph",
            "nodes": [{"name": "Inner", "type": "uniform"}],
            "connections": [],
        }
    )
    project = write_project(tmp_path / "nested.ptex", data)
    cli = MaterialMakerCli(executable=sys.executable, allowed_roots=[tmp_path])
    result = cli.validate_project(str(project))
    assert result["valid"] is True
    assert result["graph_count"] == 2
    assert result["node_count"] == 4


def test_project_must_stay_inside_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    project = write_project(tmp_path / "outside.ptex")
    cli = MaterialMakerCli(executable=sys.executable, allowed_roots=[allowed])
    with pytest.raises(MaterialMakerError, match="outside"):
        cli.inspect_project(str(project))


def test_project_size_limit_is_enforced(tmp_path):
    project = write_project(tmp_path / "large.ptex")
    cli = MaterialMakerCli(
        executable=sys.executable,
        allowed_roots=[tmp_path],
        max_project_bytes=1,
    )
    with pytest.raises(MaterialMakerError, match="size limit"):
        cli.inspect_project(str(project))


def test_export_material_stages_and_moves_a_new_directory(tmp_path):
    project = write_project(tmp_path / "bricks.ptex")
    destination = tmp_path / "export"
    cli = FakeMaterialMakerCli(tmp_path)
    result = cli.export_material(str(project), str(destination), target="Godot")
    assert destination.is_dir()
    assert result["file_count"] == 2
    assert result["total_bytes"] == 8
    assert {item["path"] for item in result["files"]} == {
        "material_0.png",
        "material_1.png",
    }
    assert all(len(item["sha256"]) == 64 for item in result["files"])


def test_export_rejects_existing_output_directory(tmp_path):
    project = write_project(tmp_path / "bricks.ptex")
    destination = tmp_path / "export"
    destination.mkdir()
    cli = FakeMaterialMakerCli(tmp_path)
    with pytest.raises(MaterialMakerError, match="already exists"):
        cli.export_material(str(project), str(destination))


def test_export_destination_must_stay_inside_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    project = write_project(allowed / "bricks.ptex")
    cli = FakeMaterialMakerCli(allowed)
    with pytest.raises(MaterialMakerError, match="outside"):
        cli.export_material(str(project), str(outside / "export"))


def test_export_rejects_undocumented_target(tmp_path):
    project = write_project(tmp_path / "bricks.ptex")
    cli = FakeMaterialMakerCli(tmp_path)
    with pytest.raises(MaterialMakerError, match="target must be one of"):
        cli.export_material(str(project), str(tmp_path / "export"), target="Shell")


def test_export_rejects_unsafe_output_template(tmp_path):
    project = write_project(tmp_path / "bricks.ptex")
    cli = FakeMaterialMakerCli(tmp_path)
    with pytest.raises(MaterialMakerError, match="safe filename"):
        cli.export_material(
            str(project),
            str(tmp_path / "export"),
            output_file="../outside",
        )


def test_export_failure_does_not_publish_staging_directory(tmp_path):
    project = write_project(tmp_path / "bricks.ptex")
    destination = tmp_path / "export"
    cli = FakeMaterialMakerCli(tmp_path, failure=True)
    with pytest.raises(MaterialMakerError, match="export failed"):
        cli.export_material(str(project), str(destination))
    assert not destination.exists()
    assert not list(tmp_path.glob(".export.*"))


def test_export_file_count_limit_is_enforced(tmp_path):
    project = write_project(tmp_path / "bricks.ptex")
    destination = tmp_path / "export"
    cli = FakeMaterialMakerCli(tmp_path, file_count=3)
    cli.max_export_files = 2
    with pytest.raises(MaterialMakerError, match="file-count"):
        cli.export_material(str(project), str(destination))
    assert not destination.exists()


def test_export_byte_limit_is_enforced(tmp_path):
    project = write_project(tmp_path / "bricks.ptex")
    destination = tmp_path / "export"
    cli = FakeMaterialMakerCli(tmp_path)
    cli.max_export_bytes = 4
    with pytest.raises(MaterialMakerError, match="byte limit"):
        cli.export_material(str(project), str(destination))
    assert not destination.exists()


def test_timeout_must_stay_inside_configured_bound(tmp_path):
    cli = FakeMaterialMakerCli(tmp_path)
    cli.max_timeout_secs = 10
    with pytest.raises(MaterialMakerError, match="no more than 10"):
        cli._timeout(11)
