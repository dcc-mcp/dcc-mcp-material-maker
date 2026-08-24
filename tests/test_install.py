from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dcc_mcp_material_maker import install, server
from dcc_mcp_material_maker.bridge import MaterialMakerError

ROOT = Path(__file__).resolve().parents[1]


def test_standard_doctor_json_reports_missing_executable_as_preflight_failure(tmp_path, capsys):
    missing = tmp_path / "material_maker"

    with pytest.raises(SystemExit) as raised:
        server.main(["doctor", "--json", "--executable", str(missing)])

    assert raised.value.code == 10
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["command"] == "doctor"
    assert report["exit_code"] == 10
    assert report["directly_usable"] is False
    assert report["failure"] == {
        "stage": "preflight",
        "reason": "material_maker_not_found",
    }
    assert report["next_steps"]
    assert isinstance(report["next_steps"][0]["command"], list)
    assert report["endpoint"] == {
        "kind": "native_cli",
        "value": None,
        "source": "command_line",
    }
    assert report["config"]["requested_executable"] == str(missing)
    assert report["runtime"]["python_executable"] == sys.executable
    assert report["core"]["minimum_version"] == "0.19.38"
    assert report["host"]["minimum_version"] == "1.7.0"


def test_verify_enforces_material_maker_1_7_floor(capsys):
    with pytest.raises(SystemExit) as raised:
        server.main(
            [
                "verify",
                "--json",
                "--executable",
                sys.executable,
                "--material-maker-version",
                "1.6.0",
            ]
        )

    assert raised.value.code == 40
    report = json.loads(capsys.readouterr().out)
    assert report["host"] == {
        "name": "Material Maker",
        "version": "1.6.0",
        "version_source": "command_line",
        "minimum_version": "1.7.0",
        "satisfies_minimum": False,
    }
    assert report["failure"] == {
        "stage": "verify",
        "reason": "material_maker_version_unsupported",
    }
    for field in ("adapter", "runtime", "core", "endpoint", "config", "side_effects"):
        assert field in report


def test_verify_success_reports_runtime_core_endpoint_and_config(monkeypatch, tmp_path, capsys):
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"fixture")
    probe_project = tmp_path / "probe.ptex"
    probe_project.write_text('{"nodes": [], "connections": []}', encoding="utf-8")

    class ReadyMaterialMakerCli:
        def __init__(self, executable=None, allowed_roots=None):
            self.executable = executable
            self.allowed_roots = tuple(allowed_roots or (tmp_path,))

        def status(self, probe_project=None):
            assert probe_project
            return {
                "ready": True,
                "engine": {"returncode": 0, "duration_secs": 0.01},
                "readiness_evidence": {"output_file_count": 1, "output_bytes": 1},
            }

    monkeypatch.setattr(install, "MaterialMakerCli", ReadyMaterialMakerCli)

    server.main(
        [
            "verify",
            "--json",
            "--executable",
            str(executable),
            "--material-maker-version",
            "1.7.2",
            "--probe-project",
            str(probe_project),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["directly_usable"] is True
    assert report["exit_code"] == 0
    assert report["failure"] is None
    assert report["next_steps"] == []
    assert report["host"]["satisfies_minimum"] is True
    assert report["core"]["minimum_version"] == "0.19.38"
    assert report["core"]["satisfies_minimum"] is True
    assert report["endpoint"] == {
        "kind": "native_cli",
        "value": str(executable),
        "source": "command_line",
    }
    assert report["config"]["allowed_roots"] == [str(tmp_path)]
    assert report["runtime"]["python_executable"] == sys.executable


def test_doctor_rejects_unsupported_core_before_claiming_usable(monkeypatch, tmp_path, capsys):
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"fixture")

    class ReadyMaterialMakerCli:
        def __init__(self, executable=None, allowed_roots=None):
            self.executable = executable
            self.allowed_roots = tuple(allowed_roots or (tmp_path,))

        def status(self, probe_project=None):
            return {"ready": True, "engine": {"returncode": 0}}

    monkeypatch.setattr(install, "MaterialMakerCli", ReadyMaterialMakerCli)
    monkeypatch.setattr(install.metadata, "version", lambda _name: "0.19.37")

    with pytest.raises(SystemExit) as raised:
        server.main(
            [
                "doctor",
                "--json",
                "--executable",
                str(executable),
                "--material-maker-version",
                "1.7.0",
            ]
        )

    assert raised.value.code == 10
    report = json.loads(capsys.readouterr().out)
    assert report["directly_usable"] is False
    assert report["core"]["satisfies_minimum"] is False
    assert report["failure"] == {
        "stage": "preflight",
        "reason": "core_version_unsupported",
    }


def test_verify_requires_explicit_product_version_when_native_cli_cannot_report_it(
    monkeypatch, tmp_path, capsys
):
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"fixture")

    class UnversionedMaterialMakerCli:
        def __init__(self, executable=None, allowed_roots=None):
            self.executable = executable
            self.allowed_roots = tuple(allowed_roots or (tmp_path,))

        def status(self, probe_project=None):
            raise AssertionError("version floor must be checked before the native readiness probe")

    monkeypatch.setattr(install, "MaterialMakerCli", UnversionedMaterialMakerCli)
    monkeypatch.delenv("DCC_MCP_MATERIAL_MAKER_VERSION", raising=False)

    with pytest.raises(SystemExit) as raised:
        server.main(["verify", "--json", "--executable", str(executable)])

    assert raised.value.code == 40
    report = json.loads(capsys.readouterr().out)
    assert report["host"]["version"] is None
    assert report["host"]["version_source"] == "unavailable"
    assert report["host"]["satisfies_minimum"] is None
    assert report["failure"] == {
        "stage": "verify",
        "reason": "material_maker_version_unknown",
    }
    assert "--material-maker-version" not in report["next_steps"][0]["command"]
    assert "1.7.0" not in report["next_steps"][0]["command"]


