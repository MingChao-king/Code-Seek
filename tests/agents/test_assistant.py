from __future__ import annotations

import json
from pathlib import Path

import pytest

from minisweagent.agents.assistant import AgentLimitsExceeded, AssistantAgent
from minisweagent.agents.events import RecordingEventSink
from minisweagent.agents.schema import (
    MemoryBatch,
    Message,
    ModelCapabilities,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    RunEvent,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from minisweagent.agents.session import SessionStore
from minisweagent.agents.tools import BashTool, ToolRegistry
from minisweagent.context import ContextManager

INSTRUCTIONS = "Answer freely. Tool calls are progress; finish with text and no tool call."
SUMMARY = "Summarize the supplied records as short plain text without tools."


class ScriptedModel:
    def __init__(self, outputs: list[ModelResponse], *, context_window: int = 100_000):
        self.outputs = outputs
        self.calls: list[dict] = []
        self.capabilities = ModelCapabilities(
            model_name="scripted",
            context_window=context_window,
            max_output_tokens=10_000,
            context_window_source="provider",
            max_output_tokens_source="provider",
        )

    def query(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        response = self.outputs.pop(0)
        if kwargs.get("on_text_delta") and response.content:
            kwargs["on_text_delta"](response.content)
        return response

    def estimate_input_tokens(self, messages: list[ModelMessage], tools: list[ToolSpec]) -> int:
        return len(json.dumps([item.model_dump(mode="json") for item in messages], ensure_ascii=False)) + len(
            json.dumps([item.model_dump(mode="json") for item in tools], ensure_ascii=False)
        )


class RecordingEnvironment:
    def __init__(self, outputs: list[dict] | None = None):
        self.outputs = outputs or []
        self.calls: list[tuple[dict, str]] = []

    def execute(self, action: dict, cwd: str = "", **kwargs):
        self.calls.append((action, cwd))
        return self.outputs.pop(0) if self.outputs else {"output": action["command"], "returncode": 0}


def response(content: str = "", calls: list[ToolCall] | None = None, cost: float | None = 0.1) -> ModelResponse:
    return ModelResponse(content=content, tool_calls=calls or [], usage=ModelUsage(cost=cost))


def make_agent(tmp_path: Path, model: ScriptedModel, environment: RecordingEnvironment, **agent_kwargs):
    store = SessionStore(tmp_path / "sessions")
    lease = store.create(str(tmp_path))
    session = lease.__enter__()
    sink = RecordingEventSink()
    tools = ToolRegistry([BashTool(environment, lambda: session.workspace)])
    context = ContextManager(
        model.estimate_input_tokens,
        summary_instructions=SUMMARY,
        compact_at_ratio=0.8,
        compact_to_ratio=0.2,
        keep_recent_turns=2,
        summary_token_budget=128,
        safety_margin_ratio=0.05,
    )
    agent = AssistantAgent(
        model,
        tools,
        context,
        session,
        store,
        event_sinks=[sink],
        instructions=INSTRUCTIONS,
        approval_policy=agent_kwargs.pop("approval_policy", "auto"),
        **agent_kwargs,
    )
    return agent, sink, lease


def test_direct_answer_finishes_without_environment(tmp_path):
    model = ScriptedModel([response("上下文为 100000 tokens")])
    environment = RecordingEnvironment()
    agent, sink, lease = make_agent(tmp_path, model, environment)
    try:
        assert agent.receive("模型上下文多少？") == "上下文为 100000 tokens"
        assert environment.calls == []
        assert [event.type for event in sink.events].count("turn.completed") == 1
        completed = next(event for event in sink.events if event.type == "assistant.message.completed")
        assert completed.payload["final"] is True
        assert agent.events.state == "IDLE"
        assert agent.session.limits.model_calls is None
        assert agent.session.limits.max_output_tokens is None
    finally:
        lease.__exit__(None, None, None)


def test_context_usage_snapshot_is_current_and_does_not_mutate_session(tmp_path):
    model = ScriptedModel([])
    agent, sink, lease = make_agent(tmp_path, model, RecordingEnvironment())
    try:
        assert agent.session.context_usage.input_tokens is None
        assert agent.session.events == []

        snapshot = agent.context_usage_snapshot()

        assert snapshot.context_window == 100_000
        assert snapshot.input_tokens is not None and snapshot.input_tokens > 0
        assert snapshot.remaining_tokens == 100_000 - snapshot.input_tokens
        assert snapshot.compacting is False
        assert agent.session.context_usage.input_tokens is None
        assert agent.session.events == []
        assert sink.events == []
    finally:
        lease.__exit__(None, None, None)


def test_every_tool_step_is_visible_and_model_closes_turn(tmp_path):
    calls = [
        ToolCall(id="call-1", name="bash", arguments={"command": "printf first", "purpose": "读取第一个值"}),
        ToolCall(id="call-2", name="bash", arguments={"command": "printf second", "purpose": "读取第二个值"}),
    ]
    model = ScriptedModel([response("我先读取两个值。", calls), response("已读取 first 和 second，并完成核对。")])
    environment = RecordingEnvironment(
        [{"output": "first", "returncode": 0}, {"output": "second", "returncode": 0}]
    )
    agent, sink, lease = make_agent(tmp_path, model, environment)
    try:
        assert agent.receive("读取并总结") == "已读取 first 和 second，并完成核对。"
        assert len(model.calls) == 2
        assert len(environment.calls) == 2
        assert [event.step_id for event in sink.events if event.type == "tool.proposed"] == ["call-1", "call-2"]
        assert [event.step_id for event in sink.events if event.type == "tool.resolved"] == ["call-1", "call-2"]
        completed = [event for event in sink.events if event.type == "assistant.message.completed"]
        assert [event.payload["final"] for event in completed] == [False, True]
        assert all(message.turn_id == agent.session.messages[0].turn_id for message in agent.session.messages)
        assert [message.role for message in agent.session.messages] == ["user", "assistant", "tool", "tool", "assistant"]
        second_request = model.calls[1]["messages"]
        assert [message.role for message in second_request[-3:]] == ["assistant", "tool", "tool"]
    finally:
        lease.__exit__(None, None, None)


def test_rejected_tool_returns_observation_and_continues(tmp_path):
    call = ToolCall(id="call-reject", name="bash", arguments={"command": "touch x", "purpose": "创建文件"})
    model = ScriptedModel([response("准备创建。", [call]), response("用户拒绝了创建操作，因此没有修改文件。")])
    environment = RecordingEnvironment()
    agent, sink, lease = make_agent(tmp_path, model, environment, approval_policy="ask")
    agent.approve = lambda _call, _title: (False, "只允许查看")
    try:
        assert agent.receive("创建 x") == "用户拒绝了创建操作，因此没有修改文件。"
        assert environment.calls == []
        tool_message = next(message for message in agent.session.messages if message.role == "tool")
        result = json.loads(tool_message.content)
        assert result["status"] == "rejected"
        assert result["executed"] is False
        assert next(event for event in sink.events if event.type == "tool.resolved").payload["executed"] is False
        assert len(model.calls) == 2
    finally:
        lease.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (ToolCall(id="bad-1", name="missing", arguments={}), "Unknown tool"),
        (ToolCall(id="bad-2", name="bash", arguments={"purpose": "缺少命令"}), "bash.command"),
    ],
)
def test_invalid_tool_call_gets_one_error_result(tmp_path, call, expected):
    model = ScriptedModel([response("", [call]), response("工具请求无效，未执行。")])
    environment = RecordingEnvironment()
    agent, sink, lease = make_agent(tmp_path, model, environment)
    try:
        assert agent.receive("执行") == "工具请求无效，未执行。"
        results = [message for message in agent.session.messages if message.role == "tool"]
        assert len(results) == 1
        assert expected in json.loads(results[0].content)["content"]
        assert environment.calls == []
        resolved = [event for event in sink.events if event.type == "tool.resolved"]
        assert len(resolved) == 1 and resolved[0].payload["executed"] is False
    finally:
        lease.__exit__(None, None, None)


