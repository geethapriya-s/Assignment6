"""Context-isolated decision selector — answer or tool call (LLM-only)."""
from __future__ import annotations

import json
from typing import Any

from action import _missing_required_fields
from llm_gatewayV3.client import LLM
from prompts import render
from schemas import DecisionOutput, Goal, MemoryItem, ToolCall

_llm = LLM(base_url="http://localhost:8101")


class DecisionError(RuntimeError):
    """Raised when the decision LLM does not return a valid DecisionOutput."""


def _tool_spec(name: str, mcp_tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((t for t in mcp_tools if t.get("name") == name), None)


def _hits_block(hits: list[MemoryItem]) -> str:
    if not hits:
        return "(no memory hits)"
    return "\n".join(
        f"- {h.descriptor} (kind={h.kind}, artifact={h.artifact_id or 'none'})"
        for h in hits
    )


def _attached_block(attached: dict[str, Any] | None) -> str:
    if not attached:
        return "(none)"
    meta = attached.get("meta")
    preview = attached.get("preview", "")
    return (
        f"artifact_id={attached.get('artifact_id')}\n"
        f"content_type={getattr(meta, 'content_type', 'unknown')}\n"
        f"descriptor={getattr(meta, 'descriptor', '')}\n"
        f"PREVIEW:\n{preview[:6000]}"
    )


def _tools_block(mcp_tools: list[dict[str, Any]]) -> str:
    if not mcp_tools:
        return "(no tools)"
    lines: list[str] = []
    for tool in mcp_tools:
        name = tool.get("name", "")
        schema = tool.get("input_schema") or {}
        required = schema.get("required") or []
        props = schema.get("properties") or {}
        req_parts = []
        for field in required:
            ptype = props.get(field, {}).get("type", "any")
            req_parts.append(f"{field}:{ptype}")
        req_label = ", ".join(req_parts) if req_parts else "none"
        desc = (tool.get("description") or "")[:160]
        lines.append(f"- {name}({req_label}) — {desc}")
    lines.append(render("decision.tools_rules_footer"))
    return "\n".join(lines)


def _first_https_in_text(text: str) -> str | None:
    for token in text.split():
        cleaned = token.strip(".,;()[]\"'")
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            return cleaned
    return None


def _clean_str(value: Any) -> str:
    s = str(value).strip()
    for _ in range(5):
        nxt = s.lstrip(":\"' ").strip()
        if nxt == s:
            break
        s = nxt
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def _is_valid_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _compact_search_query(goal_text: str, raw: str, user_query: str) -> str:
    q = _clean_str(raw)
    goal = goal_text.strip()
    user = user_query.strip()
    if len(q.split()) > 10 or (goal and q == goal):
        q = user or goal
    if len(q.split()) > 15:
        q = " ".join(q.split()[:15])
    return q or goal or user


def _sanitize_tool_call(tool_call: ToolCall, query: str, goal_text: str) -> ToolCall:
    """Normalize malformed strings from structured LLM output (not tool selection)."""
    args = dict(tool_call.arguments)
    context = f"{query} {goal_text}"

    if tool_call.name == "fetch_url":
        url = _clean_str(args.get("url", ""))
        if not _is_valid_url(url):
            url = _first_https_in_text(url) or _first_https_in_text(context) or url
        args["url"] = url
        args.setdefault("fast", True)
        args.setdefault("timeout", 45)
    elif tool_call.name == "web_search":
        raw = _compact_search_query(goal_text, _clean_str(args.get("query", "")), query)
        if len(raw) < 3:
            raw = goal_text.strip() or query.strip()
        args["query"] = raw
        args.setdefault("max_results", 5)

    return ToolCall(name=tool_call.name, arguments=args)


def _arguments_need_repair(
    tool_call: ToolCall, mcp_tools: list[dict[str, Any]]
) -> bool:
    if _missing_required_fields(tool_call, mcp_tools):
        return True
    if tool_call.name == "fetch_url":
        url = _clean_str(tool_call.arguments.get("url", ""))
        return not _is_valid_url(url)
    if tool_call.name == "web_search":
        q = _clean_str(tool_call.arguments.get("query", ""))
        return len(q) < 3 or len(q.split()) > 15
    return False


def _repair_tool_arguments_llm(
    tool_call: ToolCall,
    goal: Goal,
    history: list[str],
    mcp_tools: list[dict[str, Any]],
    query: str,
) -> ToolCall:
    """Second LLM call (decision route) to fill missing tool_call.arguments."""
    spec = _tool_spec(tool_call.name, mcp_tools)
    if not spec:
        raise DecisionError(f"unknown tool {tool_call.name!r}")

    schema = spec.get("input_schema") or {}
    required = schema.get("required") or []
    if not required:
        return tool_call

    history_text = "\n".join(history[-15:]) if history else "(empty)"
    prompt = render(
        "decision.repair_arguments.user",
        query=query,
        goal_text=goal.text,
        tool_name=tool_call.name,
        required_fields=", ".join(required),
        current_arguments=json.dumps(tool_call.arguments),
        execution_history=history_text,
    )
    system = render("decision.repair_arguments.system")

    try:
        result = _llm.chat(
            prompt,
            system=system,
            auto_route="decision",
            temperature=0.5,
            max_tokens=512,
            response_format={
                "type": "json_schema",
                "schema": schema,
                "name": f"{tool_call.name}_args",
                "strict": True,
            },
        )
    except Exception as exc:
        raise DecisionError(
            f"argument repair LLM failed for {tool_call.name}: {exc}"
        ) from exc

    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        raise DecisionError(
            f"argument repair returned no JSON for {tool_call.name}"
        )

    merged = {**tool_call.arguments, **parsed}
    repaired = ToolCall(name=tool_call.name, arguments=merged)
    missing = _missing_required_fields(repaired, mcp_tools)
    if missing:
        raise DecisionError(
            f"tool_call.arguments still missing {missing} after repair for {tool_call.name}"
        )
    return repaired


def _ensure_tool_call(
    output: DecisionOutput,
    goal: Goal,
    history: list[str],
    mcp_tools: list[dict[str, Any]],
    query: str,
) -> DecisionOutput:
    if output.tool_call is None:
        return output

    tool_call = _sanitize_tool_call(output.tool_call, query, goal.text)
    if _arguments_need_repair(tool_call, mcp_tools):
        tool_call = _repair_tool_arguments_llm(
            tool_call, goal, history, mcp_tools, query
        )
        tool_call = _sanitize_tool_call(tool_call, query, goal.text)

    if _arguments_need_repair(tool_call, mcp_tools):
        raise DecisionError(
            f"tool_call.arguments invalid for {tool_call.name}: {tool_call.arguments!r}"
        )
    return DecisionOutput(tool_call=tool_call)


def next_step(
    goal: Goal,
    hits: list[MemoryItem],
    attached: dict[str, Any] | None,
    history: list[str],
    mcp_tools: list[dict[str, Any]],
    query: str = "",
    *,
    goal_position: str = "only",
    total_goals: int = 1,
) -> DecisionOutput:
    history_text = "\n".join(history[-25:]) if history else "(empty)"
    try:
        substantive_block = render("decision.substantive_rule")
    except KeyError:
        substantive_block = ""
    if substantive_block:
        substantive_block = f"{substantive_block}\n\n"

    # Hard positional constraint: the final synthesis goal must always answer.
    if goal_position in ("last", "only"):
        position_note = (
            "IMPORTANT: This is the FINAL synthesis goal. "
            "You MUST produce an answer field — do NOT call any tool. "
            "Compose your response from the EXECUTION HISTORY above.\n"
        )
    else:
        position_note = ""

    system = render("decision.system")
    prompt = render(
        "decision.user",
        query=query,
        goal_id=goal.id,
        goal_text=goal.text,
        goal_done=str(goal.done),
        attach_artifact_id=str(goal.attach_artifact_id),
        goal_position=goal_position,
        total_goals=str(total_goals),
        substantive_rule=substantive_block,
        position_note=position_note,
        memory_hits=_hits_block(hits),
        attached_artifacts=_attached_block(attached),
        mcp_tools=_tools_block(mcp_tools),
        execution_history=history_text,
    )

    try:
        result = _llm.chat(
            prompt,
            system=system,
            auto_route="decision",
            temperature=0.7,
            max_tokens=2048,
            response_format={
                "type": "json_schema",
                "schema": DecisionOutput.model_json_schema(),
                "name": "decision",
                "strict": True,
            },
        )
    except Exception as exc:
        raise DecisionError(f"decision gateway call failed: {exc}") from exc

    parsed = result.get("parsed")
    if parsed:
        output = DecisionOutput.model_validate(parsed)
        return _ensure_tool_call(output, goal, history, mcp_tools, query)

    snippet = (result.get("text") or "")[:200]
    raise DecisionError(
        f"decision returned no structured output (text preview: {snippet!r})"
    )
