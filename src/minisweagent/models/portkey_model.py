from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig


class PortkeyModelConfig(LitellmModelConfig):
    provider: str = ""
    litellm_model_name_override: str = ""


class PortkeyModel(LitellmModel):
    def __init__(self, *, config_class: type = PortkeyModelConfig, **kwargs: Any):
        super().__init__(config_class=config_class, **kwargs)
        try:
            from portkey_ai import Portkey
        except ImportError as error:
            raise ImportError("PortkeyModel requires the optional portkey-ai package") from error
        api_key = os.getenv("PORTKEY_API_KEY")
        if not api_key:
            raise ValueError("PORTKEY_API_KEY is required")
        client_kwargs = {"api_key": api_key}
        if virtual_key := os.getenv("PORTKEY_VIRTUAL_KEY"):
            client_kwargs["virtual_key"] = virtual_key
        self.client = Portkey(**client_kwargs)

    def _query(
        self,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
        on_text_delta: Callable[[str], None] | None,
    ):
        kwargs.pop("timeout", None)
        response = self.client.chat.completions.create(
            model=self.config.model_name,
            messages=messages,
            **kwargs,
        )
        content = response.choices[0].message.content or ""
        if on_text_delta is not None and content:
            on_text_delta(content)
        return response

    def _calculate_cost(self, response: Any) -> float | None:
        model_name = self.config.litellm_model_name_override or self.config.model_name
        try:
            from litellm.cost_calculator import completion_cost

            cost = float(completion_cost(response, model=model_name))
            return cost if cost > 0 else None
        except Exception:
            return None
