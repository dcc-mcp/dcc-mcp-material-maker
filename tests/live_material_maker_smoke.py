"""Exercise Material Maker 1.7.0+ and every bundled typed MCP tool."""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from dcc_mcp_material_maker.server import MaterialMakerMcpServer


def post(url: str, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def call(url: str, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    response = post(url, "tools/call", {"name": name, "arguments": arguments or {}})
    result = response.get("result", {})
    if response.get("error") or result.get("isError"):
        raise RuntimeError(json.dumps(response))
    envelope = result.get("structuredContent")
    if envelope is None:
        envelope = json.loads(result["content"][0]["text"])
    job_id = envelope.get("job_id") if isinstance(envelope, dict) else None
    if not job_id:
        return envelope
    deadline = time.monotonic() + 1_800
    while time.monotonic() < deadline:
        poll = post(
            url,
            "tools/call",
            {
                "name": "jobs_get_status",
                "arguments": {"job_id": job_id, "include_result": True},
            },
        )
        poll_result = poll.get("result", {})
        if poll.get("error") or poll_result.get("isError"):
            raise RuntimeError(json.dumps(poll))
        status = poll_result.get("structuredContent")
        if status is None:
            status = json.loads(poll_result["content"][0]["text"])
        if status.get("status") == "completed":
            return status["result"]
        if status.get("status") in {"failed", "cancelled", "interrupted"}:
            raise RuntimeError(json.dumps(status))
        time.sleep(1)
    raise TimeoutError("MCP job %s did not complete within 1800 seconds" % job_id)


def list_tool_names(url: str) -> set[str]:
    names: set[str] = set()
    cursor: Optional[str] = None
    for _page in range(20):
        response = post(url, "tools/list", {"cursor": cursor} if cursor else None)
        if response.get("error"):
            raise RuntimeError(json.dumps(response))
        result = response.get("result", {})
        names.update(item["name"] for item in result.get("tools", []))
        cursor = result.get("nextCursor")
        if not cursor:
            return names
    raise RuntimeError("MCP tools/list exceeded the 20-page smoke-test budget")


def typed_name(names: set[str], base_name: str) -> str:
    return next(name for name in names if name == base_name or name.endswith("__" + base_name))


def main() -> None:
    executable = os.environ.get("DCC_MCP_MATERIAL_MAKER_EXECUTABLE")
    sample_value = os.environ.get("DCC_MCP_MATERIAL_MAKER_SAMPLE")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("DCC_MCP_MATERIAL_MAKER_EXECUTABLE must name the official executable")
    if not sample_value:
        raise RuntimeError("DCC_MCP_MATERIAL_MAKER_SAMPLE must name a real .ptex project")
    sample = Path(sample_value).resolve()
    if not sample.is_file() or sample.suffix.lower() != ".ptex":
        raise RuntimeError("DCC_MCP_MATERIAL_MAKER_SAMPLE must name a real .ptex project")

    with tempfile.TemporaryDirectory(prefix="dcc-mcp-material-maker-live-") as temp_dir:
        root = Path(temp_dir).resolve()
        os.environ["DCC_MCP_MATERIAL_MAKER_ALLOWED_ROOTS"] = os.pathsep.join(
            (str(sample.parent), str(root))
        )
        os.environ["DCC_MCP_DISABLE_DEFAULT_SKILL_PATHS"] = "1"
        server = MaterialMakerMcpServer(port=0, registry_dir=str(root / "registry"))
        try:
            server.register_builtin_actions()
            server.start(install_atexit_hook=False)
            call(server.mcp_url, "load_skill", {"skill_name": "material-maker-materials"})
            names = list_tool_names(server.mcp_url)
            required = {
                name: typed_name(names, name)
                for name in (
                    "get_status",
                    "inspect_project",
                    "validate_project",
                    "export_material",
                )
            }

            status = call(
                server.mcp_url,
                required["get_status"],
                {"probe_project": str(sample)},
            )
            inspected = call(
                server.mcp_url,
                required["inspect_project"],
                {"path": str(sample)},
            )
            validated = call(
                server.mcp_url,
                required["validate_project"],
                {"path": str(sample)},
            )
            exports = {}
            for target in ("Godot", "Blender"):
                output = root / ("export-" + target.lower())
                exports[target] = call(
                    server.mcp_url,
                    required["export_material"],
                    {
                        "project_path": str(sample),
                        "output_directory": str(output),
                        "target": target,
                        "timeout_secs": 1_800,
                    },
                )
        finally:
            server.stop()

        assert status["success"] is True and status["context"]["ready"] is True
        assert inspected["success"] is True and inspected["context"]["valid"] is True
        assert validated["success"] is True and validated["context"]["valid"] is True
        for target, result in exports.items():
            assert result["success"] is True
            context = result["context"]
            assert context["target"] == target
            assert context["file_count"] > 0
            assert context["total_bytes"] > 0
            assert all(item["bytes"] > 0 and len(item["sha256"]) == 64 for item in context["files"])

        print(
            json.dumps(
                {
                    "sample_sha256": inspected["context"]["sha256"],
                    "graphs": inspected["context"]["graph_count"],
                    "nodes": inspected["context"]["node_count"],
                    "connections": inspected["context"]["connection_count"],
                    "exports": {
                        target: {
                            "files": result["context"]["file_count"],
                            "bytes": result["context"]["total_bytes"],
                        }
                        for target, result in exports.items()
                    },
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
