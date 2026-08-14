from __future__ import annotations

import pytest

from minisweagent.agents.schema import (
    ConversationMemory,
    MemoryBatch,
    Message,
    ModelCapabilities,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from minisweagent.agents.session import SessionFormatError, SessionInUse, SessionStore
from minisweagent.agents.tools import ConversationHistoryTool
from minisweagent.context import ContextManager


def estimate(messages, tools):
    return sum(len(message.content) + 10 for message in messages) + sum(len(tool.description) for tool in tools)


def test_session_create_list_resume_and_lock(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    with store.create(str(tmp_path)) as session:
        session.messages.append(Message(turn_id="turn-1", role="user", content="hello"))
        store.save(session)
        session_id = session.session_id
        with pytest.raises(SessionInUse):
            with SessionStore(tmp_path / "sessions").resume(session_id):
                pass
        assert store.list_recent()[0].last_user_message == "hello"
    with store.resume(session_id) as resumed:
        assert resumed.messages[0].content == "hello"
        assert resumed.workspace == str(tmp_path.resolve())


def test_session_without_approval_override_keeps_startup_policy_compatibility(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    with store.create(str(tmp_path)) as session:
        assert session.approval_policy is None
        session_id = session.session_id
    with store.resume(session_id) as resumed:
        assert resumed.approval_policy is None


@pytest.mark.parametrize("session_id", ["../escape", "/tmp/file", "ses_bad", ""])
def test_session_id_is_not_a_path(tmp_path, session_id):
    store = SessionStore(tmp_path / "sessions")
    with pytest.raises(SessionFormatError):
        store.resume(session_id)


def test_context_compacts_old_turns_and_keeps_recent_two(tmp_path):
    source = [
        Message(turn_id=f"turn-{index}", role="user", content=str(index) * 260)
        for index in range(4)
    ]
    memory = ConversationMemory()
    accepted = []
    reports = []
    manager = ContextManager(
        estimate,
        summary_instructions="Summarize old conversation records only.",
        compact_at_ratio=0.8,
        compact_to_ratio=0.2,
        keep_recent_turns=2,
        summary_token_budget=64,
        safety_margin_ratio=0.05,
    )

    def summarize(messages, limit, compaction_id, operation, batch_id):
        return ModelResponse(content="Topic: old turns", usage=ModelUsage())

    def accept(candidate, payload):
        accepted.append(candidate.model_copy(deep=True))

    view = manager.build(
        messages=[ModelMessage(role="system", content="stable")],
        source_messages=source,
        tools=[],
        memory=memory,
        model_capabilities=ModelCapabilities(
            model_name="small",
            context_window=1200,
            max_output_tokens=200,
            context_window_source="provider",
            max_output_tokens_source="provider",
        ),
        user_output_limit=None,
        summarize=summarize,
        accept_memory=accept,
        report_compaction=lambda event, payload: reports.append(event),
    )
    assert view.compacted is True
    assert accepted[-1].raw_compaction_cursor == 2
    assert len(accepted[-1].active_batch_ids) == 1
    assert [message.content for message in view.messages[-2:]] == ["2" * 260, "3" * 260]
    assert reports[0] == "context.compaction.started"
    assert reports[-1] == "context.compaction.completed"


def test_conversation_history_inspects_children_and_pages_original_messages(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    with store.create(str(tmp_path)) as session:
        session.messages.extend(
            [
                Message(turn_id="t1", role="user", content="first"),
                Message(turn_id="t1", role="assistant", content="answer"),
                Message(turn_id="t2", role="user", content="second"),
            ]
        )
        left = MemoryBatch(level=0, start_message_index=0, end_message_index=2, content="left")
        right = MemoryBatch(level=0, start_message_index=2, end_message_index=3, content="right")
        parent = MemoryBatch(
            level=1,
            start_message_index=0,
            end_message_index=3,
            content="parent",
            source_batch_ids=[left.batch_id, right.batch_id],
        )
        session.memory.batches = {item.batch_id: item for item in (left, right, parent)}
        session.memory.active_batch_ids = [parent.batch_id]
        session.memory.raw_compaction_cursor = 3
        store.save(session)
        tool = ConversationHistoryTool(lambda: session, max_result_chars=2000)
        inspect_result = tool.execute(
            ToolCall(
                id="inspect",
                name="conversation_history",
                arguments={"action": "inspect", "batch_id": parent.batch_id},
            )
        )
        assert inspect_result.status == "success"
        assert len(__import__("json").loads(inspect_result.content)["children"]) == 2
        read_result = tool.execute(
            ToolCall(
                id="read",
                name="conversation_history",
                arguments={"action": "read", "batch_id": parent.batch_id, "offset": 1, "limit": 1},
            )
        )
        assert __import__("json").loads(read_result.content)["messages"][0]["content"] == "answer"


def test_repeated_compaction_builds_a_persistent_summary_tree():
    source = [Message(turn_id=f"turn-{index}", role="user", content=str(index) * 180) for index in range(8)]
    accepted = []
    operations = []
    manager = ContextManager(
        estimate,
        summary_instructions="Summarize only these records.",
        compact_at_ratio=0.8,
        compact_to_ratio=0.2,
        keep_recent_turns=2,
        summary_token_budget=30,
        safety_margin_ratio=0.05,
    )

    def summarize(messages, limit, compaction_id, operation, batch_id):
        operations.append(operation)
        return ModelResponse(content="Topic: " + operation[:8], usage=ModelUsage(output_tokens=3))

    view = manager.build(
        messages=[ModelMessage(role="system", content="stable")],
        source_messages=source,
        tools=[],
        memory=ConversationMemory(),
        model_capabilities=ModelCapabilities(model_name="small", context_window=700, max_output_tokens=100),
        user_output_limit=None,
        summarize=summarize,
        accept_memory=lambda candidate, payload: accepted.append(candidate.model_copy(deep=True)),
        report_compaction=lambda event, payload: None,
    )
    memory = accepted[-1]
    assert memory.raw_compaction_cursor == 6
    assert [message.content for message in view.messages[-2:]] == ["6" * 180, "7" * 180]
    assert len(memory.batches) > len(memory.active_batch_ids)
    assert any(batch.source_batch_ids for batch in memory.batches.values())
    assert "leaf" in operations and "merge" in operations
    for active_id in memory.active_batch_ids:
        assert active_id in memory.batches


def test_non_reducing_summary_is_retried_with_a_smaller_output_limit():
    source = [
        Message(turn_id="old", role="user", content="x" * 260),
        Message(turn_id="recent-1", role="user", content="a"),
        Message(turn_id="recent-2", role="user", content="b"),
    ]
    limits = []
    manager = ContextManager(
        estimate,
        summary_instructions="Summarize.",
        compact_at_ratio=0.2,
        compact_to_ratio=0.1,
        keep_recent_turns=2,
        summary_token_budget=64,
        safety_margin_ratio=0.05,
    )

    def summarize(messages, limit, compaction_id, operation, batch_id):
        limits.append(limit)
        return ModelResponse(content="S" * limit, usage=ModelUsage(output_tokens=limit))

    accepted = []
    manager.build(
        messages=[ModelMessage(role="system", content="stable")],
        source_messages=source,
        tools=[],
        memory=ConversationMemory(),
        model_capabilities=ModelCapabilities(model_name="small", context_window=1000, max_output_tokens=100),
        user_output_limit=None,
        summarize=summarize,
        accept_memory=lambda candidate, payload: accepted.append(candidate.model_copy(deep=True)),
        report_compaction=lambda event, payload: None,
    )
    assert len(limits) >= 2
    assert min(limits) < max(limits)
    assert accepted[-1].raw_compaction_cursor == 1


def test_oversized_single_record_is_chunked_without_deleting_original():
    original = "0123456789" * 180
    source = [
        Message(turn_id="old", role="user", content=original),
        Message(turn_id="recent-1", role="user", content="a"),
        Message(turn_id="recent-2", role="user", content="b"),
    ]
    operations = []
    accepted = []
    manager = ContextManager(
        estimate,
        summary_instructions="Summarize each source chunk faithfully.",
        compact_at_ratio=0.2,
        compact_to_ratio=0.1,
        keep_recent_turns=2,
        summary_token_budget=24,
        safety_margin_ratio=0.05,
    )

    def summarize(messages, limit, compaction_id, operation, batch_id):
        operations.append(operation)
        return ModelResponse(content="Topic: chunk", usage=ModelUsage(output_tokens=3))

    manager.build(
        messages=[ModelMessage(role="system", content="stable")],
        source_messages=source,
        tools=[],
        memory=ConversationMemory(),
        model_capabilities=ModelCapabilities(model_name="tiny", context_window=420, max_output_tokens=60),
        user_output_limit=None,
        summarize=summarize,
        accept_memory=lambda candidate, payload: accepted.append(candidate.model_copy(deep=True)),
        report_compaction=lambda event, payload: None,
    )
    assert any("chunk" in operation for operation in operations)
    assert source[0].content == original
    assert accepted[-1].raw_compaction_cursor == 1


def test_history_tool_paginates_one_oversized_message_with_content_offset(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    with store.create(str(tmp_path)) as session:
        text = "abcdefghij" * 300
        session.messages.append(Message(turn_id="t1", role="user", content=text))
        batch = MemoryBatch(level=0, start_message_index=0, end_message_index=1, content="large")
        session.memory.batches[batch.batch_id] = batch
        session.memory.active_batch_ids = [batch.batch_id]
        session.memory.raw_compaction_cursor = 1
        store.save(session)
        tool = ConversationHistoryTool(lambda: session, max_result_chars=700)
        offset = content_offset = 0
        pieces = []
        for index in range(20):
            result = tool.execute(
                ToolCall(
                    id=f"read-{index}",
                    name="conversation_history",
                    arguments={
                        "action": "read",
                        "batch_id": batch.batch_id,
                        "offset": offset,
                        "content_offset": content_offset,
                        "limit": 1,
                    },
                )
            )
            payload = __import__("json").loads(result.content)
            pieces.append(payload["messages"][0]["content"])
            offset = payload["next_offset"]
            content_offset = payload["next_content_offset"]
            if not payload["has_more"]:
                break
        assert "".join(pieces) == text
        assert offset == 1 and content_offset == 0
