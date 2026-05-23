"""Live terminal trace formatting for agent6."""
from __future__ import annotations

import io
import json
import sys
from typing import Any

from schemas import DecisionOutput, Goal, ToolCall

# Reconfigure stdout to UTF-8 with replacement so box-drawing and other
# non-ASCII characters never crash the process on Windows cp1252 consoles.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
else:
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    except Exception:
        pass

_TAG_WIDTH = 16


def _tag(name: str) -> str:
    return f"[{name}]".ljust(_TAG_WIDTH)


def _emit(line: str = "") -> None:
    print(line, flush=True)


def blank_line() -> None:
    _emit()


def log_query(query: str, run_id: str) -> None:
    _emit(f"QUERY: {query}")
    _emit(f"RUN_ID: {run_id}")
    _emit()


def log_iter(n: int) -> None:
    _emit(f"--- iter {n} ---")


def log_memory_read(hit_count: int) -> None:
    _emit(f"{_tag('memory.read')}{hit_count} hits")


def log_perception(goals: list[Goal]) -> None:
    if not goals:
        _emit(f"{_tag('perception')}(no goals)")
        return
    for i, goal in enumerate(goals):
        prefix = _tag("perception") if i == 0 else " " * _TAG_WIDTH
        status = "done" if goal.done else "open"
        _emit(f"{prefix}[{status}] {goal.text}")
        if goal.attach_artifact_id:
            _emit(f"{' ' * _TAG_WIDTH}  attach={goal.attach_artifact_id}")


def log_attach(artifact_id: str, size_bytes: int) -> None:
    _emit(f"{_tag('attach')}{artifact_id} ({size_bytes} bytes)")


def log_decision(choice: DecisionOutput) -> None:
    if choice.answer is not None:
        preview = choice.answer.replace("\n", " ")
        if len(preview) > 160:
            preview = preview[:160] + "..."
        _emit(f"{_tag('decision')}ANSWER: {preview}")
        return
    if choice.tool_call is not None:
        tc = choice.tool_call
        args_json = json.dumps(tc.arguments, ensure_ascii=False)
        _emit(f"{_tag('decision')}TOOL_CALL: {tc.name}({args_json})")


def log_action_pending(tool_call: ToolCall) -> None:
    if tool_call.name == "fetch_url":
        url = tool_call.arguments.get("url", "")
        fast = tool_call.arguments.get("fast", True)
        mode = "API/httpx only (no browser)" if fast else "crawl4ai browser (slow)"
        _emit(
            f"{_tag('action')}... fetch_url running ({mode}, <=28s)\n"
            f"{' ' * _TAG_WIDTH}  url={url}"
        )
    else:
        _emit(f"{_tag('action')}… {tool_call.name} running")


def log_action(
    tool_call: ToolCall,
    result_text: str,
    artifact_id: str | None,
    size_bytes: int | None = None,
) -> None:
    preview = result_text.replace("\n", " ").strip()
    if len(preview) > 100:
        preview = preview[:100] + "..."
    if artifact_id:
        size_label = f"{size_bytes} bytes" if size_bytes is not None else "unknown size"
        _emit(
            f"{_tag('action')}→ [artifact {artifact_id}, {size_label}] preview: {preview}"
        )
    elif "TOOL_ERROR" in result_text or "BLOCKED:" in result_text:
        _emit(f"{_tag('action')}→ ERROR: {preview}")
    elif tool_call.name == "web_search":
        count = result_text.count('"title"')
        if count:
            _emit(f"{_tag('action')}→ [{count} results returned, descriptors recorded]")
        else:
            _emit(f"{_tag('action')}→ {preview}")
    else:
        _emit(f"{_tag('action')}→ {preview}")


def log_all_goals_done(goal_count: int) -> None:
    _emit()
    _emit(f"[done] all {goal_count} goals satisfied")
    _emit()


def log_decision_error(exc: Exception) -> None:
    _emit(f"{_tag('decision')}ERROR: {exc}")


def log_perception_error(exc: Exception) -> None:
    _emit(f"{_tag('perception')}ERROR: {exc}")


def log_fatal(exc: Exception) -> None:
    _emit(f"\n[fatal] {exc}")


def log_final(answer: str, width: int = 76) -> None:
    _emit()
    text = answer.strip()
    if not text:
        _emit("FINAL: (empty)")
        return
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = current + [word]
        if len(" ".join(trial)) <= width:
            current = trial
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    _emit("FINAL: " + (lines[0] if lines else ""))
    indent = " " * 7
    for line in lines[1:]:
        _emit(f"{indent}{line}")
