"""Session 6 main cognitive loop with Proof of Performance telemetry."""
from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import action
import decision
import memory
import perception
import trace_log
from artifacts import ArtifactStore
from decision import DecisionError
from perception import PerceptionError
from schemas import Goal, ProofOfPerformance

MAX_ITERATIONS = 10
STATE_DIR = Path(__file__).resolve().parent / "state"
POP_PATH = STATE_DIR / "pop_validation.json"
ROOT = Path(__file__).resolve().parent


def clear_state() -> None:
    """Remove all persisted state for a clean test run."""
    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "artifacts").mkdir(parents=True, exist_ok=True)


def _next_incomplete(goals: list[Goal]) -> Goal | None:
    for goal in goals:
        if not goal.done:
            return goal
    return None


def _artifact_ids_from_history(history: list[str]) -> list[str]:
    return [
        line.split(":", 1)[-1].strip()
        for line in history
        if line.startswith("ARTIFACT [")
    ]


def _goal_has_evidence(goal_id: str, history: list[str]) -> bool:
    if any(line.startswith(f"ANSWER [{goal_id}]:") for line in history):
        return True
    prefix = f"goal={goal_id}:"
    return any(
        line.startswith("TOOL [") and prefix in line and "TOOL_ERROR" not in line
        for line in history
    )


def _apply_done_evidence(goals: list[Goal], history: list[str]) -> list[Goal]:
    return [
        goal.model_copy(update={"done": False})
        if goal.done and not _goal_has_evidence(goal.id, history)
        else goal
        for goal in goals
    ]


def _artifact_usable_for_attach(store: ArtifactStore, art_id: str) -> bool:
    if not store.exists(art_id):
        return False
    meta = store.get_meta(art_id)
    if meta.size_bytes > 100_000:
        return False
    head = store.get_bytes(art_id)[:600].decode("utf-8", errors="replace").lower()
    if "<!doctype" in head or (head.lstrip().startswith("<html") and head.count("<") > 8):
        return False
    return True


def _fetch_goal_ids_from_history(history: list[str]) -> set[str]:
    """Goal ids that completed a fetch_url tool (execution history protocol)."""
    ids: set[str] = set()
    for line in history:
        if not line.startswith("TOOL [fetch_url] goal=") or "TOOL_ERROR" in line:
            continue
        ids.add(line.split("goal=", 1)[1].split(":", 1)[0])
    return ids


def _sync_attach_from_history(
    goals: list[Goal], history: list[str], store: ArtifactStore
) -> list[Goal]:
    """Bind the most-recent usable artifact to every open goal that lacks one.

    Both intermediate and final (synthesis) goals benefit from having the
    artifact attached: intermediate goals read it to extract facts, and the
    synthesis goal reads it to compose a grounded answer.  The decision layer
    also always receives the full EXECUTION HISTORY, so it can cross-reference
    multiple fetched artifacts even when only the latest one is auto-bound.
    """
    arts = [
        a
        for a in _artifact_ids_from_history(history)
        if _artifact_usable_for_attach(store, a)
    ]
    if not arts or not goals:
        return goals
    fetch_ids = _fetch_goal_ids_from_history(history)
    synced: list[Goal] = []
    for goal in goals:
        g = goal.model_copy()
        if (
            not g.done
            and g.id not in fetch_ids
            and not g.attach_artifact_id
            and arts
        ):
            g.attach_artifact_id = arts[-1]
        synced.append(g)
    return synced


def _all_goals_done(goals: list[Goal]) -> bool:
    return bool(goals) and all(g.done for g in goals)


def _load_attached(store: ArtifactStore, goal: Goal) -> dict[str, Any] | None:
    if not goal.attach_artifact_id or not store.exists(goal.attach_artifact_id):
        return None
    blob = store.get_bytes(goal.attach_artifact_id)
    meta = store.get_meta(goal.attach_artifact_id)
    preview = blob.decode("utf-8", errors="replace")
    return {
        "artifact_id": goal.attach_artifact_id,
        "meta": meta,
        "preview": preview,
    }


def _mcp_tool_specs(tools) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for tool in tools:
        specs.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": getattr(tool, "inputSchema", None)
                or getattr(tool, "input_schema", {})
                or {},
            }
        )
    return specs


