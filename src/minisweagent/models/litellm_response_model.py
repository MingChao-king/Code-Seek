"""Normalized LiteLLM adapter kept as a provider shortcut.

AssistantAgent owns the stable ModelResponse contract, so callers no longer
store provider response objects or use a second action protocol.
"""

from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig


class LitellmResponseModelConfig(LitellmModelConfig):
    pass


class LitellmResponseModel(LitellmModel):
    def __init__(self, **kwargs):
        super().__init__(config_class=LitellmResponseModelConfig, **kwargs)
