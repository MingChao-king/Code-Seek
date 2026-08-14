from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import litellm
from pydantic import BaseModel
from tokenizers import Tokenizer

from minisweagent.agents.schema import (
    ModelCapabilities,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolSpec,
)
from minisweagent.exceptions import ContextWindowExceeded, ModelProtocolError, ModelTimeout
from minisweagent.models import GLOBAL_MODEL_STATS
from minisweagent.models.utils.cache_control import set_cache_control
from minisweagent.models.utils.deepseek_v4_encoding import encode_messages as encode_deepseek_v4_messages
from minisweagent.models.utils.retry import retry

logger = logging.getLogger("litellm_model")

_KNOWN_CAPABILITIES = {
    "deepseek-v4-flash": (1_000_000, 393_216),
    "deepseek/deepseek-v4-flash": (1_000_000, 393_216),
}
_DEEPSEEK_V4_REVISION = "60d8d70770c6776ff598c94bb586a859a38244f1"


class LitellmModelConfig(BaseModel):
    model_name: str
    model_kwargs: dict[str, Any] = {}
    litellm_model_registry: Path | str | None = os.getenv("LITELLM_MODEL_REGISTRY_PATH")
    set_cache_control: Literal["default_end"] | None = None
    cost_tracking: Literal["default", "ignore_errors"] = os.getenv("MSWEA_COST_TRACKING", "default")
    capabilities: dict[str, int | None] = {}


