"""OpenRouter's normalized adapter.

The final Agent contract is independent of Chat Completions versus Responses wire
formats. OpenRouterModel is the supported OpenRouter transport for both shortcut
names and returns the same ModelResponse contract.
"""

from minisweagent.models.openrouter_model import OpenRouterModel, OpenRouterModelConfig


class OpenRouterResponseModelConfig(OpenRouterModelConfig):
    pass


class OpenRouterResponseModel(OpenRouterModel):
    def __init__(self, **kwargs):
        super().__init__(config_class=OpenRouterResponseModelConfig, **kwargs)