def test_active_memory_revision_is_immutable_and_used_next_call(tmp_path):
    model = ScriptedModel([response("ok")])
    agent, _sink, lease = make_agent(tmp_path, model, RecordingEnvironment())
    try:
        agent.session.messages.extend(
            [
                Message(turn_id="old-1", role="user", content="old request"),
                Message(turn_id="old-1", role="assistant", content="old answer"),
            ]
        )
        original = MemoryBatch(level=0, start_message_index=0, end_message_index=2, content="Topic: old")
        agent.session.memory.batches[original.batch_id] = original
        agent.session.memory.active_batch_ids = [original.batch_id]
        agent.session.memory.raw_compaction_cursor = 2
        agent.session_store.save(agent.session)
        revised = agent.revise_memory(
            original.batch_id,
            "Topic: corrected <memory_batch> text",
            agent.session.next_event_sequence - 1,
        )
        assert revised != original.batch_id
        assert agent.session.memory.batches[original.batch_id].content == "Topic: old"
        assert agent.session.memory.active_batch_ids == [revised]
        assert agent.session.memory.batches[revised].origin == "user_revision"
        assert agent.receive("continue") == "ok"
        memory_message = model.calls[0]["messages"][1]
        assert 'origin="user_revision"' in memory_message.content
        assert "&lt;memory_batch&gt;" in memory_message.content
    finally:
        lease.__exit__(None, None, None)