def test_verify_converts_native_probe_error_to_stable_verify_failure(monkeypatch, tmp_path, capsys):
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"fixture")
    probe_project = tmp_path / "probe.ptex"
    probe_project.write_text('{"nodes": [], "connections": []}', encoding="utf-8")

    class FailingMaterialMakerCli:
        def __init__(self, executable=None, allowed_roots=None):
            self.executable = executable
            self.allowed_roots = tuple(allowed_roots or (tmp_path,))

        def status(self, probe_project=None):
            raise MaterialMakerError("Material Maker CLI readiness probe failed")

    monkeypatch.setattr(install, "MaterialMakerCli", FailingMaterialMakerCli)

    with pytest.raises(SystemExit) as raised:
        server.main(
            [
                "verify",
                "--json",
                "--executable",
                str(executable),
                "--material-maker-version",
                "1.7.0",
                "--probe-project",
                str(probe_project),
            ]
        )

    assert raised.value.code == 40
    report = json.loads(capsys.readouterr().out)
    assert report["directly_usable"] is False
    assert report["failure"] == {
        "stage": "verify",
        "reason": "native_probe_failed",
        "detail": "MaterialMakerError",
    }


def test_legacy_install_alias_is_deprecated_read_only_doctor(monkeypatch, tmp_path, capsys):
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"fixture")
    probe_project = tmp_path / "probe.ptex"
    probe_project.write_text('{"nodes": [], "connections": []}', encoding="utf-8")

    class ReadyMaterialMakerCli:
        def __init__(self, executable=None, allowed_roots=None):
            self.executable = executable
            self.allowed_roots = tuple(allowed_roots or (tmp_path,))

        def status(self, probe_project=None):
            return {"ready": True, "engine": {"returncode": 0}}

    monkeypatch.setattr(install, "MaterialMakerCli", ReadyMaterialMakerCli)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dcc-mcp-material-maker-install",
            "--json",
            "--executable",
            str(executable),
            "--material-maker-version",
            "1.7.0",
            "--probe-project",
            str(probe_project),
        ],
    )

    install.main()

    report = json.loads(capsys.readouterr().out)
    assert report["command"] == "doctor"
    assert report["invocation"] == {
        "name": "dcc-mcp-material-maker-install",
        "deprecated": True,
        "replacement": ["dcc-mcp-material-maker", "doctor", "--json"],
    }
    assert report["side_effects"] == {
        "installs": False,
        "persistent_writes": False,
    }


def test_install_runbook_teaches_wheel_lifecycle_without_claiming_unpublished_pypi():
    runbook = (ROOT / "install.md").read_text(encoding="utf-8")

    for heading in (
        "## Requirements",
        "## Supported versions",
        "## Agent quick path",
        "## Manual path",
        "## Verify",
        "## Upgrade",
        "## Uninstall",
        "## Troubleshooting",
    ):
        assert heading in runbook

    assert "Windows" in runbook
    assert "macOS" in runbook
    assert "Linux" in runbook
    assert "dcc_mcp_material_maker-<version>-py3-none-any.whl" in runbook
    assert "not currently published to PyPI" in runbook
    assert "does not download or cache Material Maker" in runbook
    assert (
        "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-material-maker/main/install.md"
        in runbook
    )


def test_readme_routes_installation_to_runbook_without_dead_pypi_command():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "[Installation](install.md)" in readme
    assert "pip install dcc-mcp-material-maker" not in readme


def test_ci_executes_installed_doctor_json_exit_contract():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python tools/doctor_smoke.py" in workflow


def test_source_distribution_includes_install_runbook():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"install.md"' in pyproject
