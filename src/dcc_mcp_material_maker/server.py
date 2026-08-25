"""Standalone DCC-MCP composition root for Material Maker CLI automation."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from dcc_mcp_core import DccServerOptions, MinimalModeConfig
from dcc_mcp_core.server_base import DccServerBase

from .__version__ import __version__

logger = logging.getLogger(__name__)
SERVER_NAME = "dcc-mcp-material-maker"
_DCC_NAME = "material_maker"
_BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_server: Optional["MaterialMakerMcpServer"] = None


class MaterialMakerMcpServer(DccServerBase):
    """Standalone adapter using Material Maker's documented CLI export path."""

    def __init__(
        self,
        port: Optional[int] = None,
        extra_skill_paths: Optional[list[str]] = None,
        gateway_port: Optional[int] = None,
        registry_dir: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        os.environ.setdefault("DCC_MCP_PYTHON_EXECUTABLE", sys.executable)
        self._extra_skill_paths = list(extra_skill_paths or [])
        options = DccServerOptions.from_env(
            _DCC_NAME,
            _BUILTIN_SKILLS_DIR,
            port=port,
            server_name=SERVER_NAME,
            server_version=__version__,
            adapter_version=__version__,
            dcc_version=__version__,
            instance_type="standalone",
            standalone_main_thread=False,
            gateway_port=gateway_port,
            registry_dir=registry_dir,
            **kwargs,
        )
        super().__init__(options=options)

    def _version_string(self) -> str:
        return __version__

    @property
    def port(self) -> int:
        if self._handle is not None:
            return int(self._handle.port)
        return int(self._options.port)

    @property
    def mcp_url(self) -> str:
        return "http://127.0.0.1:{}/mcp".format(self.port)

    def register_builtin_actions(
        self,
        extra_skill_paths: Optional[list[str]] = None,
        include_bundled: bool = True,
        minimal_mode: Optional[MinimalModeConfig] = None,
    ) -> None:
        if minimal_mode is None:
            minimal_mode = MinimalModeConfig(skills=("material-maker-materials",))
        paths = list(self._extra_skill_paths)
        if extra_skill_paths:
            paths.extend(extra_skill_paths)
        super().register_builtin_actions(
            extra_skill_paths=paths,
            include_bundled=include_bundled,
            minimal_mode=minimal_mode,
        )


def start_server(
    port: Optional[int] = None,
    extra_skill_paths: Optional[list[str]] = None,
    gateway_port: Optional[int] = None,
    registry_dir: Optional[str] = None,
    **kwargs: Any,
) -> MaterialMakerMcpServer:
    global _server
    if _server is None:
        server = MaterialMakerMcpServer(
            port=port,
            extra_skill_paths=extra_skill_paths,
            gateway_port=gateway_port,
            registry_dir=registry_dir,
            **kwargs,
        )
        server.register_builtin_actions()
        server.start()
        _server = server
        logger.info("Material Maker MCP server started")
    return _server


def stop_server() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def main(argv: Optional[list[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    lifecycle_commands = {
        "install",
        "status",
        "verify",
        "uninstall",
        "upgrade",
        "doctor",
        "configure",
    }
    lifecycle_flags = {"--json", "--yes", "--dry-run", "--execute", "--dcc-path", "--python"}
    if args and (args[0] in lifecycle_commands or any(item in lifecycle_flags for item in args)):
        from .install import main as diagnostic_main

        diagnostic_main(args, program="dcc-mcp-material-maker")
        return
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    start_server()
    try:
        stopped.wait()
    finally:
        stop_server()


if __name__ == "__main__":
    main()
