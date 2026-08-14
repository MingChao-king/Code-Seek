from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from minisweagent import Model
from minisweagent.agents.events import CompositeEventSink, EventBus, EventSink
from minisweagent.agents.schema import (
    ContextUsage,
    ConversationMemory,
    MemoryBatch,
    Message,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    SessionState,
    ToolCall,
    ToolResult,
    new_id,
)
from minisweagent.agents.session import SessionSaveError, SessionStore
from minisweagent.agents.tools import ToolRegistry, ToolValidationError
from minisweagent.context import ContextManager
from minisweagent.exceptions import ContextWindowExceeded, ModelProtocolError


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instructions: str
    approval_policy: Literal["ask", "auto"] = "ask"
    max_consecutive_format_errors: int = 3

    def model_post_init(self, __context: Any) -> None:
        if not self.instructions.strip():
            raise ValueError("agent.instructions must not be empty")
        if self.max_consecutive_format_errors <= 0:
            raise ValueError("max_consecutive_format_errors must be positive")


class AgentLimitsExceeded(RuntimeError):
    pass


ApprovalCallback = Callable[[ToolCall, str], tuple[bool, str]]


@dataclass(frozen=True)
class ManualCompactionResult:
    changed: bool
    before_tokens: int
    after_tokens: int
    raw_compaction_cursor: int
    active_batch_ids: tuple[str, ...]