def test_manual_compaction_preserves_messages_and_uses_user_focus(tmp_path):
    model = ScriptedModel(
        [
            ModelResponse(
                content="Topic: preserve the architecture decision",
                usage=ModelUsage(input_tokens=901, output_tokens=8, cost=0.1),
            )
        ]
    )
    agent, sink, lease = make_agent(tmp_path, model, RecordingEnvironment())
    original_messages = []
    session_id = agent.session.session_id
    try:
        for index in range(4):
            agent.session.messages.extend(
                [
                    Message(turn_id=f"turn-{index}", role="user", content=f"request {index} " + "x" * 120),
                    Message(turn_id=f"turn-{index}", role="assistant", content=f"answer {index} " + "y" * 120),
                ]
            )
        agent.session_store.save(agent.session)
        original_messages = [message.model_dump(mode="json") for message in agent.session.messages]

        result = agent.compress("重点保留架构决定和准确文件路径")

        assert result.changed is True
        assert result.after_tokens < result.before_tokens
        assert agent.session.memory.raw_compaction_cursor == 4
        assert [message.model_dump(mode="json") for message in agent.session.messages] == original_messages
        assert len(model.calls) == 1
        summary_system = model.calls[0]["messages"][0].content
        assert "USER_PRESERVATION_FOCUS" in summary_system
        assert "重点保留架构决定和准确文件路径" in summary_system
        assert model.calls[0]["tools"] == []
        assert agent.events.state == "IDLE"
        assert sink.events[-1].type == "context.manual_compaction.completed"
        summary_completed = next(
            event
            for event in sink.events
            if event.type == "model.completed" and event.payload.get("kind") == "summary"
        )
        assert summary_completed.payload["input_tokens"] == 901
        assert not any(
            event.type == "context.usage.updated" and event.payload.get("source") == "provider"
            for event in sink.events
        )
        assert agent.session.context_usage.source == "estimated"
        assert agent.session.context_usage.compacting is False
        assert agent.session.context_usage.input_tokens == result.after_tokens
    finally:
        lease.__exit__(None, None, None)

    with SessionStore(tmp_path / "sessions").resume(session_id) as resumed:
        assert [message.model_dump(mode="json") for message in resumed.messages] == original_messages
        assert resumed.memory.raw_compaction_cursor == 4


def test_manual_compaction_is_a_safe_noop_when_only_recent_turns_exist(tmp_path):
    model = ScriptedModel([])
    agent, sink, lease = make_agent(tmp_path, model, RecordingEnvironment())
    try:
        agent.session.messages.extend(
            [
                Message(turn_id="turn-1", role="user", content="first"),
                Message(turn_id="turn-1", role="assistant", content="answer"),
                Message(turn_id="turn-2", role="user", content="second"),
                Message(turn_id="turn-2", role="assistant", content="answer"),
            ]
        )
        agent.session_store.save(agent.session)

        result = agent.compress()

        assert result.changed is False
        assert model.calls == []
        assert agent.session.memory.raw_compaction_cursor == 0
        assert agent.events.state == "IDLE"
        completed = [event for event in sink.events if event.type == "context.manual_compaction.completed"]
        assert len(completed) == 1 and completed[0].payload["changed"] is False
    finally:
        lease.__exit__(None, None, None)


def test_automatic_compaction_reports_only_main_context_usage(tmp_path):
    model = ScriptedModel(
        [
            ModelResponse(
                content="Topic: compacted old work",
                usage=ModelUsage(input_tokens=1200, output_tokens=6, cost=0.1),
            ),
            ModelResponse(
                content="continued",
                usage=ModelUsage(input_tokens=15000, output_tokens=2, cost=0.1),
            ),
        ],
        context_window=30_000,
    )
    agent, sink, lease = make_agent(tmp_path, model, RecordingEnvironment())
    agent.context_manager = ContextManager(
        model.estimate_input_tokens,
        summary_instructions=SUMMARY,
        compact_at_ratio=0.8,
        compact_to_ratio=0.7,
        keep_recent_turns=2,
        summary_token_budget=128,
        safety_margin_ratio=0.05,
    )
    try:
        agent.session.messages.extend(
            Message(turn_id=f"old-{index}", role="user", content=str(index) * 7000)
            for index in range(4)
        )
        agent.session_store.save(agent.session)

        assert agent.receive("continue") == "continued"

        event_types = [event.type for event in sink.events]
        started = event_types.index("context.compaction.started")
        completed = event_types.index("context.compaction.completed")
        during_compaction = sink.events[started : completed + 1]
        assert not any(
            event.type == "context.usage.updated" and event.payload.get("source") == "provider"
            for event in during_compaction
        )
        summary_completed = next(
            event
            for event in during_compaction
            if event.type == "model.completed" and event.payload.get("kind") == "summary"
        )
        assert summary_completed.payload["input_tokens"] == 1200
        main_usage = next(event for event in sink.events[completed + 1 :] if event.type == "context.usage.updated")
        assert main_usage.payload["source"] == "estimated"
        assert main_usage.payload["compacting"] is False
    finally:
        lease.__exit__(None, None, None)


