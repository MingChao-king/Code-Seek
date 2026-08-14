from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import requests

from minisweagent.agents.schema import ModelResponse, ModelUsage, ToolCall
from minisweagent.exceptions import ContextWindowExceeded, ModelProtocolError, ModelTimeout
from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig


class OpenRouterModelConfig(LitellmModelConfig):
    api_url: str = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterAPIError(RuntimeError):
    pass


class OpenRouterAuthenticationError(OpenRouterAPIError):
    pass


class OpenRouterRateLimitError(OpenRouterAPIError):
    pass


class OpenRouterModel(LitellmModel):
    def __init__(self, *, config_class: type = OpenRouterModelConfig, **kwargs: Any):
        super().__init__(config_class=config_class, **kwargs)
        self._api_key = os.getenv("OPENROUTER_API_KEY", "")
        self.capabilities.cost_tracking_supported = True

    def _query(
        self,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
        on_text_delta: Callable[[str], None] | None,
    ) -> dict[str, Any]:
        timeout = kwargs.pop("timeout", 60)
        payload = {"model": self.config.model_name, "messages": messages, "usage": {"include": True}, **kwargs}
        try:
            response = requests.post(
                self.config.api_url,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as error:
            raise ModelTimeout(str(error)) from error
        except requests.exceptions.HTTPError as error:
            if response.status_code == 401:
                raise OpenRouterAuthenticationError("OpenRouter authentication failed") from error
            if response.status_code == 429:
                raise OpenRouterRateLimitError("OpenRouter rate limit exceeded") from error
            if response.status_code == 400 and "context" in response.text.lower():
                raise ContextWindowExceeded(response.text) from error
            raise OpenRouterAPIError(f"HTTP {response.status_code}: {response.text}") from error
        except requests.exceptions.RequestException as error:
            raise OpenRouterAPIError(str(error)) from error
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content") or ""
        if on_text_delta is not None and content:
            on_text_delta(content)
        return result

    def _normalize_response(self, response: dict[str, Any]) -> ModelResponse:
        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})
        calls = []
        seen = set()
        for raw in message.get("tool_calls") or []:
            call_id = str(raw.get("id") or "").strip()
            if not call_id or call_id in seen:
                raise ModelProtocolError("Tool call IDs must be non-empty and unique")
            seen.add(call_id)
            try:
                arguments = json.loads(raw.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError as error:
                raise ModelProtocolError(f"Invalid tool arguments for {call_id}: {error}") from error
            if not isinstance(arguments, dict):
                raise ModelProtocolError(f"Tool arguments for {call_id} must be an object")
            calls.append(ToolCall(id=call_id, name=raw.get("function", {}).get("name", ""), arguments=arguments))
        usage = response.get("usage") or {}
        cost = usage.get("cost")
        return ModelResponse(
            content=message.get("content") or "",
            tool_calls=calls,
            usage=ModelUsage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                cost=float(cost) if cost is not None and float(cost) > 0 else None,
            ),
            finish_reason=choice.get("finish_reason"),
        )

    def _calculate_cost(self, response: dict[str, Any]) -> float | None:
        cost = (response.get("usage") or {}).get("cost")
        return float(cost) if cost is not None and float(cost) > 0 else None
