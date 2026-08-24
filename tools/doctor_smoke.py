"""Exercise the installed JSON diagnostic CLI and its stable process exits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "dcc_mcp_material_maker"
    / "schemas"
    / "adapter-install-sop-v1.schema.json"
)
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _check(args: list[str], expected_exit: int) -> None:
    env = os.environ.copy()
    env.pop("DCC_MCP_MATERIAL_MAKER_EXECUTABLE", None)
    env.pop("DCC_MCP_MATERIAL_MAKER_VERSION", None)
    completed = subprocess.run(
        [sys.executable, "-m", "dcc_mcp_material_maker.install", *args],
        capture_output=True,
        check=False,
        encoding="utf-8",
        env=env,
    )
    if completed.returncode != expected_exit:
        raise RuntimeError(
            "diagnostic exit %d != %d: %s"
            % (completed.returncode, expected_exit, completed.stderr[:512])
        )
    report = json.loads(completed.stdout)
    Draft202012Validator(SCHEMA).validate(report)
    if report["schema_version"] != 1 or report["exit_code"] != expected_exit:
        raise RuntimeError("diagnostic JSON and process exit disagree")
    if report["directly_usable"] is not (expected_exit == 0):
        raise RuntimeError("directly_usable does not match the stable exit")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="dcc-mcp-material-maker-doctor-") as directory:
        root = Path(directory)
        _check(
            ["doctor", "--json", "--executable", str(root / "missing")],
            10,
        )
        _check(
            [
                "verify",
                "--json",
                "--executable",
                sys.executable,
                "--material-maker-version",
                "1.6.0",
            ],
            40,
        )
        if os.name == "posix":
            fake_host = root / "material_maker"
            fake_host.write_text(
                "#!/bin/sh\n"
                'while [ "$#" -gt 0 ]; do\n'
                '  if [ "$1" = "--output-dir" ]; then shift; output=$1; fi\n'
                "  shift\n"
                "done\n"
                'mkdir -p "$output"\n'
                'printf png > "$output/probe.png"\n',
                encoding="utf-8",
            )
            fake_host.chmod(0o700)
            probe_project = root / "probe.ptex"
            probe_project.write_text(
                '{"nodes":[{"name":"Material","type":"material"}],"connections":[]}',
                encoding="utf-8",
            )
            _check(
                [
                    "verify",
                    "--json",
                    "--executable",
                    str(fake_host),
                    "--material-maker-version",
                    "1.7.0",
                    "--probe-project",
                    str(probe_project),
                ],
                0,
            )


if __name__ == "__main__":
    main()
