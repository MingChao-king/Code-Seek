from unittest.mock import patch

from minisweagent.agents.session import SessionStore
from minisweagent.models.test_models import DeterministicModel, make_output
from minisweagent.run.hello_world import main


def test_run_hello_world_uses_the_same_assistant_agent(tmp_path):
    model = DeterministicModel(outputs=[make_output("hello")])
    with (
        patch("minisweagent.run.hello_world.LitellmModel", return_value=model),
        patch("minisweagent.run.hello_world.SessionStore", side_effect=lambda _directory: SessionStore(tmp_path / "sessions")),
    ):
        agent = main(task="say hello", model_name="deterministic")

    assert agent.session.messages[-1].content == "hello"
    assert agent.session.events[-1].type == "turn.completed"
    assert len(model.queries) == 1
