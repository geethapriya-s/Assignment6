"""Persistent typed memory store with keyword retrieval and gateway-backed ingest."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from llm_gatewayV3.client import LLM
from prompts import render
from schemas import MemoryItem, RememberDraft, ToolCall, utc_now

STATE_DIR = Path(__file__).resolve().parent / "state"
MEMORY_PATH = STATE_DIR / "memory.json"
_GATEWAY_URL = "http://localhost:8101"
_llm = LLM(base_url=_GATEWAY_URL)
_items_adapter = TypeAdapter(list[MemoryItem])


def _ensure_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not MEMORY_PATH.exists():
        MEMORY_PATH.write_text("[]", encoding="utf-8")


def _load_all() -> list[MemoryItem]:
    _ensure_state()
    raw = MEMORY_PATH.read_text(encoding="utf-8").strip() or "[]"
    return _items_adapter.validate_json(raw)


def _save_all(items: list[MemoryItem]) -> None:
    _ensure_state()
    MEMORY_PATH.write_text(
        json.dumps([i.model_dump(mode="json") for i in items], indent=2),
        encoding="utf-8",
    )


def _tokenize(text: str) -> set[str]:
    return {part.lower() for part in text.split() if part.strip()}


def _score_item(item: MemoryItem, query_tokens: set[str]) -> int:
    item_tokens = set(item.keywords)
    item_tokens.update(_tokenize(item.descriptor))
    return len(query_tokens & item_tokens)


def filter(
    kinds: list[str] | None = None,
    goal_id: str | None = None,
    recent: int | None = None,
) -> list[MemoryItem]:
    items = _load_all()
    if kinds:
        kind_set = set(kinds)
        items = [i for i in items if i.kind in kind_set]
    if goal_id is not None:
        items = [i for i in items if i.goal_id == goal_id]
    items = sorted(items, key=lambda x: x.created_at, reverse=True)
    if recent is not None and recent > 0:
        items = items[:recent]
    return items


def read(
    query: str,
    history: list[str],
    kinds: list[str] | None = None,
    top_k: int = 8,
) -> list[MemoryItem]:
    query_tokens = _tokenize(query)
    for line in history:
        query_tokens.update(_tokenize(line))
    candidates = filter(kinds=kinds)
    scored: list[tuple[int, MemoryItem]] = []
    for item in candidates:
        score = _score_item(item, query_tokens)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
    hits = [item for _, item in scored[:top_k]]
    if not hits:
        hits = relevant(query, kinds=kinds, top_k=top_k)
    return hits


def relevant(
    query: str,
    kinds: list[str] | None = None,
    top_k: int = 5,
) -> list[MemoryItem]:
    pool = filter(kinds=kinds, recent=50)
    if not pool:
        return []
    lines = []
    for idx, item in enumerate(pool):
        lines.append(
            f"[{idx}] kind={item.kind} descriptor={item.descriptor} "
            f"keywords={','.join(item.keywords)} value={json.dumps(item.value)[:400]}"
        )
    prompt = render(
        "memory.relevant.user",
        query=query,
        top_k=str(top_k),
        memory_entries="\n".join(lines),
    )
    schema = {
        "type": "object",
        "properties": {"indices": {"type": "array", "items": {"type": "integer"}}},
        "required": ["indices"],
        "additionalProperties": False,
    }
    result = _llm.chat(
        prompt,
        provider="g",
        auto_route="memory",
        temperature=1.0,
        max_tokens=512,
        response_format={"type": "json_schema", "schema": schema, "name": "rank", "strict": True},
    )
    parsed = result.get("parsed") or {}
    indices = parsed.get("indices") or []
    out: list[MemoryItem] = []
    for i in indices:
        if isinstance(i, int) and 0 <= i < len(pool):
            out.append(pool[i])
        if len(out) >= top_k:
            break
    return out


def remember(
    raw_text: str,
    source: str,
    run_id: str,
    goal_id: str | None = None,
) -> MemoryItem:
    prompt = render("memory.remember.user", raw_text=raw_text)
    parsed = None
    try:
        result = _llm.chat(
            prompt,
            provider="g",
            auto_route="memory",
            temperature=1.0,
            max_tokens=1024,
            response_format={
                "type": "json_schema",
                "schema": RememberDraft.model_json_schema(),
                "name": "remember",
                "strict": True,
            },
        )
        parsed = result.get("parsed")
    except Exception:
        parsed = None
    if not parsed:
        draft = RememberDraft(
            kind="fact",
            keywords=list(_tokenize(raw_text))[:12],
            descriptor=raw_text[:200],
            value={"text": raw_text[:2000]},
            confidence=0.5,
        )
    else:
        draft = RememberDraft.model_validate(parsed)
    item = MemoryItem(
        id=f"mem:{uuid.uuid4().hex[:12]}",
        kind=draft.kind,
        keywords=draft.keywords,
        descriptor=draft.descriptor,
        value=draft.value,
        artifact_id=draft.artifact_id,
        source=source,
        run_id=run_id,
        goal_id=goal_id,
        confidence=draft.confidence,
        created_at=utc_now(),
    )
    items = _load_all()
    items.append(item)
    _save_all(items)
    return item


def record_outcome(
    tool_call: ToolCall,
    result_text: str,
    artifact_id: str | None,
    run_id: str,
    goal_id: str | None = None,
) -> MemoryItem:
    # Build richer keywords from tool name, arguments, and result preview
    kw_set: set[str] = {tool_call.name.lower()}
    for v in tool_call.arguments.values():
        kw_set.update(_tokenize(str(v)))
    kw_set.update(list(_tokenize(result_text[:300]))[:20])
    # Filter out very short/noise tokens
    keywords = [k for k in kw_set if len(k) > 2][:20]

    item = MemoryItem(
        id=f"mem:{uuid.uuid4().hex[:12]}",
        kind="tool_outcome",
        keywords=keywords,
        descriptor=f"Outcome of {tool_call.name}",
        value={
            "tool": tool_call.name,
            "arguments": tool_call.arguments,
            "result_preview": result_text[:500],
        },
        artifact_id=artifact_id,
        source="action",
        run_id=run_id,
        goal_id=goal_id,
        confidence=1.0,
        created_at=utc_now(),
    )
    items = _load_all()
    items.append(item)
    _save_all(items)
    return item