def test_decision_provider_usage_still_updates_main_context_gauge(tmp_path):
    model = ScriptedModel(
        [ModelResponse(content="ok", usage=ModelUsage(input_tokens=321, output_tokens=1, cost=0.1))]
    )
    agent, sink, lease = make_agent(tmp_path, model, RecordingEnvironment())
    try:
        assert agent.receive("hello") == "ok"
        provider_usage = [
            event
            for event in sink.events
            if event.type == "context.usage.updated" and event.payload.get("source") == "provider"
        ]
        assert len(provider_usage) == 1
        assert provider_usage[0].payload["input_tokens"] == 321
    finally:
        lease.__exit__(None, None, None)


def test_approval_policy_change_persists_and_overrides_resume_config(tmp_path):
    model = ScriptedModel([])
    agent, sink, lease = make_agent(tmp_path, model, RecordingEnvironment(), approval_policy="ask")
    session_id = agent.session.session_id
    try:
        assert agent.approval_policy == "ask"
        assert agent.session.approval_policy is None

        agent.set_approval_policy("auto")

        assert agent.approval_policy == "auto"
        assert agent.session.approval_policy == "auto"
        event = next(event for event in reversed(sink.events) if event.type == "session.approval_policy.updated")
        assert event.payload["policy"] == "auto"
    finally:
        lease.__exit__(None, None, None)

    store = SessionStore(tmp_path / "sessions")
    resumed_lease = store.resume(session_id)
    resumed = resumed_lease.__enter__()
    try:
        resumed_model = ScriptedModel([])
        resumed_agent = AssistantAgent(
            resumed_model,
            ToolRegistry([BashTool(RecordingEnvironment(), lambda: resumed.workspace)]),
            ContextManager(
                resumed_model.estimate_input_tokens,
                summary_instructions=SUMMARY,
                compact_at_ratio=0.8,
                compact_to_ratio=0.2,
                keep_recent_turns=2,
                summary_token_budget=128,
                safety_margin_ratio=0.05,
            ),
            resumed,
            store,
            instructions=INSTRUCTIONS,
            approval_policy="ask",
        )
        assert resumed_agent.approval_policy == "auto"
        resumed_agent.set_approval_policy("ask")
        assert resumed.approval_policy == "ask"
    finally:
        resumed_lease.__exit__(None, None, None)


def test_duplicate_tool_call_ids_are_retried_without_execution(tmp_path):
    duplicate = [
        ToolCall(id="same", name="bash", arguments={"command": "echo one"}),
        ToolCall(id="same", name="bash", arguments={"command": "echo two"}),
    ]
    model = ScriptedModel([response("", duplicate), response("协议已纠正，没有执行重复调用。")])
    environment = RecordingEnvironment()
    agent, sink, lease = make_agent(tmp_path, model, environment)
    try:
        assert agent.receive("执行") == "协议已纠正，没有执行重复调用。"
        assert environment.calls == []
        assert len(model.calls) == 2
        assert any(event.type == "model.protocol_error" for event in sink.events)
        assert [message.role for message in agent.session.messages] == ["user", "assistant"]
    finally:
        lease.__exit__(None, None, None)


def test_model_call_limit_closes_tool_calls_before_failing(tmp_path):
    call = ToolCall(id="blocked", name="bash", arguments={"command": "touch must-not-exist"})
    model = ScriptedModel([response("准备执行", [call])])
    environment = RecordingEnvironment()
    agent, sink, lease = make_agent(tmp_path, model, environment)
    agent.update_limit("model-calls", 1)
    try:
        with pytest.raises(AgentLimitsExceeded, match="could not report"):
            agent.receive("执行")
        assert environment.calls == []
        result = ToolResult.model_validate_json(agent.session.messages[-1].content)
        assert result.executed is False and result.status == "error"
        assert agent.events.state == "FAILED"
        assert sink.events[-1].type == "turn.failed"
    finally:
        lease.__exit__(None, None, None)


