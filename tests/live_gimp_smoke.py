"""Exercise the real MATERIAL_MAKER plug-in bridge and MCP tools."""

from __future__ import annotations

import json
import time
import urllib.request

from dcc_mcp_material_maker.bridge import MaterialMakerBridge
from dcc_mcp_material_maker.server import MaterialMakerMcpServer


def post(url: str, method: str, params=None):
    request = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def call(url: str, name: str, arguments=None):
    response = post(url, "tools/call", {"name": name, "arguments": arguments or {}})
    result = response.get("result", {})
    if response.get("error") or result.get("isError"):
        raise RuntimeError(json.dumps(response))
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    return json.loads(result["content"][0]["text"])


def main() -> None:
    bridge = MaterialMakerBridge.from_env()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            status = bridge.call("material_maker.get_status")
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise RuntimeError("MATERIAL_MAKER plug-in bridge did not become ready")

    server = MaterialMakerMcpServer(port=0)
    try:
        server.register_builtin_actions()
        server.start(install_atexit_hook=False)
        url = server.mcp_url
        call(url, "load_skill", {"skill_name": "material_maker-session"})
        listed = post(url, "tools/list", {})["result"]["tools"]
        names = {item["name"] for item in listed}
        assert any(name.endswith("__get_status") for name in names)
        assert call(url, next(name for name in names if name.endswith("__get_status")))["ready"]
        images = call(url, next(name for name in names if name.endswith("__list_images")))
        assert isinstance(images.get("images"), list)
        print(json.dumps({"status": status, "images": images}))
    finally:
        server.stop()


if __name__ == "__main__":
    main()
