from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from minisweagent.agents.schema import ModelMessage, ToolSpec
from minisweagent.exceptions import ModelProtocolError
from minisweagent.models.litellm_model import LitellmModel


def api_response(*, content: str = "", tool_calls: list | None = None, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls or []),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3),
    )


def api_tool_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class CapturingModel(LitellmModel):
    def __init__(self, response, **kwargs):
        self.response = response
        self.requests = []
        super().__init__(**kwargs)

    def _query(self, messages, kwargs, on_text_delta):
        self.requests.append((messages, kwargs))
        if on_text_delta is not None and self.response.choices[0].message.content:
            on_text_delta(self.response.choices[0].message.content)
        return self.response

    def _calculate_cost(self, response):
        return 0.25


def test_query_accepts_plain_text_and_does_not_impose_default_output_limit():
    deltas = []
    model = CapturingModel(api_response(content="answer"), model_name="deepseek/deepseek-v4-flash")
    result = model.query(
        [ModelMessage(role="user", content="question")],
        tools=[],
        max_output_tokens=None,
        available_output_tokens=100,
        timeout_seconds=None,
        on_text_delta=deltas.append,
    )
    assert result.content == "answer"
    assert result.tool_calls == []
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 3
    assert result.usage.cost == 0.25
    assert deltas == ["answer"]
    assert "max_tokens" not in model.requests[0][1]
    assert "tools" not in model.requests[0][1]


def test_query_sends_dynamic_tools_and_normalizes_native_calls():
    response = api_response(
        content="checking",
        tool_calls=[api_tool_call("call-1", "bash", json.dumps({"command": "pwd"}))],
        finish_reason="tool_calls",
    )
    model = CapturingModel(
        response,
        model_name="example/model",
        capabilities={"context_window": 1000, "max_output_tokens": 400},
    )
    tool = ToolSpec(
        name="bash",
        description="run",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
    )
    result = model.query(
        [ModelMessage(role="user", content="pwd")],
        tools=[tool],
        max_output_tokens=500,
        available_output_tokens=300,
        timeout_seconds=5,
    )
    assert result.tool_calls[0].model_dump() == {
        "id": "call-1",
        "name": "bash",
        "arguments": {"command": "pwd"},
    }
    request_kwargs = model.requests[0][1]
    assert request_kwargs["max_tokens"] == 300
    assert request_kwargs["timeout"] == 5
    assert request_kwargs["tools"][0]["function"]["name"] == "bash"


@pytest.mark.parametrize(
    "calls",
    [
        [api_tool_call("", "bash", "{}")],
        [api_tool_call("same", "bash", "{}"), api_tool_call("same", "bash", "{}")],
        [api_tool_call("bad-json", "bash", "{")],
        [api_tool_call("array", "bash", "[]")],
    ],
)
def test_invalid_native_tool_calls_are_protocol_errors(calls):
    model = CapturingModel(api_response(tool_calls=calls), model_name="example/model")
    with pytest.raises(ModelProtocolError):
        model.query(
            [ModelMessage(role="user", content="go")],
            tools=[],
            max_output_tokens=None,
            available_output_tokens=None,
            timeout_seconds=None,
        )


def test_deepseek_v4_flash_has_known_provider_capabilities():
    model = CapturingModel(api_response(content="ok"), model_name="deepseek/deepseek-v4-flash")
    assert model.capabilities.context_window == 1_000_000
    assert model.capabilities.max_output_tokens == 393_216
    assert model.capabilities.context_window_source == "provider"
    assert model.capabilities.cost_tracking_supported is True


def test_deepseek_v4_uses_official_encoding_and_cached_tokenizer():
    model = CapturingModel(api_response(content="ok"), model_name="deepseek/deepseek-v4-flash")
    tool = ToolSpec(
        name="bash",
        description="run a command",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    )
    messages = [ModelMessage(role="system", content="You are helpful."), ModelMessage(role="user", content="你好")]
    count = model.estimate_input_tokens(messages, [tool])
    if model._deepseek_tokenizer is None:
        pytest.skip("Official DeepSeek V4 tokenizer is not available in this environment")
    tokenizer = model._deepseek_tokenizer
    assert count == 273
    assert model.estimate_input_tokens(messages, [tool]) == count
    assert model._deepseek_tokenizer is tokenizer
    assert model.estimate_input_tokens(messages, []) < count
