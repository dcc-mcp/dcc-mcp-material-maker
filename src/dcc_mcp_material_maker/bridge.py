"""Bounded wrapper around Material Maker's official command-line export API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from dcc_mcp_core.skills_helper import check_dcc_cancelled

_PROJECT_SUFFIXES = frozenset({".ptex"})
_DOCUMENTED_TARGETS = ("Blender", "Godot", "Unity", "Unreal")
_OUTPUT_TEMPLATE = re.compile(r"^[A-Za-z0-9._% -]{1,128}$")


class MaterialMakerError(RuntimeError):
    """Material Maker is unavailable or rejected a bounded operation."""


class MaterialMakerTimeoutError(MaterialMakerError):
    """Material Maker did not complete before the configured deadline."""


def _split_roots(value: str) -> list[Path]:
    roots = []
    for item in value.split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(Path(item).expanduser().resolve())
    return roots


def _within(path: Path, roots: Sequence[Path]) -> bool:
    candidate = os.path.normcase(str(path))
    for root in roots:
        normalized_root = os.path.normcase(str(root))
        try:
            if os.path.commonpath((candidate, normalized_root)) == normalized_root:
                return True
        except ValueError:
            continue
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_text(value: Any, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MaterialMakerError("%s must be a non-empty string" % label)
    if len(value) > maximum:
        raise MaterialMakerError("%s is limited to %d characters" % (label, maximum))
    return value


class MaterialMakerCli:
    """Typed, workspace-bounded Material Maker project and export operations."""

    def __init__(
        self,
        executable: Optional[str] = None,
        allowed_roots: Optional[Iterable[Path]] = None,
        max_project_bytes: int = 64 * 1024 * 1024,
        max_nodes: int = 50_000,
        max_connections: int = 200_000,
        max_export_files: int = 512,
        max_export_bytes: int = 2 * 1024 * 1024 * 1024,
        max_timeout_secs: float = 1_800,
    ) -> None:
        self.executable = self._resolve_executable(executable)
        roots = list(allowed_roots or (Path.cwd(),))
        self.allowed_roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self.max_project_bytes = max(1, int(max_project_bytes))
        self.max_nodes = max(1, int(max_nodes))
        self.max_connections = max(1, int(max_connections))
        self.max_export_files = max(1, int(max_export_files))
        self.max_export_bytes = max(1, int(max_export_bytes))
        self.max_timeout_secs = max(1.0, float(max_timeout_secs))

    @classmethod
    def from_env(cls) -> "MaterialMakerCli":
        roots_value = os.environ.get("DCC_MCP_MATERIAL_MAKER_ALLOWED_ROOTS", "")
        roots = _split_roots(roots_value) if roots_value else [Path.cwd().resolve()]
        return cls(
            os.environ.get("DCC_MCP_MATERIAL_MAKER_EXECUTABLE") or None,
            allowed_roots=roots,
            max_project_bytes=int(
                os.environ.get("DCC_MCP_MATERIAL_MAKER_MAX_PROJECT_BYTES", str(64 * 1024 * 1024))
            ),
            max_nodes=int(os.environ.get("DCC_MCP_MATERIAL_MAKER_MAX_NODES", "50000")),
            max_connections=int(os.environ.get("DCC_MCP_MATERIAL_MAKER_MAX_CONNECTIONS", "200000")),
            max_export_files=int(os.environ.get("DCC_MCP_MATERIAL_MAKER_MAX_EXPORT_FILES", "512")),
            max_export_bytes=int(
                os.environ.get(
                    "DCC_MCP_MATERIAL_MAKER_MAX_EXPORT_BYTES", str(2 * 1024 * 1024 * 1024)
                )
            ),
            max_timeout_secs=float(
                os.environ.get("DCC_MCP_MATERIAL_MAKER_MAX_TIMEOUT_SECS", "1800")
            ),
        )

    @staticmethod
    def _resolve_executable(explicit: Optional[str]) -> Optional[str]:
        candidates = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        else:
            for name in ("material_maker", "material-maker", "material_maker.exe"):
                found = shutil.which(name)
                if found:
                    candidates.append(Path(found))
            if os.name == "nt":
                program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home()))
                user_profile = Path(os.environ.get("USERPROFILE", Path.home()))
                candidates.extend(
                    (
                        program_files / "Material Maker" / "material_maker.exe",
                        local_app_data / "Programs" / "Material Maker" / "material_maker.exe",
                        user_profile
                        / "scoop"
                        / "apps"
                        / "material-maker"
                        / "current"
                        / "material_maker.exe",
                    )
                )
        for candidate in candidates:
            try:
                candidate = candidate.resolve()
            except OSError:
                continue
            if candidate.is_file():
                return str(candidate)
        return None

    def _timeout(self, value: float) -> float:
        timeout = float(value)
        if timeout <= 0 or timeout > self.max_timeout_secs:
            raise MaterialMakerError(
                "timeout_secs must be greater than 0 and no more than %s"
                % int(self.max_timeout_secs)
            )
        return timeout

    def _run(self, args: Sequence[str], timeout_secs: float) -> dict[str, Any]:
        if not self.executable:
            raise MaterialMakerError(
                "Material Maker was not found; set DCC_MCP_MATERIAL_MAKER_EXECUTABLE"
            )
        timeout = self._timeout(timeout_secs)
        command = [self.executable, "--headless"] + [str(item) for item in args]
        started = time.monotonic()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file:
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    creationflags=creationflags,
                )
                try:
                    deadline = started + timeout
                    while process.poll() is None:
                        check_dcc_cancelled()
                        if time.monotonic() >= deadline:
                            raise MaterialMakerTimeoutError(
                                "Material Maker exceeded the %.1f second timeout" % timeout
                            )
                        time.sleep(0.05)
                except BaseException:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                    raise
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read(131_073)
                stderr = stderr_file.read(131_073)
        return {
            "returncode": int(process.returncode or 0),
            "duration_secs": round(time.monotonic() - started, 3),
            "stdout": stdout[:131_072],
            "stderr": stderr[:131_072],
            "stdout_truncated": len(stdout) > 131_072,
            "stderr_truncated": len(stderr) > 131_072,
        }

    def _project_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() not in _PROJECT_SUFFIXES:
            raise MaterialMakerError("Project must use the .ptex extension")
        if not path.is_file():
            raise MaterialMakerError("Project does not exist: %s" % path)
        if not _within(path, self.allowed_roots):
            raise MaterialMakerError("Project is outside DCC_MCP_MATERIAL_MAKER_ALLOWED_ROOTS")
        if path.stat().st_size > self.max_project_bytes:
            raise MaterialMakerError("Project exceeds the configured size limit")
        return path

    def _output_directory(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if path.exists():
            raise MaterialMakerError("Output directory already exists; choose a new directory")
        if not path.parent.is_dir():
            raise MaterialMakerError("Output parent directory does not exist: %s" % path.parent)
        if not _within(path, self.allowed_roots):
            raise MaterialMakerError(
                "Output directory is outside DCC_MCP_MATERIAL_MAKER_ALLOWED_ROOTS"
            )
        return path

    def _read_project(self, path: Path) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MaterialMakerError("Project is not valid UTF-8 JSON") from exc
        if not isinstance(value, Mapping):
            raise MaterialMakerError("Project root must be a JSON object")
        return value

    def _validate_data(self, value: Mapping[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        node_types: Counter[str] = Counter()
        node_count = 0
        connection_count = 0
        graph_count = 0
        material_count = 0
        containers_seen = 0
        stack: list[Any] = [value]
        graph_ids: set[int] = set()

        while stack:
            item = stack.pop()
            if isinstance(item, Mapping):
                containers_seen += 1
                if containers_seen > 500_000:
                    errors.append("Project nesting exceeds the inspection budget")
                    break
                nodes = item.get("nodes")
                connections = item.get("connections")
                if isinstance(nodes, list) and isinstance(connections, list):
                    identity = id(item)
                    if identity not in graph_ids:
                        graph_ids.add(identity)
                        graph_count += 1
                        names: set[str] = set()
                        for index, node in enumerate(nodes):
                            node_count += 1
                            if node_count > self.max_nodes:
                                errors.append("Project exceeds the configured node limit")
                                break
                            if not isinstance(node, Mapping):
                                errors.append("Graph node %d is not an object" % index)
                                continue
                            name = node.get("name")
                            node_type = node.get("type")
                            if not isinstance(name, str) or not name or len(name) > 512:
                                errors.append("Graph node %d has an invalid name" % index)
                            elif name in names:
                                errors.append("Graph contains duplicate node name: %s" % name)
                            else:
                                names.add(name)
                            if not isinstance(node_type, str) or not node_type:
                                errors.append("Graph node %d has an invalid type" % index)
                            else:
                                node_types[node_type] += 1
                                if node_type == "material" or node_type.endswith("_material"):
                                    material_count += 1
                        for index, connection in enumerate(connections):
                            connection_count += 1
                            if connection_count > self.max_connections:
                                errors.append("Project exceeds the configured connection limit")
                                break
                            if not isinstance(connection, Mapping):
                                errors.append("Graph connection %d is not an object" % index)
                                continue
                            for endpoint in ("from", "to"):
                                name = connection.get(endpoint)
                                if not isinstance(name, str) or name not in names:
                                    errors.append(
                                        "Graph connection %d has unknown %s node"
                                        % (index, endpoint)
                                    )
                            for port in ("from_port", "to_port"):
                                port_value = connection.get(port)
                                if (
                                    isinstance(port_value, bool)
                                    or not isinstance(port_value, int)
                                    or port_value < 0
                                ):
                                    errors.append(
                                        "Graph connection %d has invalid %s" % (index, port)
                                    )
                stack.extend(item.values())
            elif isinstance(item, list):
                containers_seen += 1
                if containers_seen > 500_000:
                    errors.append("Project nesting exceeds the inspection budget")
                    break
                stack.extend(item)

        if graph_count == 0:
            errors.append("Project contains no Material Maker graph")
        if material_count == 0:
            warnings.append("Project contains no material export node")
        return {
            "valid": not errors,
            "errors": errors[:100],
            "warnings": warnings[:100],
            "graph_count": graph_count,
            "node_count": node_count,
            "connection_count": connection_count,
            "material_node_count": material_count,
            "node_types": dict(sorted(node_types.items())),
        }

    def status(self, probe: bool = True, timeout_secs: float = 60) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ready": False,
            "executable": self.executable,
            "instance_type": "standalone",
            "driver": "official_material_maker_cli",
            "documented_targets": list(_DOCUMENTED_TARGETS),
            "allowed_roots": [str(root) for root in self.allowed_roots],
            "limits": {
                "max_project_bytes": self.max_project_bytes,
                "max_nodes": self.max_nodes,
                "max_connections": self.max_connections,
                "max_export_files": self.max_export_files,
                "max_export_bytes": self.max_export_bytes,
                "max_timeout_secs": self.max_timeout_secs,
            },
            "arbitrary_script_input": False,
        }
        if not self.executable:
            result["reason"] = "material_maker_not_found"
            return result
        if probe:
            with tempfile.TemporaryDirectory(prefix="dcc-mcp-material-maker-probe-") as directory:
                run = self._run(
                    ("--export-material", "--output-dir", directory),
                    timeout_secs,
                )
            combined = "%s\n%s" % (run["stdout"], run["stderr"])
            if run["returncode"] != 0 or "ERROR:" in combined:
                raise MaterialMakerError("Material Maker CLI readiness probe failed")
            result["engine"] = {
                key: run[key]
                for key in (
                    "returncode",
                    "duration_secs",
                    "stdout_truncated",
                    "stderr_truncated",
                )
            }
        result["ready"] = True
        result["version"] = os.environ.get("DCC_MCP_MATERIAL_MAKER_VERSION", "unknown")
        return result

    def inspect_project(self, path: str) -> dict[str, Any]:
        project = self._project_path(path)
        data = self._read_project(project)
        validation = self._validate_data(data)
        return {
            "path": str(project),
            "bytes": project.stat().st_size,
            "sha256": _sha256_file(project),
            "name": data.get("name"),
            "label": data.get("label"),
            "project_type": data.get("type"),
            **validation,
        }

    def validate_project(self, path: str) -> dict[str, Any]:
        project = self._project_path(path)
        data = self._read_project(project)
        return {"path": str(project), **self._validate_data(data)}

    def export_material(
        self,
        project_path: str,
        output_directory: str,
        target: str = "Godot",
        output_file: str = "%f",
        timeout_secs: float = 900,
    ) -> dict[str, Any]:
        project = self._project_path(project_path)
        destination = self._output_directory(output_directory)
        target = _safe_text(target, "target", maximum=32)
        if target not in _DOCUMENTED_TARGETS:
            raise MaterialMakerError("target must be one of: %s" % ", ".join(_DOCUMENTED_TARGETS))
        output_file = _safe_text(output_file, "output_file", maximum=128)
        if (
            not _OUTPUT_TEMPLATE.fullmatch(output_file)
            or ".." in output_file
            or "/" in output_file
            or "\\" in output_file
        ):
            raise MaterialMakerError("output_file must be a safe filename template")
        validation = self.validate_project(str(project))
        if not validation["valid"]:
            raise MaterialMakerError("Project failed structural validation")

        staging = Path(
            tempfile.mkdtemp(prefix=".%s." % destination.name, dir=str(destination.parent))
        )
        try:
            run = self._run(
                (
                    "--export-material",
                    "--target",
                    target,
                    "--output-dir",
                    str(staging),
                    "--output-file",
                    output_file,
                    str(project),
                ),
                timeout_secs,
            )
            combined = "%s\n%s" % (run["stdout"], run["stderr"])
            if run["returncode"] != 0 or "ERROR:" in combined:
                raise MaterialMakerError("Material Maker CLI export failed")
            files = []
            total_bytes = 0
            for candidate in sorted(staging.rglob("*")):
                if candidate.is_symlink():
                    raise MaterialMakerError("Material Maker export produced a symbolic link")
                if not candidate.is_file():
                    continue
                relative = candidate.relative_to(staging).as_posix()
                size = candidate.stat().st_size
                total_bytes += size
                if len(files) >= self.max_export_files:
                    raise MaterialMakerError("Export exceeds the configured file-count limit")
                if total_bytes > self.max_export_bytes:
                    raise MaterialMakerError("Export exceeds the configured byte limit")
                files.append(
                    {
                        "path": relative,
                        "bytes": size,
                        "sha256": _sha256_file(candidate),
                    }
                )
            if not files:
                raise MaterialMakerError("Material Maker produced no export files")
            os.replace(str(staging), str(destination))
        finally:
            if staging.exists():
                shutil.rmtree(staging)

        return {
            "project_path": str(project),
            "output_directory": str(destination),
            "target": target,
            "file_count": len(files),
            "total_bytes": total_bytes,
            "files": files,
            "engine": {
                key: run[key]
                for key in (
                    "returncode",
                    "duration_secs",
                    "stdout_truncated",
                    "stderr_truncated",
                )
            },
        }


def get_bridge() -> MaterialMakerCli:
    return MaterialMakerCli.from_env()
