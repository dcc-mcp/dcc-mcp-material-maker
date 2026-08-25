"""Private POSIX process-group leader for one bounded native bridge call."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def _write_status(status_fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(status_fd, view)
        view = view[written:]


def _reset_target_signals() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _remain_group_leader() -> None:
    while True:
        signal.pause()


def main() -> None:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        raise SystemExit(64)
    try:
        status_fd = int(sys.argv[1])
    except ValueError:
        raise SystemExit(64) from None
    command = sys.argv[3:]
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        target = subprocess.Popen(command, close_fds=True, preexec_fn=_reset_target_signals)
    except BaseException:
        _write_status(status_fd, b"spawn_error\n")
        _remain_group_leader()
    returncode = target.wait()
    _write_status(status_fd, ("returncode:%d\n" % returncode).encode("ascii"))
    _remain_group_leader()


if __name__ == "__main__":
    main()
