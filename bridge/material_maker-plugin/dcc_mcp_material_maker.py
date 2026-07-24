#!/usr/bin/env python3
"""Loopback bridge contract for Material Maker integrations."""

import json
import os
import socketserver

PORT = int(os.environ.get("DCC_MCP_MATERIAL_MAKER_BRIDGE_PORT", "3848"))


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        method = request.get("method")
        if method in {"material_maker.get_status", "material_maker.ping"}:
            result = {"ready": True, "bridge_port": PORT}
        elif method in {"material_maker.list_images", "material_maker.list_materials"}:
            result = []
        elif method in {"material_maker.get_active_image", "material_maker.get_active_material"}:
            result = None
        else:
            result = {"error": f"Unsupported Material Maker bridge method: {method}"}
        self.wfile.write((json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}) + "\n").encode())


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(("127.0.0.1", PORT), Handler) as server:
        server.serve_forever()
