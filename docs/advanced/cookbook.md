# Extending the final Agent

CodeSeek has one built-in `AssistantAgent`. Extend capabilities by composing protocols around it instead of adding Agent subclasses or fixed workflows.

## Add a tool

Implement the `Tool` protocol from `minisweagent.agents.tools`: provide a native `ToolSpec`, validation, user-visible descriptions, an approval requirement, and an `execute()` method returning one `ToolResult`. Register the instance in `ToolRegistry`. The Agent loop does not change.

## Add a model provider

Implement the public `Model` protocol: expose verified `ModelCapabilities`, normalize provider responses into `ModelResponse`, accept native `ToolSpec` values, and provide complete-request input-token estimation. Providers may stream visible text through `on_text_delta`.

## Add an environment

Implement `Environment.execute()` for Bash execution. Environments that can stream may also implement `execute_stream(action, cwd=..., on_output=...)`; `BashTool` uses it when available and otherwise publishes the final output once.

## Add a UI or integration

Implement `EventSink.emit(RunEvent)`. Sinks receive the same durable lifecycle events and non-durable output deltas. Sink failures are isolated from Agent decisions and tool results. Session persistence remains the source of truth.

## Compose in Python

```python
from pathlib import Path

import yaml

from minisweagent import package_dir
from minisweagent.agents import build_assistant
from minisweagent.agents.session import SessionStore
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models import get_model

config = yaml.safe_load((package_dir / "config" / "mini.yaml").read_text())
store = SessionStore(config["session"]["directory"])

with store.create(str(Path.cwd())) as session:
    agent = build_assistant(
        get_model(config={"model_name": "deepseek/deepseek-v4-flash", **config["model"]}),
        LocalEnvironment(cwd=session.workspace, **config["environment"]),
        session,
        store,
        config,
    )
    print(agent.receive("Inspect this repository and explain its entry point."))
```

For the complete loop, see [Agent control flow](control_flow.md).

{% include-markdown "_footer.md" %}