async def run(query: str, *, live_log: bool = True) -> str:
    run_id = secrets.token_hex(4)
    memory.remember(query, source="user_query", run_id=run_id)
    if live_log:
        trace_log.log_query(query, run_id)

    history: list[str] = []
    goals: list[Goal] = []
    trace: list[dict[str, Any]] = []
    final_answer = ""
    iterations = 0
    compliance = True
    store = ArtifactStore()

    server_params = StdioServerParameters(
        command="uv",
        args=["run", "python", str(ROOT / "mcp_server.py")],
        cwd=str(ROOT),
    )

    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                mcp_tools = _mcp_tool_specs(tools_result.tools)

                for iteration in range(1, MAX_ITERATIONS + 1):
                    iterations = iteration
                    step: dict[str, Any] = {"iteration": iteration, "run_id": run_id}
                    if live_log:
                        trace_log.log_iter(iteration)

                    hits = memory.read(query, history)
                    step["memory_hits"] = len(hits)
                    if live_log:
                        trace_log.log_memory_read(len(hits))

                    try:
                        obs = perception.observe(query, hits, history, goals, run_id)
                    except PerceptionError as exc:
                        compliance = False
                        history.append(f"PERCEPTION_ERROR: {exc}")
                        step["perception_error"] = str(exc)
                        trace.append(step)
                        if live_log:
                            trace_log.log_perception_error(exc)
                        break
                    goals = _sync_attach_from_history(obs.goals, history, store)
                    goals = _apply_done_evidence(goals, history)
                    step["goals"] = [g.model_dump() for g in goals]
                    if live_log:
                        trace_log.log_perception(goals)

                    if _all_goals_done(goals):
                        step["status"] = "all_goals_done"
                        trace.append(step)
                        if live_log:
                            trace_log.log_all_goals_done(len(goals))
                        break

                    active = _next_incomplete(goals)
                    if active is None:
                        step["status"] = "no_active_goal"
                        trace.append(step)
                        break

                    goal_index = next(i for i, g in enumerate(goals) if g.id == active.id)
                    goal_position = (
                        "first"
                        if goal_index == 0
                        else "last"
                        if goal_index == len(goals) - 1
                        else "middle"
                    )

                    step["active_goal"] = active.model_dump()
                    attached = _load_attached(store, active)
                    if live_log and attached:
                        meta = attached["meta"]
                        trace_log.log_attach(
                            attached["artifact_id"],
                            meta.size_bytes,
                        )

                    try:
                        choice = decision.next_step(
                            active,
                            hits,
                            attached,
                            history,
                            mcp_tools,
                            query,
                            goal_position=goal_position,
                            total_goals=len(goals),
                        )
                    except (DecisionError, Exception) as exc:
                        compliance = False
                        history.append(f"DECISION_ERROR: {exc}")
                        step["decision_error"] = str(exc)
                        trace.append(step)
                        if live_log:
                            trace_log.log_decision_error(exc)
                        break

                    step["decision"] = choice.model_dump()
                    if live_log:
                        trace_log.log_decision(choice)

                    if choice.answer is not None:
                        answer_text = choice.answer
                        history.append(f"ANSWER [{active.id}]: {answer_text}")
                        final_answer = answer_text
                        step["action"] = "answer"
                    elif choice.tool_call is not None:
                        if live_log:
                            trace_log.log_action_pending(choice.tool_call)
                        result_text, artifact_id = await action.execute(
                            session, choice.tool_call, mcp_tools
                        )
                        memory.record_outcome(
                            choice.tool_call,
                            result_text,
                            artifact_id,
                            run_id,
                            goal_id=active.id,
                        )
                        history.append(
                            f"TOOL [{choice.tool_call.name}] goal={active.id}: "
                            f"{result_text[:2000]}"
                        )
                        if artifact_id:
                            history.append(
                                f"ARTIFACT [{choice.tool_call.name}]: {artifact_id}"
                            )
                        if (
                            "TOOL_ERROR" not in result_text
                            and "BLOCKED:" not in result_text
                            and "Error executing tool" not in result_text
                            and "exceeded 4KB" not in result_text
                        ):
                            final_answer = result_text[:4000]
                        step["action"] = "tool"
                        step["tool"] = choice.tool_call.model_dump()
                        step["tool_result_preview"] = result_text[:500]
                        step["artifact_id"] = artifact_id
                        if live_log:
                            size = None
                            if artifact_id and store.exists(artifact_id):
                                size = store.get_meta(artifact_id).size_bytes
                            trace_log.log_action(
                                choice.tool_call,
                                result_text,
                                artifact_id,
                                size,
                            )
                    else:
                        compliance = False
                        step["action"] = "invalid_decision"
                    trace.append(step)
                    if live_log:
                        trace_log.blank_line()

                if not final_answer:
                    for line in reversed(history):
                        if line.startswith("ANSWER "):
                            final_answer = line.split(":", 1)[-1].strip()
                            break
                    if not final_answer:
                        for line in reversed(history):
                            if line.startswith("TOOL ") and "TOOL_ERROR" not in line:
                                final_answer = line.split(":", 1)[-1].strip()
                                break
                    if not final_answer:
                        final_answer = (
                            "Task processing completed. See execution trace for details."
                        )

    except Exception as exc:
        # Unwrap Python 3.11+ ExceptionGroup so the real cause is visible
        real_exc = exc
        if hasattr(exc, "exceptions") and exc.exceptions:  # ExceptionGroup
            real_exc = exc.exceptions[0]
        compliance = False
        trace.append({"fatal_error": str(real_exc)})
        if not final_answer:
            final_answer = f"Run failed: {real_exc}"
        if live_log:
            trace_log.log_fatal(real_exc)
        print(f"\n[fatal] {type(real_exc).__name__}: {real_exc}", file=sys.stderr)
        traceback.print_exception(type(real_exc), real_exc, real_exc.__traceback__, file=sys.stderr)

    if compliance:
        for step in trace:
            preview = step.get("tool_result_preview") or ""
            if "TOOL_ERROR" in preview or "Error executing tool" in preview:
                compliance = False
                break

    answers = [
        line.split(":", 1)[-1].strip()
        for line in history
        if line.startswith("ANSWER ")
    ]
    if answers:
        final_answer = answers[-1]

    pop = ProofOfPerformance(
        run_id=run_id,
        query=query,
        total_iterations=iterations,
        final_answer=final_answer,
        goals_snapshot=goals,
        execution_trace=trace,
        compliance_verified=compliance,
    )
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    POP_PATH.write_text(pop.model_dump_json(indent=2), encoding="utf-8")
    if live_log:
        trace_log.log_final(final_answer)
    return final_answer


