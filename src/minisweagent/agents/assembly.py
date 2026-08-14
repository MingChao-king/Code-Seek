from __future__ import annotations

from typing import Any

from minisweagent import Environment, Model
from minisweagent.agents.assistant import ApprovalCallback, AssistantAgent
from minisweagent.agents.events import EventSink
from minisweagent.agents.schema import SessionState
from minisweagent.agents.session import SessionStore
from minisweagent.agents.tools import BashTool, ConversationHistoryTool, ToolRegistry
from minisweagent.context import ContextManager


def build_assistant(
    model: Model,
    environment: Environment,
    session: SessionState,
    session_store: SessionStore,
    config: dict[str, Any],
    *,
    event_sinks: list[EventSink] | None = None,
    approve: ApprovalCallback | None = None,
) -> AssistantAgent:
    tools_config = config.get("tools", {})
    enabled = tools_config.get("enabled", ["bash", "conversation_history"])
    if set(enabled) - {"bash", "conversation_history"} or len(enabled) != len(set(enabled)):
        raise ValueError(f"Invalid tools configuration: {enabled}")
    for key in ("max_result_chars", "history_tool_max_result_chars"):
        if key in tools_config and (
            not isinstance(tools_config[key], int) or isinstance(tools_config[key], bool) or tools_config[key] <= 0
        ):
            raise ValueError(f"tools.{key} must be a positive integer")
    tools = []
    if "bash" in enabled:
        tools.append(BashTool(environment, lambda: session.workspace, tools_config.get("max_result_chars", 10000)))
    if "conversation_history" in enabled:
        tools.append(
            ConversationHistoryTool(
                lambda: session,
                tools_config.get("history_tool_max_result_chars", 20000),
            )
        )
    context_manager = ContextManager(
        model.estimate_input_tokens,
        split_text=getattr(model, "split_text", None),
        **config.get("context", {}),
    )
    agent = AssistantAgent(
        model,
        ToolRegistry(tools),
        context_manager,
        session,
        session_store,
        event_sinks=event_sinks,
        approve=approve,
        **config.get("agent", {}),
    )
    context_manager.validate_minimum_request(
        system=agent.compose_system_message(),
        tools=agent.tools.specs(),
        capabilities=model.capabilities,
    )
    return agent
