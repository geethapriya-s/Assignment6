"""Pydantic v2 contracts for all cognitive-layer boundary transitions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class MemoryItem(BaseModel):
    id: str
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str
    value: dict[str, Any]
    artifact_id: str | None = None
    source: str
    run_id: str
    goal_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: datetime


class Artifact(BaseModel):
    id: str
    content_type: str
    size_bytes: int
    source: str
    descriptor: str


class Goal(BaseModel):
    id: str
    text: str
    done: bool = False
    attach_artifact_id: str | None = None


class Observation(BaseModel):
    goals: list[Goal]

    @property
    def all_done(self) -> bool:
        return bool(self.goals) and all(g.done for g in self.goals)


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class DecisionOutput(BaseModel):
    answer: str | None = None
    tool_call: ToolCall | None = None

    @model_validator(mode="after")
    def exactly_one_populated(self) -> DecisionOutput:
        has_answer = self.answer is not None
        has_tool = self.tool_call is not None
        if has_answer == has_tool:
            raise ValueError("DecisionOutput must have exactly one of answer or tool_call set")
        if has_answer and not str(self.answer).strip():
            raise ValueError("DecisionOutput answer must be non-empty when set")
        if has_tool and not self.tool_call.name.strip():
            raise ValueError("DecisionOutput tool_call must have a non-empty name")
        return self


class ProofOfPerformance(BaseModel):
    run_id: str
    query: str
    total_iterations: int
    final_answer: str
    goals_snapshot: list[Goal]
    execution_trace: list[dict[str, Any]]
    compliance_verified: bool


class RememberDraft(BaseModel):
    """Structured memory-ingest payload from the gateway (no server-side fields)."""

    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str
    value: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str | None = None
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class PerceptionGoals(BaseModel):
    goals: list[Goal]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
