"""Run the final AssistantAgent on one SWE-Bench instance."""

import json
from pathlib import Path

import typer
from datasets import load_dataset

from minisweagent import global_config_dir
from minisweagent.agents import build_assistant
from minisweagent.agents.session import SessionStore
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.models import get_model
from minisweagent.run.benchmarks.swebench import DATASET_MAPPING, get_sb_environment
from minisweagent.utils.log import logger
from minisweagent.utils.serialize import UNSET, recursive_merge

DEFAULT_OUTPUT_FILE = global_config_dir / "last_swebench_single_run.traj.json"
DEFAULT_CONFIG_FILE = builtin_config_dir / "benchmarks" / "swebench.yaml"
app = typer.Typer(rich_markup_mode="rich", add_completion=False)


@app.command()
def main(
    subset: str = typer.Option("lite", "--subset"),
    split: str = typer.Option("dev", "--split"),
    instance_spec: str = typer.Option("0", "-i", "--instance"),
    model_name: str | None = typer.Option(None, "-m", "--model"),
    model_class: str | None = typer.Option(None, "--model-class"),
    environment_class: str | None = typer.Option(None, "--environment-class"),
    config_spec: list[str] = typer.Option([str(DEFAULT_CONFIG_FILE)], "-c", "--config"),
    output: Path = typer.Option(DEFAULT_OUTPUT_FILE, "-o", "--output"),
) -> None:
    dataset_path = DATASET_MAPPING.get(subset, subset)
    logger.info(f"Loading dataset from {dataset_path}, split {split}...")
    instances = {inst["instance_id"]: inst for inst in load_dataset(dataset_path, split=split)}
    if instance_spec.isnumeric():
        instance_spec = sorted(instances)[int(instance_spec)]
    instance = instances[instance_spec]
    configs = [get_config_from_spec(spec) for spec in config_spec]
    configs.append(
        {
            "agent": {"approval_policy": "auto"},
            "model": {"model_class": model_class or UNSET, "model_name": model_name or UNSET},
            "environment": {"environment_class": environment_class or UNSET},
        }
    )
    config = recursive_merge(*configs)
    environment = get_sb_environment(config, instance)
    store = SessionStore(output.parent / "sessions")
    with store.create(getattr(environment.config, "cwd", "/")) as session:
        agent = build_assistant(get_model(config=config.get("model", {})), environment, session, store, config)
        final = agent.receive(instance["problem_statement"])
        patch = environment.execute({"command": "git diff"}, cwd=session.workspace)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    **session.model_dump(mode="json"),
                    "info": {
                        "exit_status": "Completed",
                        "final_response": final,
                        "submission": patch.get("output", "") if patch.get("returncode") == 0 else "",
                    },
                    "instance_id": instance["instance_id"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    app()
