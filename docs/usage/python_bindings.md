# Python bindings

The smallest binding example uses the same `AssistantAgent` as the CLI:

```python
from minisweagent.run.hello_world import main

agent = main(
    task="Write and verify a hello world program",
    model_name="deepseek/deepseek-v4-flash",
)
```

For custom tools, event sinks, session locations, or environments, compose the Agent with `build_assistant()` as shown in the [extension guide](../advanced/cookbook.md). Send later messages with `agent.receive(text)` while the session lease remains open; for long-lived applications, keep the `SessionStore.resume(session_id)` context manager open for the lifetime of that conversation owner.

{% include-markdown "../_footer.md" %}