def test_cost_limit_prevents_tools_when_followup_cannot_be_funded(tmp_path):
    call = ToolCall(id="cost-blocked", name="bash", arguments={"command": "touch must-not-exist"})
    model = ScriptedModel([response("准备执行", [call], cost=0.2)])
    model.capabilities.cost_tracking_supported = True
    environment = RecordingEnvironment()
    agent, _sink, lease = make_agent(tmp_path, model, environment)
    agent.update_limit("cost", 0.1)
    try:
        with pytest.raises(AgentLimitsExceeded, match="could not report"):
            agent.receive("执行")
        assert environment.calls == []
        result = ToolResult.model_validate_json(agent.session.messages[-1].content)
        assert result.executed is False
    finally:
        lease.__exit__(None, None, None)


def test_unreliable_cost_limit_is_rejected(tmp_path):
    model = ScriptedModel([response("ok", cost=None)])
    agent, _sink, lease = make_agent(tmp_path, model, RecordingEnvironment())
    try:
        with pytest.raises(ValueError, match="reliable cost"):
            agent.update_limit("cost", 1.0)
    finally:
        lease.__exit__(None, None, None)


def test_event_sink_failure_does_not_change_agent_result(tmp_path):
    class FailingSink:
        def emit(self, event):
            raise RuntimeError(f"cannot render {event.type}")

    model = ScriptedModel([response("still works")])
    agent, sink, lease = make_agent(tmp_path, model, RecordingEnvironment())
    agent.events.sink.sinks.insert(0, FailingSink())
    try:
        assert agent.receive("hello") == "still works"
        assert any(event.type == "turn.completed" for event in sink.events)
    finally:
        lease.__exit__(None, None, None)


def test_interrupted_external_call_is_closed_without_replay(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    lease = store.create(str(tmp_path))
    session = lease.__enter__()
    call = ToolCall(id="started-before-crash", name="bash", arguments={"command": "touch x"})
    turn_id = "turn_crashed"
    session.messages.extend(
        [
            Message(turn_id=turn_id, role="user", content="create x"),
            Message(turn_id=turn_id, role="assistant", content="starting", tool_calls=[call]),
        ]
    )
    session.events.extend(
        [
            RunEvent(
                sequence=1,
                session_id=session.session_id,
                turn_id=turn_id,
                type="turn.started",
                state="IDLE",
            ),
            RunEvent(
                sequence=2,
                session_id=session.session_id,
                turn_id=turn_id,
                step_id=call.id,
                type="tool.started",
                state="RUNNING_TOOL",
            ),
        ]
    )
    session.next_event_sequence = 3
    store.save(session)
    session_id = session.session_id
    lease.__exit__(None, None, None)

    resumed_lease = store.resume(session_id)
    resumed = resumed_lease.__enter__()
    environment = RecordingEnvironment()
    model = ScriptedModel([response("已说明上次结果未知，没有重放。")])
    tools = ToolRegistry([BashTool(environment, lambda: resumed.workspace)])
    context = ContextManager(
        model.estimate_input_tokens,
        summary_instructions=SUMMARY,
        compact_at_ratio=0.8,
        compact_to_ratio=0.2,
        keep_recent_turns=2,
        summary_token_budget=128,
        safety_margin_ratio=0.05,
    )
    try:
        agent = AssistantAgent(
            model,
            tools,
            context,
            resumed,
            store,
            instructions=INSTRUCTIONS,
            approval_policy="auto",
        )
        recovered = ToolResult.model_validate_json(resumed.messages[-1].content)
        assert recovered.executed is True and recovered.status == "error"
        assert environment.calls == []
        assert any(event.type == "turn.failed" and event.payload.get("recovered") for event in resumed.events)
        assert agent.receive("继续") == "已说明上次结果未知，没有重放。"
        assert environment.calls == []
    finally:
        resumed_lease.__exit__(None, None, None)


def test_event_redaction_covers_keys_and_secret_literals():
    value = {
        "api_key": "visible",
        "command": "curl -H 'Authorization: Bearer abc.def' -d token=secret sk-1234567890abcdef",
    }
    redacted = AssistantAgent._redact(value)
    assert redacted["api_key"] == "<redacted>"
    assert "abc.def" not in redacted["command"]
    assert "token=secret" not in redacted["command"]
    assert "sk-1234567890abcdef" not in redacted["command"]
