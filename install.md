# Install DCC-MCP Material Maker

This adapter wraps Material Maker's documented command-line exporter. Its
Install SOP manages only a dedicated adapter configuration and receipt. It
never installs, updates, patches, or removes Material Maker itself.

The adapter wheel is not currently published to PyPI. Do not claim that
`pip install dcc-mcp-material-maker` works until a release wheel is published
and the DCC-MCP Core catalog contains its immutable URL and SHA-256 digest.
The canonical runbook URL is:

```text
https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-material-maker/main/install.md
```

## Requirements

- Python 3.9 or newer.
- `dcc-mcp-core>=0.20.14,<1.0.0`.
- An official Material Maker final release 1.7.0 or newer.
- A trusted adapter wheel named
  `dcc_mcp_material_maker-<version>-py3-none-any.whl` and its published
  SHA-256 digest.
- Absolute paths to the Material Maker executable and a trusted `.ptex`
  readiness project.

Material Maker has no documented product-version verb. Supply the canonical
`X.Y.Z` release from the trusted package or package manager. Prerelease,
local, prefixed, whitespace-padded, and oversized version strings fail closed.

## Supported versions

| Surface | Supported contract |
| --- | --- |
| Windows | Python 3.9+ and official Material Maker 1.7.0+ executable |
| macOS | Python 3.9+ and official Material Maker 1.7.0+ executable |
| Linux | Python 3.9+ and official Material Maker 1.7.0+ executable |
| DCC-MCP Core | `>=0.20.14,<1.0.0` |

Material Maker stays user- or OS-package-manager-owned. The adapter does not download or cache Material Maker, scrape a latest release, accept an unpinned
artifact, execute arbitrary scripts, or fall back to UI automation.

The Install SOP v1 JSON schema is loaded from the canonical resource published
by `dcc-mcp-core` 0.20.14 or newer. Source and installed-wheel tests validate
the exact public loader and resource; the adapter carries no fallback copy.

## Agent quick path

1. Obtain the exact wheel through an operator-approved channel and verify its
   published SHA-256 before installing it. The absent PyPI/catalog release is
   not permission to use an editable checkout or an unaudited download.
2. Install the local wheel with the adapter interpreter:

   ```text
   python -m pip install ./dcc_mcp_material_maker-<version>-py3-none-any.whl
   ```

3. Plan managed activation. Replace the examples below with concrete absolute
   paths before running them:

   ```text
   dcc-mcp-material-maker install --json --install-root ABSOLUTE_MANAGED_ROOT --executable ABSOLUTE_MATERIAL_MAKER --material-maker-version 1.7.0 --probe-project ABSOLUTE_PROBE_PROJECT.ptex
   ```

4. Review the schema-valid result and execute only its exact
   `next_steps[].command` argument vector. The emitted vector contains no
   placeholders or shell-sensitive version expressions.
5. Require `status: ok`, `exit_code: 0`, and
   `verify.directly_usable: true` after execution.

Mutating commands are plan-first: omitting `--execute` performs no persistent
writes. The deprecated `dcc-mcp-material-maker-install` alias remains a
verification-only doctor alias and never activates or removes managed state.

## Manual path

### 1. Verify the wheel digest

Windows PowerShell:

```powershell
Get-FileHash .\dcc_mcp_material_maker-<version>-py3-none-any.whl -Algorithm SHA256
```

macOS or Linux:

```bash
sha256sum ./dcc_mcp_material_maker-<version>-py3-none-any.whl
```

The result must exactly equal the publisher's immutable digest.

### 2. Install the wheel

```text
python -m pip install ./dcc_mcp_material_maker-<version>-py3-none-any.whl
```

An editable checkout is a development workflow, not a release installation.

### 3. Plan and execute managed activation

`install` writes a deterministic `adapter.json` only inside the dedicated
install root. Its receipt is:

```text
MANAGED_ROOT/.dcc-mcp/receipts/material-maker.json
```

The receipt binds the resolved install root and records the byte count and
SHA-256 for every adapter-owned file. Existing unowned, moved, copied,
malformed, missing, or tampered state fails closed. A different configuration
requires `upgrade`; `install` never replaces it implicitly.

After reviewing the plan, rerun the exact emitted command with `--execute`.
Install and upgrade publish a complete staged state by atomic directory
replacement. A publish failure restores the prior verified state.

