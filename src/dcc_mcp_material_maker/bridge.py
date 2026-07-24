"""Small JSON-lines client for the MaterialMaker plug-in bridge."""

from __future__ import annotations

import json
import os
import socket
from typing import Any


class MaterialMakerBridgeError(RuntimeError):
    """Raised when the MATERIAL_MAKER plug-in bridge is unavailable or rejects a call."""


class MaterialMakerBridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 3848, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "MaterialMakerBridge":
        return cls(
            host=os.environ.get("DCC_MCP_MATERIAL_MAKER_BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("DCC_MCP_MATERIAL_MAKER_BRIDGE_PORT", "3848")),
            timeout=float(os.environ.get("DCC_MCP_MATERIAL_MAKER_BRIDGE_TIMEOUT", "10")),
        )

    def call(self, method: str, **params: Any) -> Any:
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ) as connection:
                connection.sendall((request + "\n").encode("utf-8"))
                response = connection.makefile("r", encoding="utf-8").readline()
        except OSError as exc:
            raise MaterialMakerBridgeError(
                f"MATERIAL_MAKER bridge unavailable at {self.host}:{self.port}; "
                "install and run the MaterialMaker plug-in"
            ) from exc
        if not response:
            raise MaterialMakerBridgeError(
                "MATERIAL_MAKER bridge closed the connection without a response"
            )
        payload = json.loads(response)
        if "error" in payload:
            raise MaterialMakerBridgeError(str(payload["error"]))
        return payload.get("result")


def get_bridge() -> MaterialMakerBridge:
    return MaterialMakerBridge.from_env()