ASSIGNMENT_TARGETS = [
    "Search the web for the latest Python 3.13 release highlights and summarize in 3 bullet points.",
    "List all files in the sandbox directory using list_dir.",
    "What is the current time in Asia/Kolkata? Use get_time.",
    "Convert 250 USD to INR using currency_convert.",
]


async def run_all_targets(
    *, clear_between: bool = False, live_log: bool = True
) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for q in ASSIGNMENT_TARGETS:
        if clear_between:
            clear_state()
        answer = await run(q, live_log=live_log)
        results.append((q, answer))
    return results


def _parse_args(argv: list[str]) -> tuple[bool, bool, bool, str]:
    """Return (clear_state, run_targets, quiet_log, query_text)."""
    clear = "--clear" in argv
    targets = "--targets" in argv
    quiet = "--quiet" in argv
    rest = [a for a in argv if a not in ("--clear", "--targets", "--quiet")]
    if targets:
        return clear, True, quiet, ""
    query = " ".join(rest).strip()
    if not query:
        query = "Search the web for Python asyncio best practices and summarize briefly."
    return clear, False, quiet, query


def main() -> None:
    clear, targets, quiet, query = _parse_args(sys.argv[1:])
    if clear:
        clear_state()

    if targets:
        outcomes = asyncio.run(
            run_all_targets(clear_between=clear, live_log=not quiet)
        )
        for q, answer in outcomes:
            print("=" * 72)
            print("QUERY:", q)
            if quiet:
                print("ANSWER:", answer[:1200])
        print("\nPoP written to", POP_PATH)
        return

    asyncio.run(run(query, live_log=not quiet))
    print(f"\n[run complete] PoP -> {POP_PATH}")


if __name__ == "__main__":
    main()
