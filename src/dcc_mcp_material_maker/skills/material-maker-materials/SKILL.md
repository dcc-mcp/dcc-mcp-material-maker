---
name: material-maker-materials
description: >-
  Inspect and validate bounded Material Maker PTEX graphs, then export their
  procedural materials through Material Maker's documented command-line path
  for Blender, Godot, Unity, or Unreal pipelines. Do not use for interactive
  Material Maker GUI control or arbitrary shader execution.
license: MIT
compatibility: "Python 3.9+; Material Maker 1.7.0+; dcc-mcp-core 0.20.14+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: material_maker
    layer: domain
    version: "0.4.1"  # x-release-please-version
    search-hint: "Material Maker PTEX procedural material graph export Blender Godot Unity Unreal"
    tags: [material-maker, ptex, procedural-materials, game-dev, export]
    tools: tools.yaml
---

# Material Maker Materials

Use this Skill for deterministic `.ptex` inspection, structural validation, and
native material export. The adapter runs as a standalone service and calls
Material Maker's documented `--export-material` interface. It accepts typed
data only and never evaluates caller-provided GDScript, GLSL, or shell input.

Call `get_status` with a trusted `probe_project`. Readiness requires loading
that bounded `.ptex` and producing nonempty transient export artifacts; an
executable path or process exit zero alone is insufficient.

Keep projects and export destinations under
`DCC_MCP_MATERIAL_MAKER_ALLOWED_ROOTS`. Export to a new directory, inspect the
returned file inventory and hashes, then hand the artifacts to the target DCC
or engine for its own import validation.

This Skill does not claim interactive graph editing. Use DCC UI Control only
when the user explicitly needs the visible Material Maker editor and no typed
operation can satisfy the request.
