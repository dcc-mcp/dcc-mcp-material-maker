"""Deterministic native-process tree used by bridge cleanup regressions."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


def _write(path: str, value: str) -> None:
    target = Path(path)
    target.write_text(value, encoding="ascii")


def _descendant(pid_path: str, ready_path: str) -> None:
    _write(pid_path, str(os.getpid()))
    sys.stdout.write("descendant stdout ready\n")
    sys.stdout.flush()
    sys.stderr.write("descendant stderr ready\n")
    sys.stderr.flush()
    _write(ready_path, "ready")
    while True:
        time.sleep(0.05)


def _root(
    root_pid_path: str,
    descendant_pid_path: str,
    ready_path: str,
    *,
    exit_release_path: Optional[str] = None,
) -> None:
    _write(root_pid_path, str(os.getpid()))
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "descendant",
            descendant_pid_path,
            ready_path,
        ],
        stdin=subprocess.DEVNULL,
    )
    if exit_release_path is not None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if Path(ready_path).is_file() and Path(exit_release_path).is_file():
                return
            time.sleep(0.01)
        raise RuntimeError("root exit was not released")
    while True:
        time.sleep(0.05)


def main() -> None:
    mode, first, second = sys.argv[1:4]
    if mode == "descendant":
        _descendant(first, second)
        return
    if mode == "root-exits":
        root_pid_path, descendant_pid_path, ready_path, release_path = sys.argv[2:6]
        _root(
            root_pid_path,
            descendant_pid_path,
            ready_path,
            exit_release_path=release_path,
        )
        return
    root_pid_path, descendant_pid_path, ready_path = sys.argv[1:4]
    _root(root_pid_path, descendant_pid_path, ready_path)


if __name__ == "__main__":
    main()
