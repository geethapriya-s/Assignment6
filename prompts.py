"""Load and render LLM prompts from prompts.yaml."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROMPTS_PATH = Path(__file__).resolve().parent / "prompts.yaml"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    data = yaml.safe_load(PROMPTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid prompts file: {PROMPTS_PATH}")
    return data


def _get(path: str) -> Any:
    node: Any = _load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"Prompt not found: {path}")
        node = node[part]
    return node


def _escape_format_value(value: str) -> str:
    """Prevent user/tool text with braces from breaking str.format templates."""
    return value.replace("{", "{{").replace("}", "}}")


def render(path: str, **kwargs: str) -> str:
    """Render a prompt template from prompts.yaml using str.format."""
    template = _get(path)
    if not isinstance(template, str):
        raise TypeError(f"Prompt at {path} is not a string")
    safe = {key: _escape_format_value(str(val)) for key, val in kwargs.items()}
    return template.format(**safe).strip()


def reload_prompts() -> None:
    """Clear cached prompts after editing prompts.yaml."""
    _load.cache_clear()
