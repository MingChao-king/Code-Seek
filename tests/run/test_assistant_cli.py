import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import yaml
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from minisweagent.agents.schema import ContextUsage
from minisweagent.run import mini
from minisweagent.run.mini import CONTROL_COMMANDS, ControlCommandCompleter, app


def write_config(tmp_path: Path) -> Path:
    config = {
        "agent": {
            "instructions": "Answer the user. Tool calls are progress and text without tools is final.",
            "approval_policy": "auto",
            "max_consecutive_format_errors": 3,
        },
        "model": {
            "model_class": "deterministic",
            "model_name": "deterministic",
            "outputs": [{"content": "hello", "usage": {"cost": 0.0}}],
        },
        "context": {
            "summary_instructions": "Summarize source records only.",
            "compact_at_ratio": 0.8,
            "compact_to_ratio": 0.2,
            "keep_recent_turns": 2,
            "summary_token_budget": 128,
            "safety_margin_ratio": 0.05,
        },
        "tools": {"enabled": ["bash", "conversation_history"]},
        "session": {"directory": str(tmp_path / "sessions")},
        "events": {"sinks": []},
        "environment": {"environment_class": "local", "cwd": str(tmp_path)},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_cli_creates_and_resumes_a_continuous_session(tmp_path):
    runner = CliRunner()
    config = write_config(tmp_path)
    first = runner.invoke(app, ["--config", str(config), "--task", "first", "--workspace", str(tmp_path)])
    assert first.exit_code == 0, first.output
    assert "hello" in first.output
    session_file = next((tmp_path / "sessions").glob("ses_*.json"))
    session_id = session_file.stem
    second = runner.invoke(app, ["--config", str(config), "--resume", session_id, "--task", "second"])
    assert second.exit_code == 0, second.output
    payload = json.loads(session_file.read_text())
    assert [message["content"] for message in payload["messages"]] == ["first", "hello", "second", "hello"]
    assert payload["messages"][0]["turn_id"] != payload["messages"][2]["turn_id"]


def test_cli_rejects_session_paths_and_removed_modes(tmp_path):
    runner = CliRunner()
    config = write_config(tmp_path)
    result = runner.invoke(app, ["--config", str(config), "--resume", "../escape", "--task", "x"])
    assert result.exit_code != 0
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "--auto-approve" in help_result.output
    assert "--yolo" not in help_result.output
    assert "--agent-class" not in help_result.output


def test_control_command_menu_lists_all_top_level_commands():
    completions = list(ControlCommandCompleter().get_completions(Document("/"), None))
    assert [completion.text for completion in completions] == [command.name for command in CONTROL_COMMANDS]
    assert [command.name for command in CONTROL_COMMANDS] == [
        "/compact",
        "/auto",
        "/ask",
        "/approval",
        "/limit",
        "/memory",
        "/exit",
    ]


def test_control_command_menu_supports_down_arrow_and_enter():
    with create_pipe_input() as pipe_input:
        input_session = mini._create_input_session(input=pipe_input, output=DummyOutput())
        result = []
        thread = threading.Thread(target=lambda: result.append(input_session.prompt()))
        thread.start()
        pipe_input.send_text("/")
        for _ in range(100):
            if input_session.default_buffer.complete_state is not None:
                break
            time.sleep(0.01)
        assert input_session.default_buffer.text == "/"
        assert input_session.default_buffer.complete_state is not None
        pipe_input.send_bytes(b"\x1b[B\x1b[B\r")
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result == ["/auto"]


def test_input_session_accepts_manual_compact_instruction():
    with create_pipe_input() as pipe_input:
        pipe_input.send_text("/compact 重点保留文件地址\r")
        input_session = mini._create_input_session(input=pipe_input, output=DummyOutput())
        assert input_session.prompt() == "/compact 重点保留文件地址"


def test_slash_picker_executes_selected_command_without_sending_a_user_message(monkeypatch):
    class FakeAgent:
        def __init__(self):
            self.focuses = []
            self.received = []

        def context_usage_snapshot(self):
            return ContextUsage(
                context_window=1000,
                input_tokens=200,
                remaining_tokens=800,
                usage_ratio=0.2,
                source="estimated",
            )

        def compress(self, focus):
            self.focuses.append(focus)
            return SimpleNamespace(changed=True, before_tokens=1000, after_tokens=200)

        def receive(self, text):
            self.received.append(text)
            return "unexpected"

    inputs = iter(["/compact 重点保留路径"])

    def read_input(_session):
        try:
            return next(inputs)
        except StopIteration as error:
            raise EOFError from error

    agent = FakeAgent()
    monkeypatch.setattr(mini.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(mini, "_read_user_input", read_input)

    mini._run_input_loop(agent, None)

    assert agent.focuses == ["重点保留路径"]
    assert agent.received == []


def test_context_usage_is_formatted_for_every_input_prompt():
    assert (
        mini._format_context_usage(
            ContextUsage(
                context_window=1_000_000,
                input_tokens=44_385,
                remaining_tokens=955_615,
                usage_ratio=0.044385,
                source="estimated",
            )
        )
        == "上下文 44,385 / 1,000,000（4.4%） · 剩余 955,615"
    )
    assert (
        mini._format_context_usage(
            ContextUsage(context_window=None, input_tokens=321, remaining_tokens=None, source="unknown")
        )
        == "上下文 321 tokens · 总窗口未知"
    )


def test_approval_control_commands_are_local_and_switch_policy(capsys):
    class FakeAgent:
        approval_policy = "ask"

        def __init__(self):
            self.policies = []

        def set_approval_policy(self, policy):
            self.policies.append(policy)
            self.approval_policy = policy

    agent = FakeAgent()

    assert mini._handle_control(agent, "/approval") is True
    assert mini._handle_control(agent, "/auto") is True
    assert mini._handle_control(agent, "/approval") is True
    assert mini._handle_control(agent, "/ask") is True

    assert agent.policies == ["auto", "ask"]
    output = capsys.readouterr().out
    assert "审批策略：ask" in output
    assert "审批策略：auto" in output
