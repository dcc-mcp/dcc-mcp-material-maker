---
name: material-maker-session
description: >-
  Inspect the connected Material Maker session through the DCC-MCP Python plug-in
  bridge. Use for session health, open images, and active image metadata.
license: MIT
compatibility: "Material Maker.0+; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: material_maker
    layer: domain
    version: "0.1.0"
    search-hint: "MATERIAL MAKER image editor session document active image layers"
    tags: "material_maker,image-editing,session"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# MATERIAL MAKER Session

Install and run the bundled Material Maker plug-in before using this skill. Calls use a
loopback JSON-lines bridge and never execute arbitrary MATERIAL MAKER/Python source.
