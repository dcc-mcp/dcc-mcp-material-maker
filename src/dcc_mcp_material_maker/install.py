"""Agent-first Install SOP v1 lifecycle for the Material Maker adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import uuid
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence

from .__version__ import __version__
from .bridge import MaterialMakerCli, MaterialMakerError

EXIT_OK = 0
EXIT_PREFLIGHT = 10
EXIT_ACQUIRE = 20
EXIT_INSTALL = 30
EXIT_VERIFY = 40
EXIT_REQUIRES_RESTART = 50
MIN_CORE_VERSION = "0.20.14"
MIN_MATERIAL_MAKER_VERSION = "1.7.0"

_VERSION_COMPONENT = r"(?:0|[1-9][0-9]{0,5})"
_FINAL_RELEASE = re.compile(
    r"^(%s)\.(%s)\.(%s)$" % (_VERSION_COMPONENT, _VERSION_COMPONENT, _VERSION_COMPONENT)
)
_MAX_VERSION_LENGTH = 32
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_NAME = "adapter.json"
_RECEIPT_RELATIVE = Path(".dcc-mcp") / "receipts" / "material-maker.json"


class InstallStateError(RuntimeError):
    """Managed install state is absent, untrusted, or inconsistent."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class _ArgumentError(RuntimeError):
    """An invocation failed before the stable Install SOP report existed."""


class _SopArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _ArgumentError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SopArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "install",
            "status",
            "verify",
            "uninstall",
            "upgrade",
            "doctor",
            "configure",
        ),
        nargs="?",
        default="doctor",
    )
    parser.add_argument("--json", action="store_true", help="Emit the Install SOP v1 JSON result")
    parser.add_argument("--execute", action="store_true", help="Apply a planned mutating operation")
    parser.add_argument("--yes", action="store_true", help="Apply a reviewed mutating plan")
    parser.add_argument(
        "--dry-run", action="store_true", help="Emit a plan without persistent writes"
    )
    parser.add_argument("--install-root", help="Dedicated adapter-managed state directory")
    parser.add_argument("--executable", help="Exact path to the official Material Maker executable")
    parser.add_argument("--dcc-path", help="Standard alias for the Material Maker executable")
    parser.add_argument("--python", help="Exact Python interpreter that owns this adapter wheel")
    parser.add_argument(
        "--material-maker-version",
        help="Trusted final Material Maker release in canonical X.Y.Z form",
    )
    parser.add_argument(
        "--probe-project",
        help="Existing bounded .ptex project used only for a transient readiness export",
    )
    return parser


def _fallback_args(values: Sequence[str]) -> argparse.Namespace:
    commands = {"install", "status", "verify", "uninstall", "upgrade", "doctor", "configure"}
    command = values[0] if values and values[0] in commands else "doctor"
    return argparse.Namespace(
        command=command,
        json="--json" in values,
        execute=False,
        yes=False,
        dry_run=False,
        install_root=None,
        executable=None,
        dcc_path=None,
        python=None,
        material_maker_version=None,
        probe_project=None,
    )


def _same_path(first: Path, second: Path) -> bool:
    try:
        return first.samefile(second)
    except OSError:
        return os.path.normcase(str(first.resolve())) == os.path.normcase(str(second.resolve()))


def _normalize_args(args: argparse.Namespace) -> None:
    mutating = args.command in {"install", "upgrade", "uninstall"}
    if args.dry_run and (args.yes or args.execute):
        raise InstallStateError("invalid_arguments", "Dry-run and execution flags conflict")
    if not mutating and (args.dry_run or args.yes or args.execute):
        raise InstallStateError("invalid_arguments", "Mutation flags require a mutating command")
    if args.dcc_path and args.executable:
        if not _same_path(Path(args.dcc_path).expanduser(), Path(args.executable).expanduser()):
            raise InstallStateError("dcc_path_conflict", "DCC path aliases disagree")
    if args.dcc_path:
        args.executable = args.dcc_path
    owner = Path(args.python or sys.executable).expanduser().resolve()
    current = Path(sys.executable).resolve()
    if not owner.is_file():
        raise InstallStateError("python_owner_unavailable", "Owning Python is unavailable")
    if not _same_path(owner, current):
        raise InstallStateError(
            "python_owner_mismatch", "Owning Python does not match this process"
        )
    args.python = str(owner)
    args.execute = bool(args.execute or args.yes)


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


