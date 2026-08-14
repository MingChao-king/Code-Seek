from minisweagent.models.portkey_model import PortkeyModel, PortkeyModelConfig


class PortkeyResponseAPIModelConfig(PortkeyModelConfig):
    pass


class PortkeyResponseAPIModel(PortkeyModel):
    def __init__(self, **kwargs):
        super().__init__(config_class=PortkeyResponseAPIModelConfig, **kwargs)
