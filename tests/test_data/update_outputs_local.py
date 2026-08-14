#!/usr/bin/env python3

"""Regenerate the local inspector fixture with the final AssistantAgent schema."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from minisweagent.models.test_models import DeterministicModel, make_output
from minisweagent.run.mini import DEFAULT_CONFIG_FILE, main


def update_trajectory() -> None:
    trajectory_path = Path(__file__).parent / "local.traj.json"
    outputs = [
        make_output(
            "I will inspect the workspace.",
            [{"tool_call_id": "call_fixture", "name": "bash", "command": "printf 'hello world'"}],
        ),
        make_output("The workspace command returned hello world."),
    ]
    with tempfile.TemporaryDirectory() as session_directory:
        with patch("minisweagent.run.mini.get_model", return_value=DeterministicModel(outputs=outputs)):
            agent = main(
                model_name="deterministic",
                model_class="deterministic",
                environment_class="local",
                task="Inspect the workspace and report the result.",
                auto_approve=True,
                resume=None,
                sessions=False,
                workspace=Path.cwd(),
                config_spec=[
                    str(DEFAULT_CONFIG_FILE),
                    f"session.directory={session_directory}",
                    "events.sinks=[]",
                ],
            )
    trajectory_path.write_text(json.dumps(agent.session.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    update_trajectory()
