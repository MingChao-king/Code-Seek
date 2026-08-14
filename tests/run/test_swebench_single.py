import json
from types import SimpleNamespace
from unittest.mock import patch

from minisweagent import package_dir
from minisweagent.models.test_models import DeterministicModel, make_output
from minisweagent.run.benchmarks.swebench_single import main


class FakeEnvironment:
    def __init__(self, workspace):
        self.config = SimpleNamespace(cwd=str(workspace))
        self.calls = []

    def execute(self, action, cwd=""):
        self.calls.append((action, cwd))
        return {"output": "diff --git a/a.py b/a.py", "returncode": 0}


def test_swebench_single_runs_final_agent_and_writes_final_trajectory(tmp_path):
    output = tmp_path / "result.json"
    environment = FakeEnvironment(tmp_path)
    model = DeterministicModel(outputs=[make_output("Implemented and verified.")])
    dataset = [
        {"instance_id": "second", "problem_statement": "fix second"},
        {"instance_id": "first", "problem_statement": "fix first"},
    ]
    with (
        patch("minisweagent.run.benchmarks.swebench_single.load_dataset", return_value=dataset),
        patch("minisweagent.run.benchmarks.swebench_single.get_sb_environment", return_value=environment),
        patch("minisweagent.run.benchmarks.swebench_single.get_model", return_value=model),
    ):
        main(
            subset="_test",
            split="test",
            instance_spec="0",
            model_name="deterministic",
            model_class="deterministic",
            environment_class="local",
            config_spec=[str(package_dir / "config" / "benchmarks" / "swebench.yaml")],
            output=output,
        )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["instance_id"] == "first"
    assert data["info"]["exit_status"] == "Completed"
    assert data["info"]["final_response"] == "Implemented and verified."
    assert data["info"]["submission"].startswith("diff --git")
    assert environment.calls[-1][0] == {"command": "git diff"}
