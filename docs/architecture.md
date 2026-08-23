# Architecture

## Boundary

`dcc-mcp-material-maker` is a standalone DCC-MCP adapter. Material Maker does
not expose a supported Python plug-in or remote-control bridge for this use
case, so the adapter composes two deliberately small capabilities:

1. a pure Python, read-only `.ptex` JSON inspector and structural validator;
2. a bounded wrapper around Material Maker's documented `--export-material`
   command-line path.

It does not claim interactive graph editing, image preview control, or arbitrary
script execution.

## Request flow

```text
MCP client
  -> dcc-mcp-core typed Skill dispatcher
    -> MaterialMakerCli
      -> allowed-root and schema validation
        -> read-only PTEX inspection
        -> fixed native CLI export command
          -> private staging directory
            -> bounded artifact inventory and SHA-256
              -> atomic rename to a new destination
```

The Skill exposes four monolithic asynchronous tools. Jobs remain cancellable
through Core while the subprocess runner polls the native process.

## Native command contract

Exports use only the documented shape:

```text
material_maker --headless --export-material \
  --target <Blender|Godot|Unity|Unreal> \
  --output-dir <private-staging-directory> \
  --output-file <safe-template> \
  <validated-project.ptex>
```

The target is an enum and the output filename is restricted to a filename
template without path separators or traversal. The adapter does not expose
Material Maker's general argument surface.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DCC_MCP_MATERIAL_MAKER_EXECUTABLE` | auto-discovery | Exact official executable path. |
| `DCC_MCP_MATERIAL_MAKER_ALLOWED_ROOTS` | current directory | Platform-separated read/write roots. |
| `DCC_MCP_MATERIAL_MAKER_MAX_PROJECT_BYTES` | 64 MiB | Maximum input project size. |
| `DCC_MCP_MATERIAL_MAKER_MAX_NODES` | 50,000 | Maximum graph nodes. |
| `DCC_MCP_MATERIAL_MAKER_MAX_CONNECTIONS` | 200,000 | Maximum graph connections. |
| `DCC_MCP_MATERIAL_MAKER_MAX_EXPORT_FILES` | 512 | Maximum exported regular files. |
| `DCC_MCP_MATERIAL_MAKER_MAX_EXPORT_BYTES` | 2 GiB | Maximum aggregate export size. |
| `DCC_MCP_MATERIAL_MAKER_MAX_TIMEOUT_SECS` | 1,800 | Maximum native process deadline. |
| `DCC_MCP_MATERIAL_MAKER_VERSION` | unset | Operator-supplied product release required for verify-to-usable; the native CLI has no documented product-version verb. |

## Failure semantics

Invalid paths, malformed JSON, invalid graph connections, unsupported targets,
existing output directories, native non-zero exits, native error diagnostics,
timeouts, links, empty exports, and configured limit violations fail the typed
job. Partial staging data is removed, and the requested destination remains
absent.

The read-only doctor/verify surface separately fails closed when the executable
or Core prerequisite is missing, the supplied Material Maker version is unknown
or earlier than 1.7, or the bounded native readiness probe fails. It never
infers the Material Maker product release from Godot's engine version.

## Non-goals

- installing or patching Material Maker;
- controlling the visible editor or editing graphs interactively;
- evaluating caller-supplied GDScript, GLSL, Python, or shell commands;
- importing exported textures into Blender or Godot on the caller's behalf.

Blender/Godot import validation belongs to their own DCC-MCP adapters and is a
separate cross-DCC acceptance step.