class LitellmModel:
    abort_exceptions: list[type[Exception]] = [
        litellm.exceptions.UnsupportedParamsError,
        litellm.exceptions.NotFoundError,
        litellm.exceptions.PermissionDeniedError,
        litellm.exceptions.ContextWindowExceededError,
        litellm.exceptions.AuthenticationError,
        KeyboardInterrupt,
    ]

    def __init__(self, *, config_class: Callable = LitellmModelConfig, **kwargs):
        self.config = config_class(**kwargs)
        if self.config.litellm_model_registry and Path(self.config.litellm_model_registry).is_file():
            litellm.utils.register_model(json.loads(Path(self.config.litellm_model_registry).read_text()))
        self.capabilities = self._resolve_capabilities()
        self._deepseek_tokenizer: Tokenizer | None = None
        self._deepseek_tokenizer_unavailable = False

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
        api_messages = self._prepare_messages_for_api(messages)
        api_tools = self._prepare_tools_for_api(tools)
        kwargs = dict(self.config.model_kwargs)
        requested_limit = max_output_tokens
        if requested_limit is None and self._requires_output_limit():
            candidates = [value for value in (self.capabilities.max_output_tokens, available_output_tokens) if value]
            if not candidates:
                raise ModelProtocolError("This provider requires max_output_tokens, but no reliable model limit is known")
            requested_limit = min(candidates)
        if requested_limit is not None:
            limits = [requested_limit]
            if self.capabilities.max_output_tokens is not None:
                limits.append(self.capabilities.max_output_tokens)
            if available_output_tokens is not None:
                limits.append(available_output_tokens)
            kwargs["max_tokens"] = min(limits)
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        if api_tools:
            kwargs["tools"] = api_tools

        try:
            for attempt in retry(logger=logger, abort_exceptions=self.abort_exceptions):
                with attempt:
                    response = self._query(api_messages, kwargs, on_text_delta)
        except litellm.exceptions.ContextWindowExceededError as error:
            raise ContextWindowExceeded(str(error)) from error
        except litellm.exceptions.Timeout as error:
            raise ModelTimeout(str(error)) from error

        normalized = self._normalize_response(response)
        GLOBAL_MODEL_STATS.add(normalized.usage.cost or 0.0)
        return normalized

    def estimate_input_tokens(self, messages: list[ModelMessage], tools: list[ToolSpec]) -> int:
        api_messages = self._prepare_messages_for_api(messages)
        api_tools = self._prepare_tools_for_api(tools)
        if self._is_deepseek_v4():
            tokenizer = self._get_deepseek_tokenizer()
            if tokenizer is not None:
                rendered_messages = [dict(message) for message in api_messages]
                if api_tools:
                    if not rendered_messages or rendered_messages[0].get("role") != "system":
                        rendered_messages.insert(0, {"role": "system", "content": ""})
                    rendered_messages[0]["tools"] = api_tools
                prompt = encode_deepseek_v4_messages(
                    rendered_messages,
                    thinking_mode=self.config.model_kwargs.get("thinking_mode", "thinking"),
                )
                return len(tokenizer.encode(prompt, add_special_tokens=False).ids)
        try:
            return int(
                litellm.token_counter(
                    model=self.config.model_name,
                    messages=api_messages,
                    tools=api_tools or None,
                )
            )
        except Exception:
            rendered = json.dumps(
                {"messages": api_messages, "tools": api_tools},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return max((len(rendered) + 2) // 3, 1)

    def split_text(self, text: str, max_tokens: int) -> list[str]:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self._is_deepseek_v4():
            tokenizer = self._get_deepseek_tokenizer()
            if tokenizer is not None:
                ids = tokenizer.encode(text, add_special_tokens=False).ids
                return [
                    tokenizer.decode(ids[index : index + max_tokens], skip_special_tokens=False)
                    for index in range(0, len(ids), max_tokens)
                ] or [""]
        # Non-DeepSeek providers use the ContextManager's exact-counter binary
        # splitter, which does not assume a tokenizer API.
        return [text]

    def _query(self, messages: list[dict[str, Any]], kwargs: dict[str, Any], on_text_delta: Callable[[str], None] | None):
        try:
            if on_text_delta is None:
                return litellm.completion(model=self.config.model_name, messages=messages, **kwargs)
            chunks = []
            for chunk in litellm.completion(model=self.config.model_name, messages=messages, stream=True, **kwargs):
                chunks.append(chunk)
                content = getattr(getattr(chunk.choices[0], "delta", None), "content", None)
                if content:
                    on_text_delta(content)
            response = litellm.stream_chunk_builder(chunks, messages=messages)
            if response is None:
                raise ModelProtocolError("Provider returned an empty stream")
            return response
        except litellm.exceptions.AuthenticationError as error:
            error.message += " You can permanently set your API key with `mini-extra config set KEY VALUE`."
            raise

    def _prepare_messages_for_api(self, messages: list[ModelMessage]) -> list[dict[str, Any]]:
        prepared = []
        for source in messages:
            message = source if isinstance(source, ModelMessage) else ModelMessage.model_validate(source)
            item: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
                    }
                    for call in message.tool_calls
                ]
            if message.tool_call_id is not None:
                item["tool_call_id"] = message.tool_call_id
            prepared.append(item)
        return set_cache_control(prepared, mode=self.config.set_cache_control)

    @staticmethod
    def _prepare_tools_for_api(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    def _normalize_response(self, response: Any) -> ModelResponse:
        choice = response.choices[0]
        message = choice.message
        calls = []
        seen = set()
        for raw_call in message.tool_calls or []:
            call_id = str(raw_call.id or "").strip()
            if not call_id or call_id in seen:
                raise ModelProtocolError("Tool call IDs must be non-empty and unique")
            seen.add(call_id)
            try:
                arguments = json.loads(raw_call.function.arguments or "{}")
            except json.JSONDecodeError as error:
                raise ModelProtocolError(f"Invalid tool arguments for {call_id}: {error}") from error
            if not isinstance(arguments, dict):
                raise ModelProtocolError(f"Tool arguments for {call_id} must be an object")
            calls.append(ToolCall(id=call_id, name=raw_call.function.name, arguments=arguments))
        usage = getattr(response, "usage", None)
        return ModelResponse(
            content=message.content or "",
            tool_calls=calls,
            usage=ModelUsage(
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
                cost=self._calculate_cost(response),
            ),
            finish_reason=getattr(choice, "finish_reason", None),
        )

    def _calculate_cost(self, response: Any) -> float | None:
        try:
            cost = float(litellm.cost_calculator.completion_cost(response, model=self.config.model_name))
            return cost if cost > 0 else None
        except Exception:
            return None

    def _resolve_capabilities(self) -> ModelCapabilities:
        configured_window = self.config.capabilities.get("context_window")
        configured_output = self.config.capabilities.get("max_output_tokens")
        known = _KNOWN_CAPABILITIES.get(self.config.model_name.lower())
        provider_window = provider_output = None
        info: dict[str, Any] = {}
        try:
            info = litellm.get_model_info(self.config.model_name) or {}
            provider_window = info.get("max_input_tokens") or info.get("max_tokens")
            provider_output = info.get("max_output_tokens")
        except Exception:
            pass
        return ModelCapabilities(
            model_name=self.config.model_name,
            context_window=configured_window or (known[0] if known else provider_window),
            max_output_tokens=configured_output or (known[1] if known else provider_output),
            context_window_source="config" if configured_window else "provider" if known or provider_window else "unknown",
            max_output_tokens_source="config" if configured_output else "provider" if known or provider_output else "unknown",
            cost_tracking_supported=bool(
                known
                or (
                    info.get("input_cost_per_token")
                    and info.get("output_cost_per_token")
                )
            ),
        )

    def _is_deepseek_v4(self) -> bool:
        return self.config.model_name.lower() in _KNOWN_CAPABILITIES

    def _requires_output_limit(self) -> bool:
        name = self.config.model_name.lower()
        return any(part in name for part in ("anthropic", "claude", "sonnet", "opus"))

    def _get_deepseek_tokenizer(self) -> Tokenizer | None:
        if self._deepseek_tokenizer is not None:
            return self._deepseek_tokenizer
        if self._deepseek_tokenizer_unavailable:
            return None
        try:
            self._deepseek_tokenizer = Tokenizer.from_pretrained(
                "deepseek-ai/DeepSeek-V4-Flash",
                revision=_DEEPSEEK_V4_REVISION,
            )
        except Exception as error:
            self._deepseek_tokenizer_unavailable = True
            logger.warning("DeepSeek V4 tokenizer unavailable; falling back to LiteLLM token counting: %s", error)
            return None
        return self._deepseek_tokenizer