Optional environment inputs are:

- `DCC_MCP_MATERIAL_MAKER_INSTALL_ROOT`
- `DCC_MCP_MATERIAL_MAKER_EXECUTABLE`
- `DCC_MCP_MATERIAL_MAKER_VERSION`
- `DCC_MCP_MATERIAL_MAKER_PROBE_PROJECT`
- `DCC_MCP_MATERIAL_MAKER_ALLOWED_ROOTS` for normal project/export tools

Explicit command arguments take precedence over environment values and the
managed receipt.

## Verify

`status` verifies only the receipt/root binding and every owned-file digest:

```text
dcc-mcp-material-maker status --json --install-root ABSOLUTE_MANAGED_ROOT
```

`verify` additionally loads the configured `.ptex` through the official
Material Maker CLI and performs a transient export next to that project. The
temporary output is removed. Readiness requires validated nonempty export
artifacts within the adapter's file-count and byte limits; process exit zero by
itself is never readiness.

```text
dcc-mcp-material-maker verify --json --install-root ABSOLUTE_MANAGED_ROOT
```

`doctor` is the read-only compatibility spelling of `verify`. It can verify
explicit arguments without a receipt, but it never writes managed state.

Stable exits follow the shared Install SOP:

| Exit | Meaning |
| ---: | --- |
| `0` | Plan completed, status passed, or verified operation succeeded. |
| `10` | Preflight failed. |
| `20` | Artifact acquisition failed; reserved because this adapter does not download. |
| `30` | Managed-state transaction or receipt prerequisite failed. |
| `40` | Receipt integrity or bounded host verification failed. |
| `50` | Restart is required; reserved for shared compatibility. |

All results include integer `schema_version: 1`, adapter/Core/DCC identity,
`steps`, `receipt_path`, `verify`, and executable `next_steps`.

## Upgrade

Supply the desired concrete configuration to `upgrade`, first without and
then with `--execute`. Upgrade requires an intact existing receipt and uses
the same staged replacement and rollback contract as install.

Wheel replacement remains a separate, digest-verified operation:

```text
python -m pip install --upgrade ./dcc_mcp_material_maker-<version>-py3-none-any.whl
```

Run `verify` after an adapter, Core, Material Maker, executable, or probe
project change.

## Uninstall

Plan removal:

```text
dcc-mcp-material-maker uninstall --json --install-root ABSOLUTE_MANAGED_ROOT
```

Execute only the exact returned command. Uninstall first verifies the receipt,
root identity, complete owned-file set, byte counts, and SHA-256 values. It
then atomically moves the dedicated root before removal and restores it if
removal fails. It never removes Material Maker, projects, exports, an unrelated
file, or an unverified root.

Remove the Python wheel separately through the interpreter that owns it:

```text
python -m pip uninstall dcc-mcp-material-maker
```

## Troubleshooting

### `material_maker_not_found` / exit 10

Pass the exact official executable or configure its environment variable. The
adapter does not download a replacement.

### `core_version_unsupported` / exit 10

Use a supported Core release in the same interpreter. Do not silently change
interpreters.

### `material_maker_version_invalid` or `unsupported` / exit 40

Use the trusted canonical final `X.Y.Z` product release. Godot's engine
version is not Material Maker's product version. The emitted `configure`
command prompts on stderr for that non-secret operator attestation and then
immediately reruns the bounded readiness check; it never invents a release.

### `probe_project_required` / exit 40

Select a trusted bounded `.ptex` project that Material Maker can load and
export without modifying the source. Do not replace this gate with an empty
process launch, arbitrary script, or UI action. The emitted `configure`
command collects the exact path and then advances directly to the native
readiness export without persisting an unverified placeholder.

### `receipt_integrity_failed` or `receipt_root_mismatch`

Do not reuse, relocate, or hand-edit a receipt. Restore the exact verified
state or use a new dedicated install root. Tampered state is never replaced or
removed automatically.

### `native_probe_failed` / exit 40

Diagnose the same official executable and `.ptex` input. The JSON result
exposes only a stable exception type, not host exception text or arbitrary
process output.

### Wheel or catalog unavailable

The wheel is not published to PyPI and the Core catalog still needs an
immutable install URL and SHA-256. Use only an operator-provided,
digest-verified wheel until those release steps are complete.
