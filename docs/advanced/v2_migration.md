# Migration to the continuous AssistantAgent

This Entry Task 3 version replaces the earlier single-run Agent variants with one persistent, model-driven `AssistantAgent`.

## CLI changes

- Replace `--yolo` with `--auto-approve`.
- Replace saved trajectory paths with persistent sessions: use `--resume <session_id>` or `--sessions`.
- Replace `--exit-immediately` with `--task`; a non-interactive invocation exits after that turn completes.
- Agent class selection and `--agent-class` no longer exist.
- `human`, `confirm`, and `yolo` modes are removed. Approval is `ask` or `auto`.

## Python changes

- Replace `DefaultAgent.run(task)` or `InteractiveAgent.run(task)` with the composition root `build_assistant(...)` and `AssistantAgent.receive(text)`.
- Hold a `SessionStore.create()` or `SessionStore.resume()` lease while the Agent owns the conversation.
- Model adapters now return normalized `ModelResponse` values with native `ToolCall` objects.
- Tool execution returns one structured `ToolResult` per call. Text code-block action parsers and magic submission markers are removed.

## Context and output changes

- `SessionState.messages` is the complete persistent history.
- `ContextManager` constructs the bounded provider-visible `ContextView` and maintains immutable summary-tree memory.
- Durable `RunEvent` records replace subclass hooks for terminal display, benchmark progress, and future UI integrations.
- Benchmarks use the same Agent and collect their result explicitly, such as `git diff` for SWE-bench.

See [Agent control flow](control_flow.md), [`mini`](../usage/mini.md), and [session output](../usage/output_files.md) for the final contracts.

{% include-markdown "_footer.md" %}
