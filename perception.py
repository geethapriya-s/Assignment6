"""State-tracking orchestrator — goal decomposition and verification (LLM-only)."""
from __future__ import annotations

import json

from llm_gatewayV3.client import LLM
from prompts import render
from schemas import Goal, MemoryItem, Observation, PerceptionGoals

_llm = LLM(base_url="http://localhost:8101")


class PerceptionError(RuntimeError):
    """Raised when the perception LLM does not return valid structured goals."""


def _artifact_index_map(hits: list[MemoryItem]) -> tuple[dict[int, str], str]:
    index_map: dict[int, str] = {}
    lines: list[str] = []
    idx = 0
    for hit in hits:
        if not hit.artifact_id:
            continue
        index_map[idx] = hit.artifact_id
        tool = ""
        preview = ""
        if isinstance(hit.value, dict):
            tool = str(hit.value.get("tool") or "")
            preview = str(hit.value.get("result_preview") or "")[:160]
        lines.append(
            f"i={idx} -> {hit.artifact_id} | descriptor={hit.descriptor} | "
            f"tool={tool or 'unknown'} | run_id={hit.run_id} | "
            f"keywords={','.join(hit.keywords)} | preview={preview or '(none)'}"
        )
        idx += 1
    block = "\n".join(lines) if lines else "(no artifacts in memory hits)"
    return index_map, block


def _hits_block(hits: list[MemoryItem]) -> str:
    if not hits:
        return "(no memory hits)"
    rows = []
    for i, hit in enumerate(hits):
        rows.append(
            f"[{i}] kind={hit.kind} descriptor={hit.descriptor} "
            f"artifact_id={hit.artifact_id or 'none'} run_id={hit.run_id} "
            f"keywords={','.join(hit.keywords)}"
        )
    return "\n".join(rows)


def observe(
    query: str,
    hits: list[MemoryItem],
    history: list[str],
    prior_goals: list[Goal],
    run_id: str,
) -> Observation:
    index_map, artifact_block = _artifact_index_map(hits)
    history_text = "\n".join(history[-20:]) if history else "(empty)"
    prior_json = json.dumps([g.model_dump() for g in prior_goals], indent=2)
    memory_hits = _hits_block(hits)

    if not prior_goals:
        system = render("perception.decompose.system")
        prompt = render(
            "perception.decompose.user",
            run_id=run_id,
            query=query,
            memory_hits=memory_hits,
            artifact_index_map=artifact_block,
        )
    else:
        system = render("perception.verify.system")
        prompt = render(
            "perception.verify.user",
            run_id=run_id,
            query=query,
            prior_goals=prior_json,
            memory_hits=memory_hits,
            artifact_index_map=artifact_block,
            execution_history=history_text,
        )

    try:
        result = _llm.chat(
            prompt,
            system=system,
            provider="g",
            temperature=1.0,
            max_tokens=2048,
            response_format={
                "type": "json_schema",
                "schema": PerceptionGoals.model_json_schema(),
                "name": "observation",
                "strict": True,
            },
        )
    except Exception as exc:
        raise PerceptionError(f"perception gateway call failed: {exc}") from exc

    parsed = result.get("parsed")
    if not parsed:
        snippet = (result.get("text") or "")[:200]
        raise PerceptionError(
            f"perception returned no structured goals (text preview: {snippet!r})"
        )

    goals = PerceptionGoals.model_validate(parsed).goals

    valid_artifacts = set(index_map.values())
    for goal in goals:
        if goal.attach_artifact_id and goal.attach_artifact_id not in valid_artifacts:
            goal.attach_artifact_id = None

    if prior_goals:
        merged: list[Goal] = []
        for pg in prior_goals:
            updated = next((g for g in goals if g.id == pg.id), None)
            if updated:
                merged.append(
                    Goal(
                        id=pg.id,
                        text=pg.text,
                        done=updated.done,
                        attach_artifact_id=updated.attach_artifact_id or pg.attach_artifact_id,
                    )
                )
            else:
                merged.append(pg)
        goals = merged
        for goal in goals:
            if goal.attach_artifact_id and goal.attach_artifact_id not in valid_artifacts:
                goal.attach_artifact_id = None

    return Observation(goals=goals)
