"""Read-only installation diagnostics for the Material Maker adapter."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Optional, Sequence

from .__version__ import __version__
from .bridge import MaterialMakerCli, MaterialMakerError

EXIT_PREFLIGHT = 10
EXIT_VERIFY = 40
MIN_CORE_VERSION = "0.19.38"
MIN_MATERIAL_MAKER_VERSION = "1.7"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "verify"), nargs="?", default="doctor")
    parser.add_argument("--json", action="store_true", help="Emit the stable JSON contract")
    parser.add_argument("--executable", help="Exact path to the official Material Maker executable")
    parser.add_argument(
        "--material-maker-version",
        help=(
            "Installed Material Maker release version (the native CLI has no product-version verb)"
        ),
    )
    return parser


def _program_name(program: Optional[str]) -> str:
    return program or Path(sys.argv[0]).name or "dcc-mcp-material-maker-doctor"


def _invocation(program: Optional[str]) -> dict[str, object]:
    name = _program_name(program)
    if name.endswith("-install"):
        return {
            "name": name,
            "deprecated": True,
            "replacement": ["dcc-mcp-material-maker", "doctor", "--json"],
        }
    return {"name": name, "deprecated": False}


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value.strip())
    if not match:
        return ()
    return tuple(int(item) for item in match.groups(default="0"))


def _configured_version(args: argparse.Namespace) -> tuple[str, str]:
    if args.material_maker_version:
        return args.material_maker_version, "command_line"
    value = os.environ.get("DCC_MCP_MATERIAL_MAKER_VERSION", "")
    return value, "environment" if value else "unavailable"


def _endpoint_source(args: argparse.Namespace, cli: MaterialMakerCli) -> str:
    if args.executable:
        return "command_line"
    if os.environ.get("DCC_MCP_MATERIAL_MAKER_EXECUTABLE"):
        return "environment"
    return "auto_discovery" if cli.executable else "unavailable"


def _base_report(
    args: argparse.Namespace,
    cli: MaterialMakerCli,
    program: Optional[str],
) -> dict[str, Any]:
    core_version = metadata.version("dcc-mcp-core")
    core_supported = _version_tuple(core_version) >= _version_tuple(MIN_CORE_VERSION)
    host_version, version_source = _configured_version(args)
    host_supported: Optional[bool]
    if not host_version:
        host_supported = None
    else:
        host_supported = _version_tuple(host_version) >= _version_tuple(MIN_MATERIAL_MAKER_VERSION)
    return {
        "schema_version": 1,
        "command": args.command,
        "status": "failed",
        "directly_usable": False,
        "exit_code": EXIT_VERIFY,
        "adapter": {
            "name": "dcc-mcp-material-maker",
            "version": __version__,
        },
        "runtime": {
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "platform": sys.platform,
            "machine": platform.machine(),
        },
        "core": {
            "version": core_version,
            "minimum_version": MIN_CORE_VERSION,
            "satisfies_minimum": core_supported,
        },
        "endpoint": {
            "kind": "native_cli",
            "value": cli.executable,
            "source": _endpoint_source(args, cli),
        },
        "config": {
            "requested_executable": args.executable,
            "allowed_roots": [str(root) for root in cli.allowed_roots],
            "version_variable": "DCC_MCP_MATERIAL_MAKER_VERSION",
            "executable_variable": "DCC_MCP_MATERIAL_MAKER_EXECUTABLE",
        },
        "host": {
            "name": "Material Maker",
            "version": host_version or None,
            "version_source": version_source,
            "minimum_version": MIN_MATERIAL_MAKER_VERSION,
            "satisfies_minimum": host_supported,
        },
        "probe": None,
        "failure": None,
        "next_steps": [],
        "invocation": _invocation(program),
        "side_effects": {
            "installs": False,
            "persistent_writes": False,
        },
    }


def _emit(report: dict[str, Any], exit_code: int) -> None:
    report["exit_code"] = exit_code
    report["status"] = "ok" if exit_code == 0 else "failed"
    report["directly_usable"] = exit_code == 0
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def main(argv: Optional[Sequence[str]] = None, *, program: Optional[str] = None) -> None:
    """Print a machine-readable runtime check; no installation is performed."""
    args = _parser().parse_args(list(argv) if argv is not None else None)
    cli = (
        MaterialMakerCli(executable=args.executable)
        if args.executable
        else MaterialMakerCli.from_env()
    )
    report = _base_report(args, cli, program)

    if not cli.executable:
        report["failure"] = {
            "stage": "preflight",
            "reason": "material_maker_not_found",
        }
        report["next_steps"] = [
            {
                "id": "configure_material_maker_executable",
                "description": "Provide the exact official Material Maker executable path.",
                "why": "The adapter does not download or install external binaries.",
                "command": [
                    "dcc-mcp-material-maker",
                    args.command,
                    "--json",
                    "--executable",
                    "<PATH>",
                ],
            }
        ]
        _emit(report, EXIT_PREFLIGHT)

    if not report["core"]["satisfies_minimum"]:
        report["failure"] = {
            "stage": "preflight",
            "reason": "core_version_unsupported",
        }
        report["next_steps"] = [
            {
                "id": "upgrade_dcc_mcp_core",
                "description": "Install a supported dcc-mcp-core release.",
                "why": "This adapter requires dcc-mcp-core 0.19.38 or newer.",
                "command": [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "dcc-mcp-core>=0.19.38,<1.0.0",
                ],
            }
        ]
        _emit(report, EXIT_PREFLIGHT)

    host_version = report["host"]["version"]
    if report["host"]["satisfies_minimum"] is not True:
        version_known = bool(host_version)
        report["failure"] = {
            "stage": "verify",
            "reason": (
                "material_maker_version_unsupported"
                if version_known
                else "material_maker_version_unknown"
            ),
        }
        report["next_steps"] = [
            {
                "id": "select_supported_material_maker",
                "description": "Use Material Maker 1.7 or newer and report its release version.",
                "why": "The bounded export contract requires Material Maker 1.7 or newer.",
                "command": [
                    "dcc-mcp-material-maker",
                    args.command,
                    "--json",
                    "--executable",
                    cli.executable,
                    "--material-maker-version",
                    ">=1.7",
                ],
            }
        ]
        _emit(report, EXIT_VERIFY)

    try:
        status = cli.status()
    except MaterialMakerError as exc:
        report["failure"] = {
            "stage": "verify",
            "reason": "native_probe_failed",
            "detail": str(exc)[:512],
        }
        report["next_steps"] = [
            {
                "id": "retry_native_probe",
                "description": "Re-run the bounded native readiness probe after diagnosis.",
                "why": "The executable exists and meets the version floor but rejected the probe.",
                "command": [
                    "dcc-mcp-material-maker",
                    args.command,
                    "--json",
                    "--executable",
                    cli.executable,
                    "--material-maker-version",
                    host_version,
                ],
            }
        ]
        _emit(report, EXIT_VERIFY)
    if not status.get("ready"):
        report["failure"] = {
            "stage": "verify",
            "reason": status.get("reason", "native_probe_failed"),
        }
        _emit(report, EXIT_VERIFY)

    report["probe"] = status.get("engine")
    _emit(report, 0)


if __name__ == "__main__":
    main()
