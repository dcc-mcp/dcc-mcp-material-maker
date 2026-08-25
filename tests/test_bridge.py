from __future__ import annotations

import concurrent.futures
import ctypes
import json
import os
import select
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from dcc_mcp_material_maker import bridge
from dcc_mcp_material_maker.bridge import (
    MaterialMakerCli,
    MaterialMakerError,
    MaterialMakerTimeoutError,
)

PROCESS_TREE_HELPER = Path(__file__).with_name("process_tree_helper.py")


class _ProcessIdentity:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.handle: Optional[int] = None
        self.pidfd: Optional[int] = None
        if os.name == "nt":
            synchronize_and_terminate = 0x00100001
            handle = ctypes.windll.kernel32.OpenProcess(synchronize_and_terminate, False, pid)
            if not handle:
                raise OSError("failed to bind process identity")
            self.handle = int(handle)
        elif hasattr(os, "pidfd_open"):
            self.pidfd = os.pidfd_open(pid)

    def wait_dead(self, timeout_secs: float = 5.0) -> bool:
        if self.handle is not None:
            wait_object_0 = 0
            result = ctypes.windll.kernel32.WaitForSingleObject(
                self.handle, int(timeout_secs * 1000)
            )
            return result == wait_object_0
        if self.pidfd is not None:
            readable, _, _ = select.select([self.pidfd], [], [], timeout_secs)
            return bool(readable)
        deadline = time.monotonic() + timeout_secs
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    def force_kill(self) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.TerminateProcess(self.handle, 91)
            return
        if self.pidfd is not None and hasattr(signal, "pidfd_send_signal"):
            signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
            return
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.CloseHandle(self.handle)
        if self.pidfd is not None:
            os.close(self.pidfd)


def _wait_for_ready(future, ready_path: Path, timeout_secs: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if ready_path.is_file():
            return
        if future.done():
            future.result()
        time.sleep(0.01)
    raise AssertionError("process-tree helper did not become ready")


def _bound_tree(root_pid_path: Path, descendant_pid_path: Path) -> list[_ProcessIdentity]:
    return [
        _ProcessIdentity(int(root_pid_path.read_text(encoding="ascii"))),
        _ProcessIdentity(int(descendant_pid_path.read_text(encoding="ascii"))),
    ]


def _assert_tree_dead(identities: list[_ProcessIdentity]) -> None:
    dead = [False] * len(identities)
    try:
        for index, identity in enumerate(identities):
            dead[index] = identity.wait_dead()
        assert all(dead)
    finally:
        for identity, is_dead in zip(identities, dead):
            if not is_dead:
                identity.force_kill()
            identity.close()


def _tree_command(tmp_path: Path) -> tuple[list[str], Path, Path, Path]:
    root_pid = tmp_path / "root.pid"
    descendant_pid = tmp_path / "descendant.pid"
    ready = tmp_path / "descendant.ready"
    return (
        [
            sys.executable,
            str(PROCESS_TREE_HELPER),
            str(root_pid),
            str(descendant_pid),
            str(ready),
        ],
        root_pid,
        descendant_pid,
        ready,
    )


def _root_exit_tree_command(tmp_path: Path) -> tuple[list[str], Path, Path, Path, Path]:
    command, root_pid, descendant_pid, ready = _tree_command(tmp_path)
    release = tmp_path / "root-exit.release"
    command.insert(2, "root-exits")
    command.append(str(release))
    return command, root_pid, descendant_pid, ready, release


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


def test_status_rejects_exit_zero_with_only_zero_byte_export_artifacts(tmp_path):
    class ZeroByteExportCli(FakeMaterialMakerCli):
        def _run(self, args, timeout_secs):
            output = Path(args[args.index("--output-dir") + 1])
            (output / "empty.png").write_bytes(b"")
            return {
                "returncode": 0,
                "duration_secs": 0.01,
                "stdout": "Done\n",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

    project = write_project(tmp_path / "probe.ptex")
    cli = ZeroByteExportCli(tmp_path)

    with pytest.raises(MaterialMakerError, match="empty export file"):
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


def test_posix_owner_never_signals_a_reused_group_after_its_leader_exits(monkeypatch):
    class ReapedLeader:
        pid = 424_242

        @staticmethod
        def poll():
            return 0

    signalled = []
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, requested_signal: signalled.append((process_group, requested_signal)),
        raising=False,
    )

    owner = bridge._PosixProcessGroup(ReapedLeader())
    owner.terminate(force=True)

    assert signalled == []


def test_timeout_terminates_root_and_ready_descendant_with_inherited_pipes(tmp_path):
    cli = MaterialMakerCli(executable=sys.executable, allowed_roots=[tmp_path])
    command, root_pid, descendant_pid, ready = _tree_command(tmp_path)
    started = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cli._run_process, command, 5.0)
        _wait_for_ready(future, ready)
        identities = _bound_tree(root_pid, descendant_pid)
        with pytest.raises(MaterialMakerTimeoutError, match="exceeded"):
            future.result(timeout=10)

    assert time.monotonic() - started < 11
    _assert_tree_dead(identities)


def test_cancellation_terminates_root_and_ready_descendant_without_orphans(monkeypatch, tmp_path):
    class RequestedCancellation(RuntimeError):
        pass

    cancelled = threading.Event()

    def check_cancelled():
        if cancelled.is_set():
            raise RequestedCancellation("cancelled")

    monkeypatch.setattr(bridge, "check_dcc_cancelled", check_cancelled)
    cli = MaterialMakerCli(executable=sys.executable, allowed_roots=[tmp_path])
    command, root_pid, descendant_pid, ready = _tree_command(tmp_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cli._run_process, command, 10.0)
        _wait_for_ready(future, ready)
        identities = _bound_tree(root_pid, descendant_pid)
        cancelled.set()
        with pytest.raises(RequestedCancellation, match="cancelled"):
            future.result(timeout=6)

    _assert_tree_dead(identities)


def test_completed_root_cannot_release_group_identity_before_descendant_cleanup(tmp_path):
    cli = MaterialMakerCli(executable=sys.executable, allowed_roots=[tmp_path])
    command, root_pid, descendant_pid, ready, release = _root_exit_tree_command(tmp_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cli._run_process, command, 5.0)
        _wait_for_ready(future, ready)
        identities = _bound_tree(root_pid, descendant_pid)
        release.write_text("exit", encoding="ascii")
        result = future.result(timeout=6)

    assert result["returncode"] == 0
    _assert_tree_dead(identities)