class AssistantAgent:
    def __init__(
        self,
        model: Model,
        tools: ToolRegistry,
        context_manager: ContextManager,
        session: SessionState,
        session_store: SessionStore,
        *,
        event_sinks: list[EventSink] | None = None,
        approve: ApprovalCallback | None = None,
        **kwargs: Any,
    ):
        self.config = AgentConfig(**kwargs)
        self.model = model
        self.tools = tools
        self.context_manager = context_manager
        self.session = session
        self.session_store = session_store
        self.events = EventBus(session, CompositeEventSink(event_sinks))
        self.approve = approve
        self._turn_id: str | None = None
        self._turn_started = 0.0
        self._turn_model_calls = 0
        self._turn_tool_calls = 0
        self._turn_cost = 0.0
        self._turn_unknown_cost_calls = 0
        self._protocol_errors = 0
        self._stream_index = 0
        self._manual_compaction = False
        if self.session.approval_policy is not None:
            self.config.approval_policy = self.session.approval_policy
        self._recover_interrupted_calls()

    def receive(self, text: str) -> str:
        if self.events.state not in {"IDLE", "FAILED", "CANCELLED"}:
            raise RuntimeError(f"Agent is busy: {self.events.state}")
        if not text.strip():
            raise ValueError("User message must not be empty")
        self._begin_turn(text)
        retry_feedback = None
        rejected_view_ceiling = None
        tools = self.tools.specs()
        try:
            while True:
                system = self.compose_system_message(retry_feedback)
                view = self.context_manager.build(
                    messages=self._draft_messages(system),
                    source_messages=self.session.messages,
                    tools=tools,
                    memory=self.session.memory,
                    model_capabilities=self.model.capabilities,
                    user_output_limit=self.session.limits.max_output_tokens,
                    summarize=self._run_summary_query,
                    accept_memory=self._accept_memory,
                    report_compaction=self._report_compaction,
                    rejected_view_ceiling=rejected_view_ceiling,
                )
                self._record_context_usage(
                    view.estimated_input_tokens,
                    False,
                    token_count_seconds=view.token_count_seconds,
                    target_unreachable=view.target_unreachable_by_retention,
                )
                try:
                    response, call_id = self._query_model(
                        view.messages,
                        tools,
                        view.user_output_limit,
                        view.available_output_tokens,
                        kind="decision",
                    )
                except ContextWindowExceeded:
                    rejected_view_ceiling = view.estimated_input_tokens
                    retry_feedback = None
                    continue
                except ModelProtocolError as error:
                    self._protocol_errors += 1
                    retry_feedback = f"The previous native response was invalid: {error}"
                    self.events.emit(
                        "model.protocol_error",
                        turn_id=self._turn_id,
                        error=str(error),
                    )
                    if self._protocol_errors >= self.config.max_consecutive_format_errors:
                        raise
                    self._save()
                    continue
                rejected_view_ceiling = None
                if not response.content.strip() and not response.tool_calls:
                    self._protocol_errors += 1
                    retry_feedback = "Return either a non-empty assistant reply or at least one valid native tool call."
                    self.events.emit(
                        "model.protocol_error",
                        turn_id=self._turn_id,
                        call_id=call_id,
                        error="empty model response",
                    )
                    if self._protocol_errors >= self.config.max_consecutive_format_errors:
                        raise ModelProtocolError("Model repeatedly returned an empty response")
                    self._save()
                    continue
                try:
                    self._validate_new_tool_call_ids(response.tool_calls)
                except ModelProtocolError as error:
                    self._protocol_errors += 1
                    retry_feedback = f"The previous native response was invalid: {error}"
                    self.events.emit(
                        "model.protocol_error",
                        turn_id=self._turn_id,
                        call_id=call_id,
                        error=str(error),
                    )
                    if self._protocol_errors >= self.config.max_consecutive_format_errors:
                        raise
                    self._save()
                    continue
                self._protocol_errors = 0
                retry_feedback = None
                assistant = Message(
                    turn_id=self._require_turn_id(),
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                    extra={"finish_reason": response.finish_reason, "model_call_id": call_id},
                )
                self.session.messages.append(assistant)
                for call in response.tool_calls:
                    self.events.emit(
                        "tool.proposed",
                        turn_id=self._turn_id,
                        step_id=call.id,
                        tool_call_id=call.id,
                        tool_name=call.name,
                        call_title=self.tools.describe_call_or_fallback(call),
                        arguments=self._redact(call.arguments),
                    )
                if not response.tool_calls:
                    self.events.emit(
                        "assistant.message.completed",
                        turn_id=self._turn_id,
                        step_id=call_id,
                        message_id=assistant.message_id,
                        call_id=call_id,
                        final=True,
                        tool_call_ids=[],
                    )
                    self._refresh_context_usage()
                    self.events.emit(
                        "turn.completed",
                        turn_id=self._turn_id,
                        state="IDLE",
                        final_message_id=assistant.message_id,
                        model_calls=self._turn_model_calls,
                        tool_calls=self._turn_tool_calls,
                        cost=self._turn_cost,
                        unknown_cost_calls=self._turn_unknown_cost_calls,
                        tool_result_counts=self._tool_result_counts(),
                        context_usage=self.session.context_usage.model_dump(mode="json"),
                        duration_seconds=time.monotonic() - self._turn_started,
                    )
                    self._save()
                    return response.content
                self.events.emit(
                    "assistant.message.completed",
                    turn_id=self._turn_id,
                    step_id=call_id,
                    message_id=assistant.message_id,
                    call_id=call_id,
                    final=False,
                    tool_call_ids=[call.id for call in response.tool_calls],
                )
                self._refresh_context_usage()
                self._save()
                if not self._followup_model_call_possible():
                    self._close_without_execution(response.tool_calls, "No follow-up model-call allowance remains")
                    raise AgentLimitsExceeded("Tool calls were not executed because the model could not report the result")
                for call in response.tool_calls:
                    self._resolve_tool_call(call)
                self.events.state = "WAITING_MODEL"
        except SessionSaveError:
            raise
        except KeyboardInterrupt:
            self._resolve_open_tool_calls("The turn was cancelled before this tool call produced a result")
            self._finish_abnormally("turn.cancelled", "CANCELLED", "Interrupted by user")
            raise
        except Exception as error:
            self._resolve_open_tool_calls(f"The turn failed before this tool call produced a result: {error}")
            self._finish_abnormally("turn.failed", "FAILED", str(error))
            raise

    def compose_system_message(self, retry_feedback: str | None = None) -> ModelMessage:
        capabilities = self.model.capabilities
        runtime = {
            "workspace": self.session.workspace,
            "model": capabilities.model_name,
            "context_window": capabilities.context_window or "unknown",
            "context_window_source": capabilities.context_window_source,
            "max_output_tokens": capabilities.max_output_tokens or "unknown",
            "max_output_tokens_source": capabilities.max_output_tokens_source,
            "approval_policy": self.config.approval_policy,
        }
        content = (
            "<stable_instructions>\n"
            f"{self.config.instructions}\n"
            "</stable_instructions>\n"
            "<runtime>\n"
            f"{json.dumps(runtime, ensure_ascii=False, separators=(',', ':'))}\n"
            "</runtime>"
        )
        if retry_feedback:
            content += f"\n<protocol_correction>{retry_feedback}</protocol_correction>"
        return ModelMessage(role="system", content=content)

    def update_limit(self, field: str, value: int | float | None) -> None:
        mapping = {
            "output": "max_output_tokens",
            "model-calls": "model_calls",
            "tool-calls": "tool_calls",
            "cost": "cost_usd",
            "time": "wall_time_seconds",
        }
        if field not in mapping:
            raise ValueError(f"Unknown limit: {field}")
        if value is not None and value <= 0:
            raise ValueError("Limits must be positive")
        if field == "cost" and value is not None and not self._cost_is_available():
            raise ValueError("The current model cannot provide reliable cost data")
        setattr(self.session.limits, mapping[field], value)
        self.events.emit("session.limit.updated", field=field, value=value)
        self._save()

    def clear_limits(self) -> None:
        self.session.limits = self.session.limits.__class__()
        self.events.emit("session.limit.updated", field="all", value=None)
        self._save()

    @property
    def approval_policy(self) -> Literal["ask", "auto"]:
        return self.config.approval_policy

    def set_approval_policy(self, policy: Literal["ask", "auto"]) -> None:
        if policy not in {"ask", "auto"}:
            raise ValueError(f"Unknown approval policy: {policy}")
        if self.events.state not in {"IDLE", "FAILED", "CANCELLED"}:
            raise RuntimeError("Approval policy cannot change while the Agent is running")
        previous_config_policy = self.config.approval_policy
        previous_session_policy = self.session.approval_policy
        previous_events = list(self.session.events)
        previous_sequence = self.session.next_event_sequence
        self.config.approval_policy = policy
        self.session.approval_policy = policy
        self.events.emit("session.approval_policy.updated", policy=policy)
        try:
            self._save()
        except SessionSaveError:
            self.config.approval_policy = previous_config_policy
            self.session.approval_policy = previous_session_policy
            self.session.events = previous_events
            self.session.next_event_sequence = previous_sequence
            raise

    def revise_memory(self, batch_id: str, content: str, expected_sequence: int) -> str:
        return self._replace_active_memory(batch_id, content, expected_sequence, "revised")

    def restore_memory(self, batch_id: str, version_id: str, expected_sequence: int) -> str:
        version = self.session.memory.batches.get(version_id)
        current = self.session.memory.batches.get(batch_id)
        if version is None or current is None:
            raise ValueError("Unknown memory batch")
        if (version.start_message_index, version.end_message_index) != (
            current.start_message_index,
            current.end_message_index,
        ):
            raise ValueError("Memory versions cover different message ranges")
        return self._replace_active_memory(batch_id, version.content, expected_sequence, "restored", version_id)

    def memory_snapshot(self) -> dict[str, Any]:
        memory = self.session.memory
        return {
            "active_batch_ids": list(memory.active_batch_ids),
            "raw_compaction_cursor": memory.raw_compaction_cursor,
            "batches": {key: value.model_dump(mode="json") for key, value in memory.batches.items()},
        }

    def context_usage_snapshot(self) -> ContextUsage:
        started = time.monotonic()
        system = self.compose_system_message()
        messages = self.context_manager.compose(system, self.session.messages, self.session.memory)
        input_tokens = self.model.estimate_input_tokens(messages, self.tools.specs())
        window = self.model.capabilities.context_window
        return ContextUsage(
            context_window=window,
            input_tokens=input_tokens,
            remaining_tokens=None if window is None else max(window - input_tokens, 0),
            usage_ratio=None if window is None else input_tokens / window,
            source="estimated" if window is not None else "unknown",
            measured_at_sequence=self.session.next_event_sequence - 1,
            compacting=False,
            token_count_seconds=time.monotonic() - started,
        )

    def compress(self, focus: str = "") -> ManualCompactionResult:
        if self.events.state not in {"IDLE", "FAILED", "CANCELLED"}:
            raise RuntimeError(f"Agent is busy: {self.events.state}")
        self._turn_id = None
        self._turn_started = time.monotonic()
        self._turn_model_calls = self._turn_tool_calls = 0
        self._turn_cost = 0.0
        self._turn_unknown_cost_calls = 0
        self._protocol_errors = 0
        self._manual_compaction = True
        tools = self.tools.specs()
        system = self.compose_system_message()
        before_messages = self.context_manager.compose(system, self.session.messages, self.session.memory)
        before_tokens = self.model.estimate_input_tokens(before_messages, tools)
        before_cursor = self.session.memory.raw_compaction_cursor
        before_active = tuple(self.session.memory.active_batch_ids)
        try:
            view = self.context_manager.build(
                messages=before_messages,
                source_messages=self.session.messages,
                tools=tools,
                memory=self.session.memory,
                model_capabilities=self.model.capabilities,
                user_output_limit=self.session.limits.max_output_tokens,
                summarize=self._run_summary_query,
                accept_memory=self._accept_memory,
                report_compaction=self._report_compaction,
                force_compact=True,
                compaction_focus=focus,
            )
            changed = (
                self.session.memory.raw_compaction_cursor != before_cursor
                or tuple(self.session.memory.active_batch_ids) != before_active
            )
            self._record_context_usage(
                view.estimated_input_tokens,
                False,
                token_count_seconds=view.token_count_seconds,
                target_unreachable=view.target_unreachable_by_retention,
            )
            self.events.emit(
                "context.manual_compaction.completed",
                state="IDLE",
                changed=changed,
                focus_provided=bool(focus.strip()),
                before_tokens=before_tokens,
                after_tokens=view.estimated_input_tokens,
                raw_compaction_cursor=self.session.memory.raw_compaction_cursor,
                active_batch_ids=list(self.session.memory.active_batch_ids),
            )
            self._save()
            return ManualCompactionResult(
                changed=changed,
                before_tokens=before_tokens,
                after_tokens=view.estimated_input_tokens,
                raw_compaction_cursor=self.session.memory.raw_compaction_cursor,
                active_batch_ids=tuple(self.session.memory.active_batch_ids),
            )
        finally:
            self._manual_compaction = False
            self._turn_id = None

    def _begin_turn(self, text: str) -> None:
        self._turn_id = new_id("turn")
        self._turn_started = time.monotonic()
        self._turn_model_calls = self._turn_tool_calls = 0
        self._turn_cost = 0.0
        self._turn_unknown_cost_calls = 0
        message = Message(turn_id=self._turn_id, role="user", content=text)
        self.session.messages.append(message)
        self.events.emit(
            "turn.started",
            turn_id=self._turn_id,
            state="IDLE",
            user_message_id=message.message_id,
            started_at=message.created_at.isoformat(),
        )
        self._save()

    def _draft_messages(self, system: ModelMessage) -> list[ModelMessage]:
        return self.context_manager.compose(system, self.session.messages, self.session.memory)

    def _query_model(
        self,
        messages: list[ModelMessage],
        tools: list,
        max_output_tokens: int | None,
        available_output_tokens: int | None,
        *,
        kind: Literal["decision", "summary"],
    ) -> tuple[ModelResponse, str]:
        self._check_model_limit()
        call_id = new_id("model")
        self._turn_model_calls += 1
        self.session.usage.model_calls += 1
        self._stream_index = 0
        self.events.emit(
            "model.started",
            turn_id=self._turn_id,
            step_id=call_id,
            state="WAITING_MODEL" if kind == "decision" else "COMPRESSING",
            call_id=call_id,
            kind=kind,
        )
        self._save()
        started = time.monotonic()
        try:
            response = self.model.query(
                messages=messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
                available_output_tokens=available_output_tokens,
                timeout_seconds=self._remaining_wall_time(),
                on_text_delta=(lambda delta: self._emit_text_delta(call_id, delta)) if kind == "decision" else None,
            )
            response = ModelResponse.model_validate(response)
        except Exception as error:
            if not isinstance(error, ContextWindowExceeded):
                self.session.usage.unknown_cost_calls += 1
                self._turn_unknown_cost_calls += 1
            self.events.emit(
                "model.failed",
                turn_id=self._turn_id,
                step_id=call_id,
                call_id=call_id,
                kind=kind,
                error=f"{type(error).__name__}: {error}",
                duration_seconds=time.monotonic() - started,
            )
            self._save()
            raise
        self._record_model_usage(response.usage)
        if kind == "decision":
            self._record_provider_context_usage(response.usage, call_id)
        self.events.emit(
            "model.completed",
            turn_id=self._turn_id,
            step_id=call_id,
            call_id=call_id,
            kind=kind,
            has_tool_calls=bool(response.tool_calls),
            finish_reason=response.finish_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost=response.usage.cost,
            duration_seconds=time.monotonic() - started,
        )
        if kind == "summary":
            self._save()
        return response, call_id

    def _run_summary_query(
        self,
        messages: list[ModelMessage],
        max_output_tokens: int,
        compaction_id: str,
        operation: str,
        batch_id: str,
    ) -> ModelResponse:
        response, _ = self._query_model(
            messages,
            [],
            max_output_tokens,
            None,
            kind="summary",
        )
        return response

    def _resolve_tool_call(self, call: ToolCall) -> None:
        self._turn_tool_calls += 1
        self.session.usage.tool_calls += 1
        tool = self.tools.get(call.name)
        try:
            self._check_tool_limit()
            if tool is None:
                raise ToolValidationError(f"Unknown tool: {call.name}")
            tool.validate(call.arguments)
        except (ToolValidationError, AgentLimitsExceeded) as error:
            self._append_tool_result(
                call,
                ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    status="error",
                    content=str(error),
                    executed=False,
                ),
            )
            return
        if tool.requires_approval and self.config.approval_policy == "ask":
            title = self.tools.describe_call_or_fallback(call)
            self.events.emit(
                "approval.requested",
                turn_id=self._turn_id,
                step_id=call.id,
                state="WAITING_APPROVAL",
                call_title=title,
                arguments=self._redact(call.arguments),
            )
            self._save()
            approval_started = time.monotonic()
            approved, feedback = self.approve(call, title) if self.approve is not None else (False, "No approver available")
            self.events.emit(
                "approval.resolved",
                turn_id=self._turn_id,
                step_id=call.id,
                approved=approved,
                feedback=feedback,
                duration_seconds=time.monotonic() - approval_started,
            )
            if not approved:
                self._append_tool_result(
                    call,
                    ToolResult(
                        tool_call_id=call.id,
                        name=call.name,
                        status="rejected",
                        content=feedback or "The user rejected this tool call",
                        executed=False,
                    ),
                )
                return
        try:
            self._check_wall_time()
        except AgentLimitsExceeded as error:
            self._append_tool_result(
                call,
                ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    status="error",
                    content=str(error),
                    executed=False,
                ),
            )
            return
        title = self.tools.describe_call_or_fallback(call)
        self.events.emit(
            "tool.started",
            turn_id=self._turn_id,
            step_id=call.id,
            state="RUNNING_TOOL",
            tool_name=call.name,
            call_title=title,
            arguments=self._redact(call.arguments),
            cwd=self.session.workspace,
        )
        self._save()
        started = time.monotonic()
        output_index = 0

        def on_output(stream: Literal["stdout", "stderr"], delta: str) -> None:
            nonlocal output_index
            self.events.emit(
                "tool.output.delta",
                turn_id=self._turn_id,
                step_id=call.id,
                durable=False,
                stream=stream,
                delta=delta,
                index=output_index,
            )
            output_index += 1

        try:
            result = tool.execute(call, on_output=on_output)
        except KeyboardInterrupt:
            result = ToolResult(
                tool_call_id=call.id,
                name=call.name,
                status="error",
                content="Tool execution was interrupted; side effects and final result may be unknown",
                executed=True,
            )
            self._append_tool_result(call, result, duration_seconds=time.monotonic() - started)
            raise
        self._append_tool_result(call, result, duration_seconds=time.monotonic() - started)

    def _append_tool_result(
        self,
        call: ToolCall,
        result: ToolResult,
        *,
        duration_seconds: float = 0.0,
    ) -> None:
        self.session.messages.append(
            Message(
                turn_id=self._require_turn_id(),
                role="tool",
                content=json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
                tool_call_id=call.id,
            )
        )
        self.events.emit(
            "tool.resolved",
            turn_id=self._turn_id,
            step_id=call.id,
            state="RUNNING_TOOL" if result.executed else "WAITING_MODEL",
            tool_call_id=call.id,
            tool_name=call.name,
            executed=result.executed,
            result_title=self.tools.describe_result_or_fallback(call, result),
            status=result.status,
            exit_code=result.exit_code,
            truncated=result.truncated,
            duration_seconds=duration_seconds,
        )
        self._refresh_context_usage()
        self._save()

    def _close_without_execution(self, calls: list[ToolCall], reason: str) -> None:
        for call in calls:
            self._append_tool_result(
                call,
                ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    status="error",
                    content=reason,
                    executed=False,
                ),
            )

    def _record_context_usage(
        self,
        input_tokens: int,
        compacting: bool,
        *,
        token_count_seconds: float | None = None,
        target_unreachable: bool = False,
    ) -> None:
        window = self.model.capabilities.context_window
        usage = ContextUsage(
            context_window=window,
            input_tokens=input_tokens,
            remaining_tokens=None if window is None else max(window - input_tokens, 0),
            usage_ratio=None if window is None else input_tokens / window,
            source="estimated" if window is not None else "unknown",
            measured_at_sequence=self.session.next_event_sequence,
            compacting=compacting,
            token_count_seconds=token_count_seconds,
            target_unreachable_by_retention=target_unreachable,
        )
        self.session.context_usage = usage
        self.events.emit("context.usage.updated", turn_id=self._turn_id, **usage.model_dump(mode="json"))

    def _record_model_usage(self, usage: ModelUsage) -> None:
        if usage.cost is None:
            self.session.usage.unknown_cost_calls += 1
            self._turn_unknown_cost_calls += 1
        else:
            self.session.usage.cost += usage.cost
            self._turn_cost += usage.cost

    def _record_provider_context_usage(self, usage: ModelUsage, call_id: str) -> None:
        if usage.input_tokens is None:
            return
        window = self.model.capabilities.context_window
        self.session.context_usage = ContextUsage(
            context_window=window,
            input_tokens=usage.input_tokens,
            remaining_tokens=None if window is None else max(window - usage.input_tokens, 0),
            usage_ratio=None if window is None else usage.input_tokens / window,
            source="provider",
            measured_for_call_id=call_id,
            measured_at_sequence=self.session.next_event_sequence,
            compacting=False,
        )
        self.events.emit(
            "context.usage.updated",
            turn_id=self._turn_id,
            **self.session.context_usage.model_dump(mode="json"),
        )

    def _refresh_context_usage(self) -> None:
        started = time.monotonic()
        system = self.compose_system_message()
        messages = self.context_manager.compose(system, self.session.messages, self.session.memory)
        count = self.model.estimate_input_tokens(messages, self.tools.specs())
        self._record_context_usage(count, False, token_count_seconds=time.monotonic() - started)

    def _accept_memory(self, candidate: ConversationMemory, payload: dict[str, Any]) -> None:
        previous_memory = self.session.memory
        previous_events = list(self.session.events)
        previous_sequence = self.session.next_event_sequence
        self.session.memory = candidate
        self.events.emit("context.compaction.node_completed", turn_id=self._turn_id, **payload)
        try:
            self._save()
        except SessionSaveError:
            self.session.memory = previous_memory
            self.session.events = previous_events
            self.session.next_event_sequence = previous_sequence
            raise

    def _report_compaction(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "context.compaction.node_completed":
            return
        state = self.events.state
        if event_type in {"context.compaction.started", "context.compaction.node_started"}:
            state = "COMPRESSING"
            self.session.context_usage.compacting = True
        elif event_type == "context.compaction.completed":
            state = "IDLE" if self._manual_compaction or payload.get("manual") else "WAITING_MODEL"
            self.session.context_usage.compacting = False
        elif event_type == "context.compaction.failed":
            state = "FAILED"
            self.session.context_usage.compacting = False
        self.events.emit(event_type, turn_id=self._turn_id, state=state, **payload)
        self._save()

    def _emit_text_delta(self, call_id: str, delta: str) -> None:
        self.events.emit(
            "assistant.delta",
            turn_id=self._turn_id,
            step_id=call_id,
            durable=False,
            call_id=call_id,
            delta=delta,
            index=self._stream_index,
        )
        self._stream_index += 1

    def _finish_abnormally(self, event_type: str, state: Literal["FAILED", "CANCELLED"], reason: str) -> None:
        if self._turn_id is None or any(
            event.turn_id == self._turn_id and event.type in {"turn.completed", "turn.failed", "turn.cancelled"}
            for event in reversed(self.session.events)
        ):
            return
        self.events.emit(
            event_type,
            turn_id=self._turn_id,
            state=state,
            reason=reason,
            model_calls=self._turn_model_calls,
            tool_calls=self._turn_tool_calls,
            tool_result_counts=self._tool_result_counts(),
            duration_seconds=time.monotonic() - self._turn_started,
        )
        self._save()

    def _check_model_limit(self) -> None:
        limits = self.session.limits
        self._check_wall_time()
        if limits.model_calls is not None and self._turn_model_calls >= limits.model_calls:
            raise AgentLimitsExceeded("Model-call limit reached")
        if limits.cost_usd is not None:
            if self._turn_unknown_cost_calls:
                raise AgentLimitsExceeded("Cannot continue a cost-limited turn after an unknown-cost response")
            if self._turn_cost >= limits.cost_usd:
                raise AgentLimitsExceeded("Cost limit reached")

    def _check_tool_limit(self) -> None:
        limit = self.session.limits.tool_calls
        if limit is not None and self._turn_tool_calls > limit:
            raise AgentLimitsExceeded("Tool-call limit reached")

    def _followup_model_call_possible(self) -> bool:
        limit = self.session.limits.model_calls
        if limit is not None and self._turn_model_calls >= limit:
            return False
        cost_limit = self.session.limits.cost_usd
        if cost_limit is not None and (self._turn_unknown_cost_calls or self._turn_cost >= cost_limit):
            return False
        wall_time = self.session.limits.wall_time_seconds
        return wall_time is None or time.monotonic() - self._turn_started < wall_time

    def _check_wall_time(self) -> None:
        limit = self.session.limits.wall_time_seconds
        if limit is not None and time.monotonic() - self._turn_started >= limit:
            raise AgentLimitsExceeded("Wall-time limit reached")

    def _remaining_wall_time(self) -> float | None:
        limit = self.session.limits.wall_time_seconds
        return None if limit is None else max(limit - (time.monotonic() - self._turn_started), 0.001)

    def _cost_is_available(self) -> bool:
        return self.model.capabilities.cost_tracking_supported and self.session.usage.unknown_cost_calls == 0

    def _replace_active_memory(
        self,
        batch_id: str,
        content: str,
        expected_sequence: int,
        action: Literal["revised", "restored"],
        restored_from: str | None = None,
    ) -> str:
        if self.events.state not in {"IDLE", "FAILED", "CANCELLED"}:
            raise RuntimeError("Memory is read-only while the Agent is running")
        if expected_sequence != self.session.next_event_sequence - 1:
            raise RuntimeError("Session changed since the memory view was loaded")
        if batch_id not in self.session.memory.active_batch_ids:
            raise ValueError("Only active memory batches can be edited")
        if not content.strip():
            raise ValueError("Memory content must not be empty")
        current = self.session.memory.batches[batch_id]
        batch = MemoryBatch(
            level=current.level,
            start_message_index=current.start_message_index,
            end_message_index=current.end_message_index,
            content=content.strip(),
            source_batch_ids=current.source_batch_ids,
            origin="user_revision",
            revises_batch_id=batch_id,
        )
        candidate = self.session.memory.model_copy(deep=True)
        candidate.batches[batch.batch_id] = batch
        index = candidate.active_batch_ids.index(batch_id)
        candidate.active_batch_ids[index] = batch.batch_id
        previous_memory = self.session.memory
        previous_events = list(self.session.events)
        previous_sequence = self.session.next_event_sequence
        previous_usage = self.session.context_usage
        self.session.memory = candidate
        self.events.emit(
            f"memory.active_batch.{action}",
            batch_id=batch_id,
            new_batch_id=batch.batch_id,
            restored_from_batch_id=restored_from,
            start_message_index=batch.start_message_index,
            end_message_index=batch.end_message_index,
        )
        self._refresh_context_usage()
        try:
            self._save()
        except SessionSaveError:
            self.session.memory = previous_memory
            self.session.events = previous_events
            self.session.next_event_sequence = previous_sequence
            self.session.context_usage = previous_usage
            raise
        return batch.batch_id

    def _recover_interrupted_calls(self) -> None:
        completed_model_calls = {
            event.payload.get("call_id")
            for event in self.session.events
            if event.type in {"model.completed", "model.failed"}
        }
        changed = False
        affected_turns: set[str] = set()
        for event in list(self.session.events):
            if event.type == "model.started" and event.payload.get("call_id") not in completed_model_calls:
                self.session.usage.unknown_cost_calls += 1
                self.events.emit(
                    "model.failed",
                    turn_id=event.turn_id,
                    step_id=event.step_id,
                    call_id=event.payload.get("call_id"),
                    error="Process interrupted; response and cost are unknown",
                    recovered=True,
                )
                if event.turn_id is not None:
                    affected_turns.add(event.turn_id)
                changed = True
        result_ids = {message.tool_call_id for message in self.session.messages if message.role == "tool"}
        started_ids = {event.step_id for event in self.session.events if event.type == "tool.started"}
        for message in list(self.session.messages):
            for call in message.tool_calls:
                if call.id in result_ids:
                    continue
                was_started = call.id in started_ids
                result = ToolResult(
                    tool_call_id=call.id,
                    name=call.name,
                    status="error",
                    content=(
                        "Process interrupted after execution may have started; side effects and result are unknown"
                        if was_started
                        else "Process interrupted before this tool call started"
                    ),
                    executed=was_started,
                )
                self.session.messages.append(
                    Message(
                        turn_id=message.turn_id,
                        role="tool",
                        content=json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
                        tool_call_id=call.id,
                    )
                )
                self.events.emit(
                    "tool.resolved",
                    turn_id=message.turn_id,
                    step_id=call.id,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    executed=was_started,
                    status="error",
                    result_title=result.content,
                    recovered=True,
                )
                affected_turns.add(message.turn_id)
                changed = True
        terminal_turns = {
            event.turn_id
            for event in self.session.events
            if event.type in {"turn.completed", "turn.failed", "turn.cancelled"}
        }
        for turn_id in affected_turns - terminal_turns:
            self.events.emit(
                "turn.failed",
                turn_id=turn_id,
                state="FAILED",
                reason="The previous process was interrupted; unresolved external calls were closed without replay",
                recovered=True,
            )
            changed = True
        if changed:
            self._save()

    def _resolve_open_tool_calls(self, reason: str) -> None:
        if self._turn_id is None:
            return
        result_ids = {message.tool_call_id for message in self.session.messages if message.role == "tool"}
        started_ids = {
            event.step_id
            for event in self.session.events
            if event.turn_id == self._turn_id and event.type == "tool.started"
        }
        for message in list(self.session.messages):
            if message.turn_id != self._turn_id:
                continue
            for call in message.tool_calls:
                if call.id in result_ids:
                    continue
                self._append_tool_result(
                    call,
                    ToolResult(
                        tool_call_id=call.id,
                        name=call.name,
                        status="error",
                        content=(
                            f"{reason}; execution may have started, so side effects and result are unknown"
                            if call.id in started_ids
                            else reason
                        ),
                        executed=call.id in started_ids,
                    ),
                )
                result_ids.add(call.id)

    def _validate_new_tool_call_ids(self, calls: list[ToolCall]) -> None:
        known = {
            call.id
            for message in self.session.messages
            if message.role == "assistant"
            for call in message.tool_calls
        }
        incoming = [call.id for call in calls]
        if any(not call_id.strip() for call_id in incoming):
            raise ModelProtocolError("Tool call IDs must be non-empty")
        if len(incoming) != len(set(incoming)) or known.intersection(incoming):
            raise ModelProtocolError("Tool call IDs must be unique within the session")

    def _tool_result_counts(self) -> dict[str, int]:
        counts = {"success": 0, "error": 0, "rejected": 0}
        if self._turn_id is None:
            return counts
        for message in self.session.messages:
            if message.turn_id != self._turn_id or message.role != "tool":
                continue
            try:
                status = ToolResult.model_validate_json(message.content).status
            except Exception:
                continue
            counts[status] += 1
        return counts

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                lowered = key.lower()
                result[key] = "<redacted>" if any(word in lowered for word in ("key", "secret", "token", "authorization", "cookie")) else AssistantAgent._redact(item)
            return result
        if isinstance(value, list):
            return [AssistantAgent._redact(item) for item in value]
        if isinstance(value, str):
            value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
            value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "<redacted>", value)
            return re.sub(
                r"(?i)\b(api[_-]?key|token|secret|password)=([^\s;&]+)",
                lambda match: f"{match.group(1)}=<redacted>",
                value,
            )
        return value

    def _require_turn_id(self) -> str:
        if self._turn_id is None:
            raise RuntimeError("No active turn")
        return self._turn_id

    def _save(self) -> None:
        self.session_store.save(self.session)
