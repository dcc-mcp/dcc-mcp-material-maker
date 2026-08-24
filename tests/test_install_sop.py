from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from dcc_mcp_material_maker import install, server

SCHEMA_PATH = (
    Path(install.__file__).resolve().parent / "schemas" / "adapter-install-sop-v1.schema.json"
)


def _schema() -> dict:
    value = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(value)
    return value


def _invoke(argv: list[str], capsys) -> tuple[int, dict]:
    try:
        server.main(argv)
    except SystemExit as exc:
        code = int(exc.code)
    else:
        code = 0
    report = json.loads(capsys.readouterr().out)
    Draft202012Validator(_schema()).validate(report)
    assert report["exit_code"] == code
    if report["verify"]["directly_usable"]:
        assert code == 0 and report["status"] == "ok"
    return code, report


def _project(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "name": "Probe",
                "type": "graph",
                "nodes": [{"name": "Material", "type": "material"}],
                "connections": [],
            }
        ),
        encoding="utf-8",
    )
    return path


class ReadyCli:
    def __init__(self, executable=None, allowed_roots=None):
        self.executable = executable
        self.allowed_roots = tuple(Path(item) for item in (allowed_roots or (Path.cwd(),)))

    def status(self, probe_project=None):
        assert probe_project and Path(probe_project).is_file()
        return {
            "ready": True,
            "engine": {"returncode": 0, "duration_secs": 0.01},
            "readiness_evidence": {"output_file_count": 1, "output_bytes": 3},
        }


def _install_args(root: Path, executable: Path, project: Path, *extra: str) -> list[str]:
    return [
        "install",
        "--json",
        "--install-root",
        str(root),
        "--executable",
        str(executable),
        "--material-maker-version",
        "1.7.0",
        "--probe-project",
        str(project),
        *extra,
    ]


def test_missing_executable_is_schema_valid_and_has_no_placeholder(tmp_path, capsys):
    code, report = _invoke(
        [
            "verify",
            "--json",
            "--install-root",
            str(tmp_path / "adapter"),
            "--executable",
            str(tmp_path / "missing"),
        ],
        capsys,
    )

    assert code == install.EXIT_PREFLIGHT
    assert report["dcc_type"] == "material_maker"
    assert report["verify"]["failure_stage"] == "preflight"
    encoded = json.dumps(report["next_steps"])
    assert "<PATH>" not in encoded
    assert ">=1.7" not in encoded
    for step in report["next_steps"]:
        assert ("command" in step) ^ ("file_edit" in step)


