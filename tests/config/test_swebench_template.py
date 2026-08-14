from pathlib import Path

import yaml

from minisweagent.agents.assistant import AgentConfig
from minisweagent.context import ContextConfig

CONFIG_ROOT = Path(__file__).parents[2] / "src" / "minisweagent" / "config"


def test_all_runtime_configs_match_the_final_agent_and_context_contracts():
    for path in [CONFIG_ROOT / "mini.yaml", *sorted((CONFIG_ROOT / "benchmarks").glob("*.yaml"))]:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        AgentConfig.model_validate(config["agent"])
        ContextConfig.model_validate(config["context"])
        assert set(config["tools"]["enabled"]) == {"bash", "conversation_history"}
        assert "mode" not in config["agent"]
        assert "cost_limit" not in config["agent"]
        assert "step_limit" not in config["agent"]


def test_default_prompt_uses_native_tools_and_model_driven_intent_without_legacy_markers():
    config = yaml.safe_load((CONFIG_ROOT / "mini.yaml").read_text(encoding="utf-8"))
    prompt = config["agent"]["instructions"]
    assert "Do not classify the request into fixed" in prompt
    assert "native tool-calling interface" in prompt
    assert "A response that contains any tool call never finishes" in prompt
    assert "submission marker" not in prompt.lower()
    assert "mswea_bash_command" not in prompt


def test_summary_prompt_preserves_operational_facts_and_forbids_task_execution():
    config = yaml.safe_load((CONFIG_ROOT / "mini.yaml").read_text(encoding="utf-8"))
    prompt = config["context"]["summary_instructions"]
    assert "Never continue the task" in prompt
    assert "paths, commands, URLs, identifiers" in prompt
    assert "unfinished request" in prompt
    assert "Return only the batch summary" in prompt
