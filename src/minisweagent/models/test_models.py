from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from minisweagent.agents.schema import (
    ModelCapabilities,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolSpec,
)


def make_output(content: str, actions: list[dict] | None = None, cost: float | None = 0.0) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=[
            ToolCall(
                id=action.get("tool_call_id", f"call_{index}"),
                name=action.get("name", "bash"),
                arguments={key: value for key, value in action.items() if key not in {"tool_call_id", "name"}},
            )
            for index, action in enumerate(actions or [])
        ],
        usage=ModelUsage(cost=cost),
    )


class DeterministicModelConfig(BaseModel):
    outputs: list[ModelResponse]
    model_name: str = "deterministic"
    context_window: int | None = 100_000
    max_output_tokens: int | None = 10_000


class DeterministicModel:
    def __init__(self, **kwargs: Any):
        self.config = DeterministicModelConfig(**kwargs)
        self.current_index = 0
        self.queries: list[dict[str, Any]] = []
        self.capabilities = ModelCapabilities(
            model_name=self.config.model_name,
            context_window=self.config.context_window,
            max_output_tokens=self.config.max_output_tokens,
            context_window_source="config" if self.config.context_window else "unknown",
            max_output_tokens_source="config" if self.config.max_output_tokens else "unknown",
            cost_tracking_supported=all(output.usage.cost is not None for output in self.config.outputs),
        )

    def query(
        self,
        messages: list[ModelMessage],
        *,
        tools: list[ToolSpec],
        max_output_tokens: int | None,
        available_output_tokens: int | None,
        timeout_seconds: float | None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        self.queries.append(
            {
                "messages": messages,
                "tools": tools,
                "max_output_tokens": max_output_tokens,
                "available_output_tokens": available_output_tokens,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.current_index >= len(self.config.outputs):
            raise IndexError("DeterministicModel has no output left")
        response = self.config.outputs[self.current_index]
        self.current_index += 1
        if on_text_delta is not None and response.content:
            on_text_delta(response.content)
        return response

    def estimate_input_tokens(self, messages: list[ModelMessage], tools: list[ToolSpec]) -> int:
        return max(
            len(
                json.dumps(
                    {
                        "messages": [message.model_dump(mode="json") for message in messages],
                        "tools": [tool.model_dump(mode="json") for tool in tools],
                    },
                    ensure_ascii=False,
                )
            )
            // 3,
            1,
        )
