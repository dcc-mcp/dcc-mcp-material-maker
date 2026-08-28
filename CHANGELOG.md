# Changelog

## [0.4.1](https://github.com/dcc-mcp/dcc-mcp-material-maker/compare/v0.4.0...v0.4.1) (2026-08-28)


### Bug Fixes

* **ci:** bind release publication identity ([0f87644](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/0f876444f7092bdc755b750061d6c19d0014d3bb))
* **ci:** harden release archive recovery ([ac49227](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/ac492279d5500261d01022793855105c6c20d6fb))
* **ci:** make release publication fail closed ([0b81e65](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/0b81e6506f13c63d81d5a4a4695cb792c7925830))
* enforce canonical wheel metadata ([2ce41dc](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/2ce41dc120d0bd6cb3fb7db37f15ed8c5e12546e))
* harden release archive and rollback guards ([35f404e](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/35f404e3525e3728a3bf15ab1c2594fba856e1b2))
* harden release publication provenance ([82592a0](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/82592a010b41cf90ddfbd92e3f3dc29bdc736d0b))
* **release:** enforce final publication integrity ([46d57c7](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/46d57c787c88f31f3b9f9fe442fdf1239cc2e684))
* validate wheel data descriptors ([c576ab1](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/c576ab12636b640255fe149efa54ef2c1ebfa600))
* validate ZIP64 data descriptors ([70ca725](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/70ca72566a5f41ba7615760b77cfcb386673ae06))

## [0.4.0](https://github.com/dcc-mcp/dcc-mcp-material-maker/compare/v0.3.1...v0.4.0) (2026-08-25)


### Features

* add Material Maker install verification ([3b7bbbd](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/3b7bbbd7f5eac87ff817cb8d5e95e1fbff6668d6))
* complete material maker install sop ([3ae5174](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/3ae51747f5a2cc7c1c9840e3153e8cddf7c3e918))


### Bug Fixes

* complete Material Maker lifecycle remediation ([bf0ddba](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/bf0ddba26946ee96a46cf1d930b06a01e945d8a6))
* harden Material Maker install lifecycle ([928e011](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/928e011e394395445016eb573a1bd14bd92960e1))
* harden Material Maker lifecycle transactions ([27fb203](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/27fb2039cca147399ec9009a5477cbe355fc48ab))
* use released install SOP schema ([53bf104](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/53bf10463fccb8dd56c8a73edf8b4df399992b31))

## [0.3.1](https://github.com/dcc-mcp/dcc-mcp-material-maker/compare/v0.3.0...v0.3.1) (2026-08-13)


### Bug Fixes

* **release:** retain distributions on GitHub releases ([#4](https://github.com/dcc-mcp/dcc-mcp-material-maker/issues/4)) ([2cead3a](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/2cead3a077b0ebfb67b3c4439a0f58a0be772fcf))

## [0.3.0](https://github.com/dcc-mcp/dcc-mcp-material-maker/compare/v0.2.0...v0.3.0) (2026-08-12)


### Features

* ship production-ready Material Maker workflows ([#2](https://github.com/dcc-mcp/dcc-mcp-material-maker/issues/2)) ([7dce05f](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/7dce05fff851c705f6a5f74a44b0ee68d2fe2aa9))

## 0.3.0

- Replace the legacy session placeholder with a standalone Material Maker adapter.
- Add bounded PTEX inspection and structural validation.
- Add staged native export for documented Blender, Godot, Unity, and Unreal targets.
- Add four typed Skill tools, Python 3.9/3.12 CI, packaging checks, and architecture docs.

## [0.2.0](https://github.com/dcc-mcp/dcc-mcp-material-maker/compare/v0.1.0...v0.2.0) (2026-07-24)


### Features

* add DCC MCP adapter ([eb67a95](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/eb67a9536f171832535b2b10b906782362c919c3))
* add GIMP MCP adapter ([4fcda51](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/4fcda51b29d551e59175a81fa4fafcea5d2e8252))


### Bug Fixes

* keep persistent GIMP bridge process alive ([441aa0c](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/441aa0c8d7a8c2bf3178457e931ec2835115061c))
* match GIMP plugin folder to module name ([63799cb](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/63799cbf1c60e53c7c56367b01b54d6290af76fb))
* use GIMP persistent procedure callback signature ([9489a4f](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/9489a4f452b5d0bd947a3108b761ba26b0256aff))
* verify GIMP AppImage checksum ([573314b](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/573314bc3bf1daf6e1e10ea722d03f66927a2eaa))


### Documentation

* optimize workflow showcase ([02432f9](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/02432f9bc9934d43e703d1422dd0ee0755f3f076))
* redesign DCC-MCP brand visuals ([8c6f06e](https://github.com/dcc-mcp/dcc-mcp-material-maker/commit/8c6f06eeed0dd35cff0362a4b4f43161bf2e5ae1))

## 0.1.0

- Initial Material Maker session bridge and MCP adapter.
