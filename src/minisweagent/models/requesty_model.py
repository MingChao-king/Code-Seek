import os
from typing import Any

from minisweagent.models.openrouter_model import OpenRouterModel, OpenRouterModelConfig


class RequestyModelConfig(OpenRouterModelConfig):
    api_url: str = "https://router.requesty.ai/v1/chat/completions"


class RequestyModel(OpenRouterModel):
    def __init__(self, **kwargs: Any):
        super().__init__(config_class=RequestyModelConfig, **kwargs)
        self._api_key = os.getenv("REQUESTY_API_KEY", "")
