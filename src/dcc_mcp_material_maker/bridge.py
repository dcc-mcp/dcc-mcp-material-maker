"""Bounded wrapper around Material Maker's official command-line export API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping, Optional, Sequence

from dcc_mcp_core.skills_helper import check_dcc_cancelled

_PROJECT_SUFFIXES = frozenset({".ptex"})
_DOCUMENTED_TARGETS = ("Blender", "Godot", "Unity", "Unreal")
_OUTPUT_TEMPLATE = re.compile(r"^[A-Za-z0-9._% -]{1,128}$")
_MAX_CAPTURE_BYTES = 131_072
_PROCESS_CLEANUP_SECS = 3.0


class MaterialMakerError(RuntimeError):
    """Material Maker is unavailable or rejected a bounded operation."""


class MaterialMakerTimeoutError(MaterialMakerError):
    """Material Maker did not complete before the configured deadline."""


class _PipeCollector:
    """Drain one child pipe without allowing captured output to grow unbounded."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.buffer = bytearray()
        self.truncated = False
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _drain(self) -> None:
        while True:
            chunk = self.stream.read(64 * 1024)
            if not chunk:
                return
            remaining = (_MAX_CAPTURE_BYTES + 1) - len(self.buffer)
            if remaining > 0:
                self.buffer.extend(chunk[:remaining])
            if len(chunk) > remaining or len(self.buffer) > _MAX_CAPTURE_BYTES:
                self.truncated = True

    def finish(self, deadline: float) -> tuple[str, bool]:
        self.thread.join(max(0.0, deadline - time.monotonic()))
        if self.thread.is_alive():
            self.stream.close()
            self.thread.join(max(0.0, deadline - time.monotonic()))
        if self.thread.is_alive():
            raise MaterialMakerError("Material Maker process output cleanup exceeded its bound")
        self.stream.close()
        raw = bytes(self.buffer[:_MAX_CAPTURE_BYTES])
        return raw.decode("utf-8", errors="replace"), self.truncated


class _PosixProcessGroup:
    """Own a group whose direct-child supervisor remains its identity-bound leader."""

    def __init__(self, process: subprocess.Popen[bytes], status_fd: Optional[int] = None) -> None:
        self.process = process
        self.process_group = process.pid
        self.status_fd = status_fd
        self.status_buffer = bytearray()
        self.target_returncode: Optional[int] = None

    def poll_target(self) -> Optional[int]:
        if self.target_returncode is not None:
            return self.target_returncode
        if self.status_fd is None:
            raise MaterialMakerError("Material Maker process supervisor status is unavailable")
        while b"\n" not in self.status_buffer:
            try:
                chunk = os.read(self.status_fd, 64)
            except BlockingIOError:
                break
            if not chunk:
                break
            self.status_buffer.extend(chunk)
            if len(self.status_buffer) > 64:
                raise MaterialMakerError("Material Maker process supervisor status is invalid")
        if b"\n" in self.status_buffer:
            line, _, remainder = self.status_buffer.partition(b"\n")
            if remainder:
                raise MaterialMakerError("Material Maker process supervisor status is invalid")
            if line == b"spawn_error":
                raise MaterialMakerError("Material Maker process could not be started")
            prefix = b"returncode:"
            if not line.startswith(prefix):
                raise MaterialMakerError("Material Maker process supervisor status is invalid")
            try:
                returncode = int(line[len(prefix) :].decode("ascii"))
            except (UnicodeDecodeError, ValueError) as error:
                raise MaterialMakerError(
                    "Material Maker process supervisor status is invalid"
                ) from error
            if returncode < -255 or returncode > 255:
                raise MaterialMakerError("Material Maker process supervisor status is invalid")
            self.target_returncode = returncode
            return returncode
        if self.process.poll() is not None:
            raise MaterialMakerError(
                "Material Maker process supervisor exited before reporting target status"
            )
        return None

    def terminate(self, force: bool = False) -> None:
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process_group, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        if self.status_fd is not None:
            os.close(self.status_fd)
            self.status_fd = None


def _start_posix_supervisor(
    command: Sequence[str], popen_kwargs: Mapping[str, Any]
) -> tuple[subprocess.Popen[bytes], _PosixProcessGroup]:
    status_read, status_write = os.pipe()
    os.set_blocking(status_read, False)
    supervisor_command = [
        sys.executable,
        "-m",
        "dcc_mcp_material_maker._process_supervisor",
        str(status_write),
        "--",
    ] + [str(item) for item in command]
    supervisor_kwargs = dict(popen_kwargs)
    supervisor_kwargs["start_new_session"] = True
    supervisor_kwargs["pass_fds"] = (status_write,)
    try:
        process = subprocess.Popen(supervisor_command, **supervisor_kwargs)
    except BaseException:
        os.close(status_read)
        os.close(status_write)
        raise
    os.close(status_write)
    return process, _PosixProcessGroup(process, status_read)


