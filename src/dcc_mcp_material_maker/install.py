"""Compatibility entry point that verifies the official Material Maker CLI."""

from __future__ import annotations

import json

from .bridge import MaterialMakerCli


def main() -> None:
    """Print a machine-readable runtime check; no fake plug-in is installed."""
    status = MaterialMakerCli.from_env().status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    if not status.get("ready"):
        raise SystemExit(1)