def test_unknown_version_remediation_preserves_probe_without_fabricating_a_release(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")

    code, report = _invoke(
        [
            "verify",
            "--json",
            "--install-root",
            str(root),
            "--executable",
            str(executable),
            "--probe-project",
            str(project),
        ],
        capsys,
    )

    assert code == install.EXIT_VERIFY
    command = report["next_steps"][0]["command"]
    assert command[command.index("--probe-project") + 1] == str(project.resolve())
    assert "--material-maker-version" not in command
    assert "1.7.0" not in command

    monkeypatch.setattr("builtins.input", lambda _prompt: "1.7.2")
    remediated_code, remediated = _invoke(command[1:], capsys)
    assert remediated_code == install.EXIT_OK
    assert remediated["verify"]["directly_usable"] is True
    assert remediated["host"]["version"] == "1.7.2"


def test_missing_probe_remediation_collects_exact_path_and_advances_to_readiness(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")

    code, report = _invoke(
        [
            "verify",
            "--json",
            "--install-root",
            str(root),
            "--executable",
            str(executable),
            "--material-maker-version",
            "1.7.2",
        ],
        capsys,
    )

    assert code == install.EXIT_VERIFY
    assert report["verify"]["failure_reason"] == "probe_project_required"
    command = report["next_steps"][0]["command"]
    assert command[1] == "configure"
    assert "--probe-project" not in command

    monkeypatch.setattr("builtins.input", lambda _prompt: str(project))
    remediated_code, remediated = _invoke(command[1:], capsys)
    assert remediated_code == install.EXIT_OK
    assert remediated["verify"]["directly_usable"] is True
    assert remediated["config"]["probe_project"] == str(project.resolve())


def test_bounded_configuration_reports_noninteractive_input_as_stable_json(
    monkeypatch, tmp_path, capsys
):
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")

    def end_input(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", end_input)

    code, report = _invoke(
        [
            "configure",
            "--json",
            "--install-root",
            str(tmp_path / "managed"),
            "--executable",
            str(executable),
        ],
        capsys,
    )

    assert code == install.EXIT_VERIFY
    assert report["verify"]["failure_reason"] == "operator_configuration_cancelled"
    assert report["next_steps"] == []


@pytest.mark.parametrize(
    "value",
    [
        "1.7",
        "1.7.0rc1",
        "v1.7.0",
        "1.7.0+local",
        " 1.7.0",
        "1.7.0 ",
        "01.7.0",
        "1.07.0",
        "1.7.0000000",
        "1." + ("7" * 10_000) + ".0",
    ],
)
def test_version_parser_rejects_noncanonical_or_unbounded_values(value):
    assert install._version_tuple(value) == ()


def test_install_plan_then_execute_creates_bound_receipt_with_all_owned_hashes(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")

    code, plan = _invoke(_install_args(root, executable, project), capsys)
    assert code == 0
    assert plan["status"] == "planned"
    assert not root.exists()
    assert plan["next_steps"][0]["command"][-1] == "--execute"

    code, result = _invoke(_install_args(root, executable, project, "--execute"), capsys)
    assert code == 0
    assert result["status"] == "ok"
    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert Path(receipt["install_root"]) == root.resolve()
    assert receipt["owned_files"]
    for item in receipt["owned_files"]:
        owned = root / item["path"]
        assert owned.is_file()
        assert item["bytes"] == owned.stat().st_size
        assert item["sha256"] == install._sha256_file(owned)


def test_verify_rejects_tamper_and_receipt_reuse(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")
    _invoke(_install_args(root, executable, project, "--execute"), capsys)

    config = root / "adapter.json"
    original = config.read_bytes()
    config.write_bytes(original + b"tamper")
    code, tampered = _invoke(["verify", "--json", "--install-root", str(root)], capsys)
    assert code == install.EXIT_VERIFY
    assert tampered["verify"]["failure_reason"] == "receipt_integrity_failed"

    config.write_bytes(original)
    copied = tmp_path / "copied"
    shutil.copytree(root, copied)
    code, reused = _invoke(["status", "--json", "--install-root", str(copied)], capsys)
    assert code == install.EXIT_VERIFY
    assert reused["verify"]["failure_reason"] == "receipt_root_mismatch"


def test_upgrade_rolls_back_when_atomic_publish_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")
    _invoke(_install_args(root, executable, project, "--execute"), capsys)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }

    real_replace = os.replace

    def fail_staging_publish(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if ".staging-" in source_path.name and destination_path == root:
            raise OSError("injected publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(install.os, "replace", fail_staging_publish)
    argv = _install_args(root, executable, project, "--execute")
    argv[0] = "upgrade"
    argv[argv.index("--material-maker-version") + 1] = "1.8.0"
    code, report = _invoke(argv, capsys)
    assert code == install.EXIT_INSTALL
    assert report["verify"]["failure_reason"] == "transaction_publish_failed"
    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before


def test_upgrade_rolls_back_when_new_state_fails_host_verification(monkeypatch, tmp_path, capsys):
    class FailSecondProbeCli(ReadyCli):
        probes = 0

        def status(self, probe_project=None):
            type(self).probes += 1
            if type(self).probes == 2:
                raise install.MaterialMakerError("injected upgraded host probe failure")
            return super().status(probe_project=probe_project)

    monkeypatch.setattr(install, "MaterialMakerCli", FailSecondProbeCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")
    _invoke(_install_args(root, executable, project, "--execute"), capsys)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }

    argv = _install_args(root, executable, project, "--execute")
    argv[0] = "upgrade"
    argv[argv.index("--material-maker-version") + 1] = "1.8.0"
    code, report = _invoke(argv, capsys)

    assert code == install.EXIT_VERIFY
    assert report["verify"]["failure_reason"] == "native_probe_failed"
    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before


def test_uninstall_is_plan_first_and_removes_only_verified_managed_root(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")
    _invoke(_install_args(root, executable, project, "--execute"), capsys)

    code, plan = _invoke(["uninstall", "--json", "--install-root", str(root)], capsys)
    assert code == 0
    assert plan["status"] == "planned"
    assert root.exists()
    assert plan["next_steps"][0]["command"][-1] == "--execute"

    code, result = _invoke(
        ["uninstall", "--json", "--install-root", str(root), "--execute"], capsys
    )
    assert code == 0
    assert result["receipt_path"] is None
    assert not root.exists()
    assert not list(tmp_path.glob(".managed.uninstall-*"))
    assert not list(tmp_path.glob(".managed.uninstall-backup-*"))


def test_second_uninstall_is_a_successful_noop(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")
    _invoke(_install_args(root, executable, project, "--execute"), capsys)
    _invoke(["uninstall", "--json", "--install-root", str(root), "--execute"], capsys)

    code, report = _invoke(
        ["uninstall", "--json", "--install-root", str(root), "--execute"], capsys
    )

    assert code == install.EXIT_OK
    assert report["status"] == "ok"
    assert report["receipt_path"] is None
    assert report["steps"][-1] == {
        "id": "uninstall",
        "status": "already_absent",
        "message": "Managed state is already absent.",
    }


def test_failed_uninstall_cleanup_restores_from_an_intact_backup(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")
    _invoke(_install_args(root, executable, project, "--execute"), capsys)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    real_rmtree = shutil.rmtree

    def partially_remove_then_fail(path, *args, **kwargs):
        candidate = Path(path)
        if ".uninstall-" in candidate.name:
            (candidate / "adapter.json").unlink()
            raise OSError("injected partial quarantine deletion")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(install.shutil, "rmtree", partially_remove_then_fail)
    code, report = _invoke(
        ["uninstall", "--json", "--install-root", str(root), "--execute"], capsys
    )

    assert code == install.EXIT_INSTALL
    assert report["verify"]["failure_reason"] == "transaction_uninstall_failed"
    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before


def test_repeated_install_reuses_identical_verified_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")
    args = _install_args(root, executable, project, "--execute")
    _invoke(args, capsys)
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }

    code, report = _invoke(args, capsys)

    assert code == 0
    assert report["verify"]["directly_usable"] is True
    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before


def test_uninstall_rejects_unowned_file_without_removing_anything(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")
    _invoke(_install_args(root, executable, project, "--execute"), capsys)
    unrelated = root / "user.txt"
    unrelated.write_text("keep", encoding="utf-8")

    code, report = _invoke(
        ["uninstall", "--json", "--install-root", str(root), "--execute"], capsys
    )

    assert code == install.EXIT_INSTALL
    assert report["verify"]["failure_reason"] == "receipt_integrity_failed"
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_uninstall_rejects_unowned_empty_directory_without_removing_anything(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(install, "MaterialMakerCli", ReadyCli)
    root = tmp_path / "managed"
    executable = tmp_path / "material_maker"
    executable.write_bytes(b"host")
    project = _project(tmp_path / "probe.ptex")
    _invoke(_install_args(root, executable, project, "--execute"), capsys)
    unrelated = root / "user-empty-directory"
    unrelated.mkdir()

    code, report = _invoke(
        ["uninstall", "--json", "--install-root", str(root), "--execute"], capsys
    )

    assert code == install.EXIT_INSTALL
    assert report["verify"]["failure_reason"] == "receipt_integrity_failed"
    assert unrelated.is_dir()
    assert root.is_dir()


def test_schema_is_packaged_and_matches_core_compatibility_contract():
    schema = _schema()
    assert schema["$id"] == "https://dcc-mcp.github.io/schemas/adapter-install-sop-v1.schema.json"
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "src/dcc_mcp_material_maker/schemas/**" in pyproject
    assert install._sha256_file(SCHEMA_PATH) == (
        "3ca25788439917b4d4c0617230a762f9797756b5b54f45c8c4149f975b90f904"
    )
    assert SCHEMA_PATH.stat().st_size == 4261


def test_status_skill_requires_a_material_maker_specific_probe_project():
    tools_yaml = (
        Path(install.__file__).resolve().parent
        / "skills"
        / "material-maker-materials"
        / "tools.yaml"
    ).read_text(encoding="utf-8")
    assert "required: [probe_project]" in tools_yaml
    assert "Run a no-input native CLI readiness probe" not in tools_yaml