def _version_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or len(value) > _MAX_VERSION_LENGTH:
        return ()
    match = _FINAL_RELEASE.fullmatch(value)
    if match is None:
        return ()
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _default_install_root() -> Path:
    configured = os.environ.get("DCC_MCP_MATERIAL_MAKER_INSTALL_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (base / "dcc-mcp" / "adapters" / "material-maker").resolve()


def _install_root(value: Optional[str]) -> Path:
    root = Path(value).expanduser().resolve() if value else _default_install_root()
    anchor = Path(root.anchor).resolve()
    if root == anchor or root == Path.home().resolve():
        raise InstallStateError("unsafe_install_root", "Install root is too broad")
    return root


def _receipt_path(root: Path) -> Path:
    return root / _RECEIPT_RELATIVE


def _core_version() -> str:
    return metadata.version("dcc-mcp-core")


def _command_for(
    command: str,
    root: Path,
    *,
    executable: Optional[str] = None,
    host_version: Optional[str] = None,
    probe_project: Optional[str] = None,
    execute: bool = False,
) -> list[str]:
    result = [
        "dcc-mcp-material-maker",
        command,
        "--json",
        "--install-root",
        str(root),
    ]
    if executable:
        result.extend(("--dcc-path", executable))
    result.extend(("--python", str(Path(sys.executable).resolve())))
    if host_version:
        result.extend(("--material-maker-version", host_version))
    if probe_project:
        result.extend(("--probe-project", probe_project))
    if execute:
        result.append("--yes")
    return result


def _next_command(
    identifier: str, description: str, why: str, command: list[str]
) -> dict[str, Any]:
    return {"id": identifier, "description": description, "why": why, "command": command}


def _collect_operator_configuration(report: dict[str, Any], args: argparse.Namespace) -> None:
    """Collect only the two non-discoverable, non-secret readiness inputs."""
    parsed = _version_tuple(args.material_maker_version)
    if not parsed or parsed < _version_tuple(MIN_MATERIAL_MAKER_VERSION):
        print(
            "Enter the trusted canonical Material Maker product release (X.Y.Z):",
            file=sys.stderr,
        )
        try:
            selected_version = input("").strip()
        except (EOFError, KeyboardInterrupt):
            _fail(report, EXIT_VERIFY, "verify", "operator_configuration_cancelled")
            return
        parsed = _version_tuple(selected_version)
        if not parsed or parsed < _version_tuple(MIN_MATERIAL_MAKER_VERSION):
            _fail(report, EXIT_VERIFY, "verify", "material_maker_version_invalid")
            return
        args.material_maker_version = selected_version

    probe = Path(args.probe_project).expanduser().resolve() if args.probe_project else None
    if probe is None or probe.suffix.lower() != ".ptex" or not probe.is_file():
        print("Enter the exact trusted .ptex readiness project path:", file=sys.stderr)
        try:
            selected_probe = input("").strip()
        except (EOFError, KeyboardInterrupt):
            _fail(report, EXIT_VERIFY, "verify", "operator_configuration_cancelled")
            return
        probe = Path(selected_probe).expanduser().resolve() if selected_probe else None
        if probe is None or probe.suffix.lower() != ".ptex" or not probe.is_file():
            _fail(report, EXIT_VERIFY, "verify", "probe_project_required")
            return
        args.probe_project = str(probe)


def _base_report(args: argparse.Namespace, root: Path, program: Optional[str]) -> dict[str, Any]:
    core_version = _core_version()
    return {
        "schema_version": 1,
        "status": "failed",
        "dcc_type": "material_maker",
        "adapter_version": __version__,
        "core_version": core_version,
        "steps": [],
        "next_steps": [],
        "receipt_path": str(_receipt_path(root)),
        "verify": {
            "directly_usable": False,
            "failure_stage": None,
            "failure_reason": None,
        },
        "command": args.command,
        "exit_code": EXIT_VERIFY,
        "directly_usable": False,
        "adapter": {"name": "dcc-mcp-material-maker", "version": __version__},
        "runtime": {
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "platform": sys.platform,
            "machine": platform.machine(),
        },
        "core": {
            "version": core_version,
            "minimum_version": MIN_CORE_VERSION,
            "satisfies_minimum": _version_tuple(core_version) >= _version_tuple(MIN_CORE_VERSION),
        },
        "endpoint": {"kind": "native_cli", "value": None, "source": "unavailable"},
        "config": {
            "install_root": str(root),
            "requested_executable": args.executable,
            "requested_python": args.python,
            "version_variable": "DCC_MCP_MATERIAL_MAKER_VERSION",
            "executable_variable": "DCC_MCP_MATERIAL_MAKER_EXECUTABLE",
            "probe_project_variable": "DCC_MCP_MATERIAL_MAKER_PROBE_PROJECT",
        },
        "host": {
            "name": "Material Maker",
            "version": None,
            "version_source": "unavailable",
            "minimum_version": MIN_MATERIAL_MAKER_VERSION,
            "satisfies_minimum": None,
        },
        "probe": None,
        "failure": None,
        "invocation": _invocation(program),
        "side_effects": {
            "installs": args.command in {"install", "upgrade"} and args.execute,
            "persistent_writes": args.command in {"install", "upgrade", "uninstall"}
            and args.execute,
        },
        "schema_source": "dcc_mcp_core.deployment",
    }


def _emit(report: dict[str, Any], exit_code: int, *, status: Optional[str] = None) -> None:
    report["exit_code"] = exit_code
    report["status"] = status or ("ok" if exit_code == EXIT_OK else "failed")
    usable = exit_code == EXIT_OK and report["status"] == "ok" and report.get("probe") is not None
    report["directly_usable"] = usable
    report["verify"]["directly_usable"] = usable
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def _fail(
    report: dict[str, Any],
    exit_code: int,
    stage: str,
    reason: str,
    *,
    detail: Optional[str] = None,
    next_steps: Optional[list[dict[str, Any]]] = None,
) -> None:
    report["verify"]["failure_stage"] = stage
    report["verify"]["failure_reason"] = reason
    report["failure"] = {"stage": stage, "reason": reason}
    if detail:
        report["failure"]["detail"] = detail[:512]
    report["next_steps"] = list(next_steps or [])
    _emit(report, exit_code)


def _safe_relative(value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise InstallStateError("receipt_invalid", "Owned-file path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise InstallStateError("receipt_invalid", "Owned-file path escapes the install root")
    return Path(*pure.parts)


def _read_receipt(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = _receipt_path(root)
    if not path.is_file() or path.is_symlink():
        raise InstallStateError("receipt_missing", "Managed receipt is missing")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallStateError(
            "receipt_invalid", "Managed receipt is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(receipt, dict) or receipt.get("receipt_version") != 1:
        raise InstallStateError("receipt_invalid", "Managed receipt version is invalid")
    if receipt.get("dcc_type") != "material_maker":
        raise InstallStateError("receipt_invalid", "Managed receipt belongs to another adapter")
    if receipt.get("install_root") != str(root):
        raise InstallStateError("receipt_root_mismatch", "Managed receipt is bound to another root")
    expected_id = _sha256_bytes(("material_maker\0" + str(root)).encode("utf-8"))
    if receipt.get("installation_id") != expected_id:
        raise InstallStateError("receipt_root_mismatch", "Managed receipt root identity is invalid")
    items = receipt.get("owned_files")
    if not isinstance(items, list) or not items:
        raise InstallStateError("receipt_invalid", "Managed receipt has no owned-file manifest")
    owned: set[Path] = set()
    for item in items:
        if not isinstance(item, dict):
            raise InstallStateError("receipt_invalid", "Owned-file entry is invalid")
        relative = _safe_relative(item.get("path"))
        if relative in owned:
            raise InstallStateError("receipt_invalid", "Owned-file manifest has duplicates")
        owned.add(relative)
        candidate = root / relative
        digest = item.get("sha256")
        size = item.get("bytes")
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or not isinstance(size, int)
            or isinstance(size, bool)
        ):
            raise InstallStateError("receipt_integrity_failed", "Owned file is missing")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise InstallStateError("receipt_invalid", "Owned-file digest is invalid")
        if candidate.stat().st_size != size or _sha256_file(candidate) != digest:
            raise InstallStateError("receipt_integrity_failed", "Owned file digest does not match")
    expected_files = owned | {_RECEIPT_RELATIVE}
    expected_directories: set[Path] = set()
    for relative in expected_files:
        parent = relative.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent
    actual_entries: set[Path] = set()
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        actual_entries.add(relative)
        if candidate.is_symlink():
            raise InstallStateError(
                "receipt_integrity_failed", "Managed root contains an unowned symbolic link"
            )
    if actual_entries != expected_files | expected_directories:
        raise InstallStateError("receipt_integrity_failed", "Managed root contains unowned paths")
    if owned != {Path(_CONFIG_NAME)}:
        raise InstallStateError("receipt_invalid", "Owned-file set is not recognized")
    try:
        config = json.loads((root / _CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallStateError("receipt_integrity_failed", "Managed config is invalid") from exc
    if not isinstance(config, dict) or config.get("dcc_type") != "material_maker":
        raise InstallStateError("receipt_integrity_failed", "Managed config identity is invalid")
    return receipt, config


def _new_state(root: Path, config: dict[str, Any]) -> tuple[bytes, bytes]:
    config_bytes = _json_bytes(config)
    receipt = {
        "receipt_version": 1,
        "dcc_type": "material_maker",
        "adapter_version": __version__,
        "install_root": str(root),
        "installation_id": _sha256_bytes(("material_maker\0" + str(root)).encode("utf-8")),
        "owned_files": [
            {
                "path": _CONFIG_NAME,
                "bytes": len(config_bytes),
                "sha256": _sha256_bytes(config_bytes),
            }
        ],
    }
    return config_bytes, _json_bytes(receipt)


def _publish_state(root: Path, config: dict[str, Any]) -> Optional[Path]:
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".%s.staging-" % root.name, dir=str(root.parent)))
    backup = root.parent / (".%s.backup-%s" % (root.name, uuid.uuid4().hex))
    moved_existing = False
    try:
        config_bytes, receipt_bytes = _new_state(root, config)
        (staging / _CONFIG_NAME).write_bytes(config_bytes)
        staged_receipt = staging / _RECEIPT_RELATIVE
        staged_receipt.parent.mkdir(parents=True, exist_ok=True)
        staged_receipt.write_bytes(receipt_bytes)
        if root.exists():
            os.replace(root, backup)
            moved_existing = True
        try:
            os.replace(staging, root)
        except OSError:
            if moved_existing and backup.exists() and not root.exists():
                os.replace(backup, root)
            raise
        return backup if moved_existing else None
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _rollback_published_state(root: Path, backup: Optional[Path]) -> None:
    discarded = root.parent / (".%s.failed-%s" % (root.name, uuid.uuid4().hex))
    if root.exists():
        os.replace(root, discarded)
    if backup is not None and backup.exists():
        os.replace(backup, root)
    if discarded.exists():
        shutil.rmtree(discarded, ignore_errors=True)


def _remove_state(root: Path) -> None:
    quarantine = root.parent / (".%s.uninstall-%s" % (root.name, uuid.uuid4().hex))
    backup = root.parent / (".%s.uninstall-backup-%s" % (root.name, uuid.uuid4().hex))
    shutil.copytree(root, backup, copy_function=shutil.copy2)
    try:
        os.replace(root, quarantine)
        try:
            shutil.rmtree(quarantine)
        except OSError:
            if backup.exists() and not root.exists():
                os.replace(backup, root)
            raise
    finally:
        # A successful delete leaves neither the managed root nor quarantine,
        # so its safety copy is no longer needed.  Keep the copy only when a
        # failed restore left no root and a partial quarantine still exists.
        if backup.exists() and (root.exists() or not quarantine.exists()):
            shutil.rmtree(backup)


def _configured(args: argparse.Namespace, stored: Optional[dict[str, Any]]) -> dict[str, Any]:
    stored = stored or {}
    executable = (
        args.executable
        or os.environ.get("DCC_MCP_MATERIAL_MAKER_EXECUTABLE")
        or stored.get("executable")
    )
    host_version = (
        args.material_maker_version
        or os.environ.get("DCC_MCP_MATERIAL_MAKER_VERSION")
        or stored.get("material_maker_version")
    )
    probe_project = (
        args.probe_project
        or os.environ.get("DCC_MCP_MATERIAL_MAKER_PROBE_PROJECT")
        or stored.get("probe_project")
    )
    return {
        "schema_version": 1,
        "dcc_type": "material_maker",
        "adapter_version": __version__,
        "executable": str(Path(executable).expanduser().resolve()) if executable else None,
        "material_maker_version": host_version,
        "probe_project": str(Path(probe_project).expanduser().resolve()) if probe_project else None,
    }


def _apply_config_to_report(
    report: dict[str, Any], config: dict[str, Any], args: argparse.Namespace
) -> None:
    executable = config.get("executable")
    host_version = config.get("material_maker_version")
    source = (
        "command_line" if args.executable else ("managed_receipt" if executable else "unavailable")
    )
    report["endpoint"] = {"kind": "native_cli", "value": executable, "source": source}
    report["host"].update(
        {
            "version": host_version,
            "version_source": (
                "command_line"
                if args.material_maker_version
                else ("managed_receipt" if host_version else "unavailable")
            ),
            "satisfies_minimum": (
                _version_tuple(host_version) >= _version_tuple(MIN_MATERIAL_MAKER_VERSION)
                if _version_tuple(host_version)
                else None
            ),
        }
    )
    report["config"].update(
        {
            "probe_project": config.get("probe_project"),
            "allowed_roots": (
                [str(Path(config["probe_project"]).parent)] if config.get("probe_project") else []
            ),
        }
    )


def _preflight_config(
    report: dict[str, Any], args: argparse.Namespace, root: Path, config: dict[str, Any]
) -> MaterialMakerCli:
    if not report["core"]["satisfies_minimum"]:
        _fail(
            report,
            EXIT_PREFLIGHT,
            "preflight",
            "core_version_unsupported",
            next_steps=[
                _next_command(
                    "upgrade_dcc_mcp_core",
                    "Install a supported dcc-mcp-core release.",
                    "This adapter requires the documented Core floor.",
                    [sys.executable, "-m", "pip", "install", "dcc-mcp-core==0.20.14"],
                )
            ],
        )
    executable = config.get("executable")
    roots = [Path(config["probe_project"]).parent] if config.get("probe_project") else [Path.cwd()]
    cli = MaterialMakerCli(executable=executable, allowed_roots=roots)
    config["executable"] = cli.executable
    _apply_config_to_report(report, config, args)
    if not cli.executable:
        _fail(
            report,
            EXIT_PREFLIGHT,
            "preflight",
            "material_maker_not_found",
            next_steps=[
                _next_command(
                    "discover_material_maker",
                    "Retry bounded official executable discovery for this managed root.",
                    "The adapter never downloads or substitutes an executable.",
                    _command_for(args.command, root),
                )
            ],
        )
    version = config.get("material_maker_version")
    parsed = _version_tuple(version)
    if not parsed or parsed < _version_tuple(MIN_MATERIAL_MAKER_VERSION):
        _fail(
            report,
            EXIT_VERIFY,
            "verify",
            (
                "material_maker_version_invalid"
                if version and not parsed
                else (
                    "material_maker_version_unsupported"
                    if parsed
                    else "material_maker_version_unknown"
                )
            ),
            next_steps=[
                _next_command(
                    "select_supported_material_maker",
                    "Retry with a trusted canonical final Material Maker release.",
                    "The native CLI cannot report the Material Maker product version.",
                    _command_for(
                        "configure",
                        root,
                        executable=cli.executable,
                        probe_project=config.get("probe_project"),
                    ),
                )
            ],
        )
    probe = config.get("probe_project")
    if not probe or Path(probe).suffix.lower() != ".ptex" or not Path(probe).is_file():
        _fail(
            report,
            EXIT_VERIFY,
            "verify",
            "probe_project_required",
            next_steps=[
                _next_command(
                    "configure_probe_project",
                    "Create or select a trusted bounded .ptex readiness project.",
                    "Exit zero without Material Maker-specific project evidence is not readiness.",
                    _command_for(
                        "configure",
                        root,
                        executable=cli.executable,
                        host_version=str(version),
                    ),
                )
            ],
        )
    return cli


def _verify_host(
    report: dict[str, Any], args: argparse.Namespace, root: Path, config: dict[str, Any]
) -> None:
    cli = _preflight_config(report, args, root, config)
    try:
        status = cli.status(probe_project=config["probe_project"])
    except (MaterialMakerError, OSError) as exc:
        _fail(
            report,
            EXIT_VERIFY,
            "verify",
            "native_probe_failed",
            detail=type(exc).__name__,
            next_steps=[
                _next_command(
                    "retry_native_probe",
                    "Retry the bounded .ptex readiness export after diagnosing the host.",
                    "The official executable did not produce validated Material Maker artifacts.",
                    _command_for(
                        "verify",
                        root,
                        executable=config["executable"],
                        host_version=config["material_maker_version"],
                        probe_project=config["probe_project"],
                    ),
                )
            ],
        )
    if not status.get("ready"):
        _fail(
            report,
            EXIT_VERIFY,
            "verify",
            str(status.get("reason") or "native_probe_failed"),
        )
    report["probe"] = {
        "engine": status.get("engine"),
        "readiness_evidence": status.get("readiness_evidence"),
    }
    report["steps"].append(
        {"id": "verify_host", "status": "ok", "message": "Bounded .ptex readiness export passed."}
    )


def _state_or_failure(report: dict[str, Any], root: Path, *, exit_code: int) -> dict[str, Any]:
    try:
        _, config = _read_receipt(root)
    except InstallStateError as exc:
        _fail(report, exit_code, "verify" if exit_code == EXIT_VERIFY else "install", exc.reason)
    report["steps"].append(
        {"id": "verify_receipt", "status": "ok", "message": "Owned-file manifest matches."}
    )
    return config


def _run_read_only(report: dict[str, Any], args: argparse.Namespace, root: Path) -> None:
    if args.command == "status":
        config = _state_or_failure(report, root, exit_code=EXIT_VERIFY)
        _apply_config_to_report(report, config, args)
        _emit(report, EXIT_OK)
        return
    try:
        _, stored = _read_receipt(root)
        report["steps"].append(
            {"id": "verify_receipt", "status": "ok", "message": "Owned-file manifest matches."}
        )
        report["receipt_path"] = str(_receipt_path(root))
    except InstallStateError as exc:
        if exc.reason != "receipt_missing":
            _fail(report, EXIT_VERIFY, "verify", exc.reason)
        stored = None
        report["receipt_path"] = None
    config = _configured(args, stored)
    _apply_config_to_report(report, config, args)
    _verify_host(report, args, root, config)
    _emit(report, EXIT_OK)


def _run_install(report: dict[str, Any], args: argparse.Namespace, root: Path) -> None:
    existing: Optional[dict[str, Any]] = None
    if root.exists():
        existing = _state_or_failure(report, root, exit_code=EXIT_INSTALL)
    if args.command == "upgrade" and existing is None:
        _fail(report, EXIT_INSTALL, "install", "receipt_missing")
    config = _configured(args, existing)
    _apply_config_to_report(report, config, args)
    _preflight_config(report, args, root, config)
    if args.command == "install" and existing is not None and config != existing:
        _fail(
            report,
            EXIT_INSTALL,
            "install",
            "already_installed_use_upgrade",
            next_steps=[
                _next_command(
                    "plan_upgrade",
                    "Plan an explicit upgrade of the verified managed installation.",
                    "Install never replaces a different managed configuration implicitly.",
                    _command_for(
                        "upgrade",
                        root,
                        executable=config["executable"],
                        host_version=config["material_maker_version"],
                        probe_project=config["probe_project"],
                    ),
                )
            ],
        )
    if not args.execute:
        report["steps"].append(
            {
                "id": "plan_install",
                "status": "pending",
                "message": "No persistent writes performed.",
            }
        )
        report["next_steps"] = [
            _next_command(
                "execute_%s" % args.command,
                "Apply the reviewed managed-state transaction.",
                "Mutating lifecycle commands are plan-first.",
                _command_for(
                    args.command,
                    root,
                    executable=config["executable"],
                    host_version=config["material_maker_version"],
                    probe_project=config["probe_project"],
                    execute=True,
                ),
            )
        ]
        _emit(report, EXIT_OK, status="planned")
        return
    published = existing is None or config != existing
    backup: Optional[Path] = None
    if published:
        try:
            backup = _publish_state(root, config)
        except OSError as exc:
            _fail(
                report,
                EXIT_INSTALL,
                "install",
                "transaction_publish_failed",
                detail=type(exc).__name__,
            )
    try:
        _state_or_failure(report, root, exit_code=EXIT_INSTALL)
        report["steps"].append(
            {
                "id": args.command,
                "status": "ok",
                "message": "Managed state published atomically.",
            }
        )
        _verify_host(report, args, root, config)
        if backup is not None and backup.exists():
            shutil.rmtree(backup)
    except BaseException:
        if published:
            _rollback_published_state(root, backup)
        raise
    _emit(report, EXIT_OK)


def _run_uninstall(report: dict[str, Any], args: argparse.Namespace, root: Path) -> None:
    if not root.exists():
        report["receipt_path"] = None
        report["steps"].append(
            {
                "id": "uninstall",
                "status": "already_absent",
                "message": "Managed state is already absent.",
            }
        )
        _emit(report, EXIT_OK)
        return
    config = _state_or_failure(report, root, exit_code=EXIT_INSTALL)
    _apply_config_to_report(report, config, args)
    if not args.execute:
        report["steps"].append(
            {
                "id": "plan_uninstall",
                "status": "pending",
                "message": "No persistent writes performed.",
            }
        )
        report["next_steps"] = [
            _next_command(
                "execute_uninstall",
                "Remove the verified adapter-owned state transactionally.",
                "The receipt proves the exact dedicated root and every owned file.",
                _command_for("uninstall", root, execute=True),
            )
        ]
        _emit(report, EXIT_OK, status="planned")
        return
    try:
        _remove_state(root)
    except OSError as exc:
        _fail(
            report,
            EXIT_INSTALL,
            "install",
            "transaction_uninstall_failed",
            detail=type(exc).__name__,
        )
    report["receipt_path"] = None
    report["steps"].append(
        {"id": "uninstall", "status": "ok", "message": "Verified adapter-owned state removed."}
    )
    _emit(report, EXIT_OK)


def main(argv: Optional[Sequence[str]] = None, *, program: Optional[str] = None) -> None:
    """Run a plan-first Install SOP lifecycle and emit exactly one JSON result."""
    values = list(argv) if argv is not None else list(sys.argv[1:])
    parser = _parser()
    try:
        args = parser.parse_args(values)
    except _ArgumentError:
        if "--json" not in values:
            parser.print_usage(sys.stderr)
            print("invalid Install SOP arguments", file=sys.stderr)
            raise SystemExit(EXIT_PREFLIGHT) from None
        args = _fallback_args(values)
        root = _default_install_root()
        report = _base_report(args, root, program)
        _fail(report, EXIT_PREFLIGHT, "preflight", "invalid_arguments")
        return
    try:
        _normalize_args(args)
    except InstallStateError as exc:
        root = _default_install_root()
        report = _base_report(args, root, program)
        _fail(report, EXIT_PREFLIGHT, "preflight", exc.reason)
        return
    try:
        root = _install_root(args.install_root)
    except InstallStateError as exc:
        root = _default_install_root()
        report = _base_report(args, root, program)
        _fail(report, EXIT_PREFLIGHT, "preflight", exc.reason)
        return
    report = _base_report(args, root, program)
    if args.command == "configure":
        _collect_operator_configuration(report, args)
    if args.command in {"doctor", "verify", "status", "configure"}:
        _run_read_only(report, args, root)
    elif args.command in {"install", "upgrade"}:
        _run_install(report, args, root)
    else:
        _run_uninstall(report, args, root)


if __name__ == "__main__":
    main()
