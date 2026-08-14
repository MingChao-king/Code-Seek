from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolResult(BaseModel):
    tool_call_id: str
    name: str
    status: Literal["success", "error", "rejected"]
    content: str
    exit_code: int | None = None
    truncated: bool = False
    executed: bool = True


class ModelMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class ModelUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None


class ModelResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    finish_reason: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelCapabilities(BaseModel):
    model_name: str
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window_source: Literal["config", "provider", "unknown"] = "unknown"
    max_output_tokens_source: Literal["config", "provider", "unknown"] = "unknown"
    cost_tracking_supported: bool = False


class Message(BaseModel):
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    turn_id: str
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_model_message(self) -> ModelMessage:
        return ModelMessage(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            tool_call_id=self.tool_call_id,
        )


class MemoryBatch(BaseModel):
    batch_id: str = Field(default_factory=lambda: new_id("mem"))
    level: int = Field(ge=0)
    start_message_index: int
    end_message_index: int
    content: str
    source_batch_ids: list[str] = Field(default_factory=list)
    origin: Literal["model", "user_revision"] = "model"
    revises_batch_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ConversationMemory(BaseModel):
    raw_compaction_cursor: int = 0
    batches: dict[str, MemoryBatch] = Field(default_factory=dict)
    active_batch_ids: list[str] = Field(default_factory=list)


RunState = Literal[
    "IDLE",
    "COMPRESSING",
    "WAITING_MODEL",
    "WAITING_APPROVAL",
    "RUNNING_TOOL",
    "FAILED",
    "CANCELLED",
]


class RunEvent(BaseModel):
    sequence: int
    session_id: str
    turn_id: str | None = None
    step_id: str | None = None
    type: str
    state: RunState
    durable: bool = True
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionUsage(BaseModel):
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    cost: float = Field(default=0.0, ge=0)
    unknown_cost_calls: int = Field(default=0, ge=0)


class SessionLimits(BaseModel):
    max_output_tokens: int | None = Field(default=None, gt=0)
    model_calls: int | None = Field(default=None, gt=0)
    tool_calls: int | None = Field(default=None, gt=0)
    cost_usd: float | None = Field(default=None, gt=0)
    wall_time_seconds: float | None = Field(default=None, gt=0)


class ContextUsage(BaseModel):
    context_window: int | None = None
    input_tokens: int | None = None
    remaining_tokens: int | None = None
    usage_ratio: float | None = None
    source: Literal["estimated", "provider", "unknown"] = "unknown"
    measured_for_call_id: str | None = None
    measured_at_sequence: int = 0
    compacting: bool = False
    token_count_seconds: float | None = None
    target_unreachable_by_retention: bool = False


class SessionState(BaseModel):
    schema_version: int = 1
    session_id: str = Field(default_factory=lambda: new_id("ses"))
    workspace: str
    messages: list[Message] = Field(default_factory=list)
    memory: ConversationMemory = Field(default_factory=ConversationMemory)
    usage: SessionUsage = Field(default_factory=SessionUsage)
    limits: SessionLimits = Field(default_factory=SessionLimits)
    approval_policy: Literal["ask", "auto"] | None = None
    events: list[RunEvent] = Field(default_factory=list)
    next_event_sequence: int = Field(default=1, ge=1)
    context_usage: ContextUsage = Field(default_factory=ContextUsage)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SessionInfo(BaseModel):
    session_id: str
    workspace: str
    updated_at: datetime
    last_user_message: str | None = None


class ContextView(BaseModel):
    messages: list[ModelMessage]
    estimated_input_tokens: int
    input_ceiling: int | None = None
    available_output_tokens: int | None = None
    user_output_limit: int | None = None
    budget_source: Literal["config", "provider", "unknown"] = "unknown"
    compacted: bool = False
    token_count_seconds: float = 0.0
    target_unreachable_by_retention: bool = False
