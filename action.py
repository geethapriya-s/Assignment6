"""Pure stdio MCP tool dispatcher — zero LLM dependencies."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp import ClientSession

from artifacts import ArtifactStore
from schemas import ToolCall

_TRUNCATE_BYTES = 4096
_TOOL_TIMEOUT_SEC: dict[str, int] = {
    "fetch_url": 35,
    "web_search": 45,
}
_DEFAULT_TIMEOUT_SEC = 60
_store = ArtifactStore()


def _value_has_art_handle(value: Any) -> bool:
    if isinstance(value, str):
        return value == "art:" or value.startswith("art:")
    if isinstance(value, dict):
        return any(_value_has_art_handle(v) for v in value.values())
    if isinstance(value, list):
        return any(_value_has_art_handle(v) for v in value)
    return False


def _extract_tool_text(result: Any) -> str:
    content = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(str(block))
    if parts:
        return "\n".join(parts)
    if hasattr(result, "structuredContent") and result.structuredContent:
        return json.dumps(result.structuredContent, indent=2)
    return str(result)


def _missing_required_fields(
    tool_call: ToolCall, mcp_tools: list[dict[str, Any]] | None
) -> list[str]:
    if not mcp_tools:
        return []
    spec = next((t for t in mcp_tools if t.get("name") == tool_call.name), None)
    if not spec:
        return []
    schema = spec.get("input_schema") or {}
    missing: list[str] = []
    for field in schema.get("required") or []:
        value = tool_call.arguments.get(field)
        if value is None:
            missing.append(field)
        elif isinstance(value, str) and not value.strip():
            missing.append(field)
    return missing


async def execute(
    session: ClientSession,
    tool_call: ToolCall,
    mcp_tools: list[dict[str, Any]] | None = None,
) -> tuple[str, str | None]:
    if _value_has_art_handle(tool_call.arguments):
        return (
            "BLOCKED: Do not pass internal art: artifact handles into MCP tool arguments. "
            "Attached artifact bytes are already visible in the ATTACHED ARTIFACTS context segment.",
            None,
        )
    missing = _missing_required_fields(tool_call, mcp_tools)
    if missing:
        return (
            f"TOOL_ERROR [{tool_call.name}]: missing required argument(s): {', '.join(missing)}. "
            f"Provided: {json.dumps(tool_call.arguments)}. "
            "Supply all required fields before calling MCP (e.g. fetch_url requires url).",
            None,
        )
    timeout_sec = _TOOL_TIMEOUT_SEC.get(tool_call.name, _DEFAULT_TIMEOUT_SEC)
    try:
        result = await asyncio.wait_for(
            session.call_tool(tool_call.name, tool_call.arguments),
            timeout=timeout_sec,
        )
        text = _extract_tool_text(result)
    except asyncio.TimeoutError:
        hint = (
            "Use web_search and answer from snippets."
            if tool_call.name == "fetch_url"
            else "Retry or answer from existing history."
        )
        return (
            f"TOOL_ERROR [{tool_call.name}]: timed out after {timeout_sec}s. {hint}",
            None,
        )
    except Exception as exc:
        return f"TOOL_ERROR [{tool_call.name}]: {exc}", None

    payload = text.encode("utf-8")
    if len(payload) > _TRUNCATE_BYTES:
        artifact_id = _store.put(
            payload,
            content_type="text/plain; charset=utf-8",
            source=f"mcp:{tool_call.name}",
            descriptor=f"Truncated output from {tool_call.name} ({len(payload)} bytes)",
        )
        descriptor = (
            f"Tool output exceeded 4KB; stored as artifact {artifact_id}. "
            f"Preview: {text[:400]}..."
        )
        return descriptor, artifact_id
    return text, None
