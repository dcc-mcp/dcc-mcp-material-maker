"""Shared declarative Skill entry points for Material Maker automation."""

from __future__ import annotations

from typing import Any, Callable

from dcc_mcp_core.skill import skill_entry, skill_success

from .bridge import get_bridge


def bridge_main(method: str, message: str) -> Callable[..., dict[str, Any]]:
    @skill_entry
    def main(**kwargs: Any) -> dict[str, Any]:
        result = getattr(get_bridge(), method)(**kwargs)
        return skill_success(message, **result)

    return main
