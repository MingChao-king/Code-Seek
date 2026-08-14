from unittest.mock import patch

from minisweagent.models.test_models import DeterministicModel, make_output
from minisweagent.run.mini import DEFAULT_CONFIG_FILE, main


def test_local_end_to_end_uses_native_tool_loop_and_persists_session(tmp_path):
    model = DeterministicModel(
        outputs=[
            make_output(
                "先读取实际值。",
                [
                    {
                        "tool_call_id": "call-local",
                        "name": "bash",
                        "command": "printf 'hello world'",
                        "purpose": "读取测试值",
                    }
                ],
            ),
            make_output("读取完成：hello world"),
        ]
    )
    session_dir = tmp_path / "sessions"
    with (
        patch("minisweagent.run.mini.configure_if_first_time"),
        patch("minisweagent.run.mini.get_model", return_value=model),
    ):
        agent = main(
            model_name="deterministic",
            model_class="deterministic",
            environment_class="local",
            task="读取 hello world",
            auto_approve=True,
            resume=None,
            sessions=False,
            workspace=tmp_path,
            config_spec=[
                str(DEFAULT_CONFIG_FILE),
                f"session.directory={session_dir}",
                "events.sinks=[]",
            ],
        )

    assert agent is not None
    assert [message.role for message in agent.session.messages] == ["user", "assistant", "tool", "assistant"]
    assert agent.session.messages[-1].content == "读取完成：hello world"
    assert len(model.queries) == 2
    assert any(session_dir.glob("ses_*.json"))
    tool_result = agent.session.messages[2]
    assert tool_result.tool_call_id == "call-local"
    assert "hello world" in tool_result.content
