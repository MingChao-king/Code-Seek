# YAML configuration

The final Agent uses `src/minisweagent/config/mini.yaml` as its complete default configuration.

```yaml
--8<-- "src/minisweagent/config/mini.yaml"
```

## Top-level sections

| Section | Purpose |
|---|---|
| `agent` | Stable model instructions, `ask`/`auto` approval policy, and native-response retry protection |
| `model` | Model name override, provider kwargs, and optional verified capability overrides |
| `context` | Summary prompt, 80% trigger, 20% target, two-turn raw retention, summary budget, and safety margin |
| `tools` | Enabled native tools and persisted-result truncation limits |
| `session` | Persistent session directory; `null` uses the platform config directory |
| `events` | Display sinks; durable events are always stored in the Session by the Agent |
| `environment` | Bash execution environment and its settings |

There is no Agent class selector, text action regex, submission marker, task template, or fixed request-mode configuration.

## Merge overrides

`-c` can be repeated. If it is specified at all, include the default file explicitly, then add later overrides:

```bash
mini \
  -c src/minisweagent/config/mini.yaml \
  -c agent.approval_policy=auto \
  -c context.keep_recent_turns=3
```

Nested key-value values are parsed as YAML, so numbers, booleans, lists, and `null` keep their types.

## Model capabilities

Known providers supply verified `context_window` and `max_output_tokens` through the adapter. For a custom endpoint, configure only values verified from that provider:

```yaml
model:
  model_name: custom/provider-model
  capabilities:
    context_window: 128000
    max_output_tokens: 16384
```

Do not invent a default output limit. Normal calls omit it unless the user enables `/limit output` or the provider requires the parameter; required providers need a verified technical limit.

## Benchmark-only settings

Benchmark files may also define `run.env_startup_command`; the runner renders it from instance data and checks its exit status before constructing the Agent. This is runner configuration, not an Agent workflow.

{% include-markdown "_footer.md" %}
