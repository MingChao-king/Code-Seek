from minisweagent.agents.schema import ModelMessage, ToolSpec
from minisweagent.models.test_models import DeterministicModel, make_output


def test_deterministic_model_records_the_final_query_contract():
    output = make_output("done", [{"tool_call_id": "c1", "name": "bash", "command": "pwd"}], cost=0.5)
    model = DeterministicModel(outputs=[output], model_name="test")
    tool = ToolSpec(name="bash", description="run", parameters={"type": "object"})
    result = model.query(
        [ModelMessage(role="user", content="go")],
        tools=[tool],
        max_output_tokens=None,
        available_output_tokens=100,
        timeout_seconds=2,
    )
    assert result.tool_calls[0].id == "c1"
    assert result.tool_calls[0].arguments == {"command": "pwd"}
    assert model.queries[0]["tools"] == [tool]
    assert model.queries[0]["max_output_tokens"] is None


def test_deterministic_model_estimates_messages_and_tools():
    model = DeterministicModel(outputs=[])
    count = model.estimate_input_tokens(
        [ModelMessage(role="user", content="hello")],
        [ToolSpec(name="bash", description="run", parameters={"type": "object"})],
    )
    assert count > 0