class _WindowsJob:
    """Own a Windows child tree from its first instruction through cleanup."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._thread_entry_type = ThreadEntry
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = self._ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        snapshot = self._kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        if snapshot == self._ctypes.c_void_p(-1).value:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        resumed = False
        try:
            entry = self._thread_entry_type()
            entry.dwSize = self._ctypes.sizeof(entry)
            found = bool(self._kernel32.Thread32First(snapshot, self._ctypes.byref(entry)))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = self._kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise self._ctypes.WinError(self._ctypes.get_last_error())
                    try:
                        if self._kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise self._ctypes.WinError(self._ctypes.get_last_error())
                        resumed = True
                    finally:
                        self._kernel32.CloseHandle(thread)
                found = bool(self._kernel32.Thread32Next(snapshot, self._ctypes.byref(entry)))
        finally:
            self._kernel32.CloseHandle(snapshot)
        if not resumed:
            raise OSError("suspended process has no resumable thread")

    def terminate(self, force: bool = False) -> None:
        del force
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


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
        return self._run_process(command, timeout)

    def _run_process(self, command: Sequence[str], timeout_secs: float) -> dict[str, Any]:
        """Run one fixed argv under an owned process tree and bounded pipe drains."""
        timeout = self._timeout(timeout_secs)
        started = time.monotonic()
        process: Optional[subprocess.Popen[bytes]] = None
        owner: Any = None
        stdout_collector: Optional[_PipeCollector] = None
        stderr_collector: Optional[_PipeCollector] = None
        target_returncode: Optional[int] = None
        pending: Optional[BaseException] = None
        try:
            popen_kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": False,
            }
            if os.name == "nt":
                owner = _WindowsJob()
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | 0x00000004
            try:
                if os.name == "nt":
                    process = subprocess.Popen([str(item) for item in command], **popen_kwargs)
                    owner.assign_and_resume(process)
                else:
                    process, owner = _start_posix_supervisor(command, popen_kwargs)
            except BaseException:
                if process is not None:
                    process.kill()
                    process.wait(timeout=_PROCESS_CLEANUP_SECS)
                raise
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_collector = _PipeCollector(process.stdout)
            stderr_collector = _PipeCollector(process.stderr)
            stdout_collector.start()
            stderr_collector.start()
            deadline = started + timeout
            while target_returncode is None:
                if os.name == "nt":
                    target_returncode = process.poll()
                else:
                    target_returncode = owner.poll_target()
                if target_returncode is not None:
                    break
                check_dcc_cancelled()
                if time.monotonic() >= deadline:
                    raise MaterialMakerTimeoutError(
                        "Material Maker exceeded the %.1f second timeout" % timeout
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except BaseException as exc:
            pending = exc

        cleanup_deadline = time.monotonic() + _PROCESS_CLEANUP_SECS
        cleanup_error: Optional[BaseException] = None
        stdout = ""
        stderr = ""
        stdout_truncated = False
        stderr_truncated = False
        try:
            if owner is not None:
                owner.terminate()
            if process is not None and process.poll() is None:
                try:
                    process.wait(timeout=min(1.0, max(0.0, cleanup_deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    if owner is not None:
                        owner.terminate(force=True)
                    process.kill()
                    process.wait(timeout=max(0.0, cleanup_deadline - time.monotonic()))
            if owner is not None:
                owner.terminate(force=True)
            if stdout_collector is not None:
                stdout, stdout_truncated = stdout_collector.finish(cleanup_deadline)
            if stderr_collector is not None:
                stderr, stderr_truncated = stderr_collector.finish(cleanup_deadline)
        except BaseException as exc:
            cleanup_error = exc
        finally:
            if owner is not None:
                try:
                    owner.close()
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc

        if cleanup_error is not None:
            raise MaterialMakerError(
                "Material Maker process tree cleanup failed"
            ) from cleanup_error
        if pending is not None:
            raise pending.with_traceback(pending.__traceback__)
        assert process is not None
        return {
            "returncode": int(target_returncode or 0),
            "duration_secs": round(time.monotonic() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
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

    def status(
        self,
        probe: bool = True,
        timeout_secs: float = 60,
        probe_project: Optional[str] = None,
    ) -> dict[str, Any]:
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
        if not probe or not probe_project:
            result["reason"] = "probe_project_required"
            return result
        project = self._project_path(probe_project)
        with tempfile.TemporaryDirectory(
            prefix=".dcc-mcp-material-maker-probe-", dir=str(project.parent)
        ) as directory:
            exported = self.export_material(
                str(project),
                str(Path(directory) / "export"),
                target="Godot",
                output_file="%f",
                timeout_secs=timeout_secs,
            )
        result["engine"] = exported["engine"]
        result["readiness_evidence"] = {
            "probe_project_sha256": _sha256_file(project),
            "output_file_count": exported["file_count"],
            "output_bytes": exported["total_bytes"],
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
                if size <= 0:
                    raise MaterialMakerError("Material Maker produced an empty export file")
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
