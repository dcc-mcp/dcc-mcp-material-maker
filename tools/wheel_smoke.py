"""Build-isolated smoke for the installed Install SOP command surface."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import venv
from pathlib import Path
from typing import Any

from dcc_mcp_core.deployment import install_sop as core_install_sop
from dcc_mcp_core.deployment import load_install_sop_schema
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = load_install_sop_schema()


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def _installed_cli(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "dcc-mcp-material-maker.exe"
    return root / "bin" / "dcc-mcp-material-maker"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    for key in (
        "DCC_MCP_MATERIAL_MAKER_EXECUTABLE",
        "DCC_MCP_MATERIAL_MAKER_VERSION",
        "DCC_MCP_MATERIAL_MAKER_PROBE_PROJECT",
        "DCC_MCP_MATERIAL_MAKER_INSTALL_ROOT",
    ):
        env.pop(key, None)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        encoding="utf-8",
        timeout=180,
    )


def _check_json(cli: Path, cwd: Path, args: list[str], expected_exit: int) -> dict[str, Any]:
    completed = _run([str(cli), *args], cwd=cwd)
    if completed.returncode != expected_exit:
        raise RuntimeError("unexpected exit %d" % completed.returncode)
    if completed.stderr:
        raise RuntimeError("Install SOP JSON invocation wrote stderr")
    report = json.loads(completed.stdout)
    Draft202012Validator(SCHEMA).validate(report)
    if report["exit_code"] != expected_exit:
        raise RuntimeError("JSON exit disagrees with process exit")
    return report


def main() -> None:
    wheels = sorted((ROOT / "dist").glob("dcc_mcp_material_maker-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("expected exactly one Material Maker wheel")
    schema_path = (
        Path(core_install_sop.__file__).resolve().parent.parent
        / "schemas"
        / "adapter-install-sop-v1.schema.json"
    )
    raw_schema = schema_path.read_bytes()
    if len(raw_schema) != 4261 or hashlib.sha256(raw_schema).hexdigest() != (
        "3ca25788439917b4d4c0617230a762f9797756b5b54f45c8c4149f975b90f904"
    ):
        raise RuntimeError("unexpected published Core Install SOP schema")

    with tempfile.TemporaryDirectory(prefix="dcc-mcp-material-maker-wheel-") as directory:
        root = Path(directory)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _venv_python(environment)
        cli = _installed_cli(environment)
        installed = _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "dcc-mcp-core==0.20.14",
                "jsonschema>=4.17,<5",
                str(wheels[0]),
            ],
            cwd=root,
        )
        if installed.returncode:
            raise RuntimeError("wheel installation failed")
        package_check = _run(
            [
                str(python),
                "-c",
                (
                    "import importlib.metadata; from pathlib import Path; "
                    "from dcc_mcp_material_maker import install; "
                    "d=importlib.metadata.distribution('dcc-mcp-material-maker'); "
                    "files={str(p).replace('\\\\','/') for p in (d.files or ())}; "
                    "assert not any(p.endswith('schemas/adapter-install-sop-v1.schema.json') "
                    "for p in files); "
                    "assert not (Path(install.__file__).resolve().parent/'schemas').exists(); "
                    "assert install.MIN_CORE_VERSION == '0.20.14'"
                ),
            ],
            cwd=root,
        )
        if package_check.returncode:
            raise RuntimeError("installed wheel carried a shim or wrong Core floor")

        missing = _check_json(
            cli,
            root,
            ["doctor", "--json", "--dcc-path", str(root / "missing")],
            10,
        )
        if missing["verify"]["failure_reason"] != "material_maker_not_found":
            raise RuntimeError("missing DCC path did not fail closed")

        invalid = _check_json(
            cli,
            root,
            ["install", "--json", "--yes", "--dry-run"],
            10,
        )
        if invalid["verify"]["failure_reason"] != "invalid_arguments":
            raise RuntimeError("conflicting flags did not produce stable JSON")

        probe = root / "probe.ptex"
        probe.write_text(
            '{"nodes":[{"name":"Material","type":"material"}],"connections":[]}',
            encoding="utf-8",
        )
        managed = root / "managed"
        plan = _check_json(
            cli,
            root,
            [
                "install",
                "--json",
                "--dry-run",
                "--install-root",
                str(managed),
                "--dcc-path",
                str(python),
                "--python",
                str(python),
                "--material-maker-version",
                "1.7.0",
                "--probe-project",
                str(probe),
            ],
            0,
        )
        if plan["status"] != "planned" or managed.exists():
            raise RuntimeError("installed-wheel dry-run was not side-effect free")

        for execution_flag in ("--yes", "--execute"):
            absent = root / ("absent-" + execution_flag[2:])
            result = _check_json(
                cli,
                root,
                [
                    "uninstall",
                    "--json",
                    execution_flag,
                    "--install-root",
                    str(absent),
                    "--python",
                    str(python),
                ],
                0,
            )
            if result["status"] != "ok" or absent.exists():
                raise RuntimeError("installed-wheel execution alias was not a safe no-op")


if __name__ == "__main__":
    main()
