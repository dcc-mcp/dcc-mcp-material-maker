# Install DCC-MCP Material Maker

This adapter is a standalone Python service around Material Maker's documented
command-line exporter. Nothing is copied into the Material Maker application.
The adapter does not install, update, or patch Material Maker.

The adapter wheel is not currently published to PyPI. Do not claim that
`pip install dcc-mcp-material-maker` works until a release wheel is published
and the DCC-MCP Core catalog contains its immutable URL and SHA-256 digest.

The catalog instructions URL for this runbook is:

```text
https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-material-maker/main/install.md
```

## Requirements

- Python 3.9 or newer.
- `dcc-mcp-core` 0.19.38 or newer and earlier than 1.0.0.
- The official Material Maker application, version 1.7 or newer.
- A trusted adapter wheel file named like
  `dcc_mcp_material_maker-<version>-py3-none-any.whl` and its publisher-provided
  SHA-256 digest.
- An absolute path to the Material Maker executable and the installed Material
  Maker release version.

Material Maker's documented command-line contract does not expose a product
version verb. The verify command therefore requires the operator to supply the
release version from the trusted application package or package manager. It
does not infer the product version from the bundled Godot runtime.

## Supported versions

| Surface | Supported contract |
| --- | --- |
| Windows | Python 3.9+ and official Material Maker 1.7+ executable |
| macOS | Python 3.9+ and official Material Maker 1.7+ application executable |
| Linux | Python 3.9+ and official Material Maker 1.7+ executable |
| DCC-MCP Core | `>=0.19.38,<1.0.0` |

Material Maker is user- or OS-package-manager-owned. This adapter never
scrapes a latest release, auto-provisions an external binary, or accepts an
unpinned download on the user's behalf. It does not download or cache Material Maker,
so there is no adapter-owned binary cache to upgrade or clean.

## Agent quick path

1. Stop if an immutable adapter wheel URL and its expected SHA-256 digest are
   not available. The current absence of a PyPI wheel and Core catalog install
   block is an external release dependency, not permission to use an editable
   checkout or scrape a latest artifact.
2. Download the exact wheel through the operator-approved artifact channel and
   verify its SHA-256 digest before installation.
3. Install that local wheel with the same Python interpreter that will run the
   adapter:

   ```text
   python -m pip install ./dcc_mcp_material_maker-<version>-py3-none-any.whl
   ```

4. Run the read-only standard verification command with the exact application
   path and release version:

   ```text
   dcc-mcp-material-maker verify --json --executable <ABSOLUTE_PATH> --material-maker-version <VERSION>
   ```

5. Continue only when the JSON result has `exit_code: 0` and
   `directly_usable: true`. Execute remediation from `next_steps[].command` as
   an argument vector, substitute only the named placeholder, and then rerun
   verify.

The reported endpoint has `kind: native_cli`: it is the exact Material Maker
executable, not a network API. The DCC-MCP service itself uses normal Core
standalone discovery after `dcc-mcp-material-maker` starts.

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

The result must exactly equal the digest published beside the immutable release
artifact. Do not install on a mismatch.

### 2. Install the wheel

```text
python -m pip install ./dcc_mcp_material_maker-<version>-py3-none-any.whl
```

An editable `pip install -e` checkout is a development workflow and is not a
supported installation substitute.

### 3. Configure the runtime

The verification CLI accepts configuration directly, which avoids persistent
shell changes:

```text
dcc-mcp-material-maker doctor --json --executable <ABSOLUTE_PATH> --material-maker-version <VERSION>
```

For a persistent deployment, configure these variables in the service
supervisor or operator-owned environment:

- `DCC_MCP_MATERIAL_MAKER_EXECUTABLE`: exact official executable path.
- `DCC_MCP_MATERIAL_MAKER_VERSION`: installed Material Maker release version.
- `DCC_MCP_MATERIAL_MAKER_ALLOWED_ROOTS`: platform-separated project and
  export roots. Use semicolons on Windows and colons on macOS/Linux.

The deprecated `dcc-mcp-material-maker-install` alias performs the same
read-only doctor operation. It never installs files, changes configuration, or
writes persistent state. New automation must use
`dcc-mcp-material-maker doctor --json` or
`dcc-mcp-material-maker verify --json`.

## Verify

Run full verify after installation, configuration changes, Material Maker
upgrades, or Core upgrades:

```text
dcc-mcp-material-maker verify --json
```

The JSON result reports the adapter and Core versions, Python runtime, native
CLI endpoint source, allowed-root configuration, Material Maker 1.7 floor,
bounded readiness probe, failure stage/reason, and executable next steps.

Stable exit codes:

| Exit | Meaning |
| ---: | --- |
| `0` | All prerequisites and the bounded native readiness probe passed. |
| `10` | Preflight failed, such as a missing executable or unsupported Core. |
| `40` | Verification failed, such as an unknown/old Material Maker version or rejected native probe. |

`directly_usable: true` is emitted only with exit 0. A discovered executable
alone is not readiness.

## Upgrade

1. Obtain the desired immutable adapter wheel and its published SHA-256 digest.
2. Verify the digest as described above.
3. Upgrade from that exact local artifact:

   ```text
   python -m pip install --upgrade ./dcc_mcp_material_maker-<version>-py3-none-any.whl
   ```

4. If Material Maker is upgraded separately by the user or OS package manager,
   update the configured release version and executable path.
5. Run `dcc-mcp-material-maker verify --json` and require exit 0.

There is no adapter-owned Material Maker cache to migrate or delete.

## Uninstall

1. Stop any running `dcc-mcp-material-maker` service process through the
   operator or service supervisor that started it.
2. Remove the adapter wheel:

   ```text
   python -m pip uninstall dcc-mcp-material-maker
   ```

3. Remove the three `DCC_MCP_MATERIAL_MAKER_*` configuration variables above
   from the operator-owned environment if they are no longer used.

Uninstalling this adapter does not uninstall Material Maker and does not remove
user projects or exports. There is no adapter binary cache to clean.

## Troubleshooting

### `material_maker_not_found` / exit 10

Pass `--executable` with the exact official application path or configure
`DCC_MCP_MATERIAL_MAKER_EXECUTABLE`. The adapter does not download the
application.

### `core_version_unsupported` / exit 10

Use a supported `dcc-mcp-core>=0.19.38,<1.0.0` in the same interpreter as the
adapter, then rerun verify. Do not change the interpreter implicitly.

### `material_maker_version_unknown` / exit 40

Read the product release from the trusted Material Maker package or package
manager and pass `--material-maker-version`. Godot's engine version is not the
Material Maker product version.

### `material_maker_version_unsupported` / exit 40

The reported product version is earlier than 1.7. Upgrade Material Maker
through its user- or OS-managed installation path and rerun verify with the new
version.

### `native_probe_failed` / exit 40

Run the same executable manually using Material Maker's documented
`--export-material` contract, confirm it can start headlessly, and inspect its
own diagnostics. Check OS execution permission and application dependencies;
do not fall back to UI automation or arbitrary scripts.

### Wrong endpoint or configuration

Inspect `endpoint`, `config`, and `runtime` in the doctor JSON. Command-line
values take precedence over environment discovery. Confirm the Python
interpreter and allowed roots belong to the intended deployment.

### Wheel or catalog unavailable

The wheel is not currently published to PyPI, and the Core catalog still needs
an immutable `install:` block with URL and SHA-256. Until those external release
steps are complete, use only an operator-provided, digest-verified wheel; do not
claim that the public pip command is usable.
