from minisweagent.agents import AssistantAgent, build_assistant


def test_only_one_builtin_agent_is_exported():
    assert AssistantAgent.__name__ == "AssistantAgent"
    assert callable(build_assistant)
