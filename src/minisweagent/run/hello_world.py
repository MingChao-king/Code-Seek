"""Small Python-binding example using the same AssistantAgent as the CLI."""

import logging
import os
from pathlib import Path

import typer
import yaml

from minisweagent import package_dir
from minisweagent.agents import AssistantAgent, build_assistant
from minisweagent.agents.events import ConsoleEventSink
from minisweagent.agents.session import SessionStore
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel

app = typer.Typer()


@app.command()
def main(
    task: str = typer.Option(..., "-t", "--task", help="User message", show_default=False, prompt=True),
    model_name: str = typer.Option(
        os.getenv("MSWEA_MODEL_NAME"),
        "-m",
        "--model",
        help="Model name (defaults to MSWEA_MODEL_NAME env var)",
        prompt="What model do you want to use?",
    ),
) -> AssistantAgent:
    logging.basicConfig(level=logging.DEBUG)
    config = yaml.safe_load(Path(package_dir / "config" / "mini.yaml").read_text())
    config["agent"]["approval_policy"] = "auto"
    store = SessionStore(config.get("session", {}).get("directory"))
    with store.create(str(Path.cwd())) as session:
        agent = build_assistant(
            LitellmModel(model_name=model_name, **config.get("model", {})),
            LocalEnvironment(cwd=session.workspace, **config.get("environment", {})),
            session,
            store,
            config,
            event_sinks=[ConsoleEventSink()],
        )
        print(agent.receive(task))
        return agent


if __name__ == "__main__":
    app()
