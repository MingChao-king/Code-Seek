# `codeseek`

CodeSeek is a continuous terminal conversation backed by persistent sessions.

## Start And Resume

```bash
codeseek
codeseek --workspace /path/to/repository
codeseek --resume ses_0123456789abcdef0123456789abcdef
codeseek --sessions
codeseek --task "Explain this repository"
```

Every invocation creates a new session unless `--resume` or `--sessions` is used. The CLI prints the session ID and a reusable recovery command. A session has single-writer locking, so two processes cannot modify the same conversation simultaneously.

Useful options:

- `-m` / `--model`: override the configured model.
- `--model-class`: choose a Model adapter.
- `--environment-class`: choose the execution environment.
- `-c` / `--config`: load the default YAML plus optional overrides.
- `--auto-approve`: approve valid side-effecting tools automatically. Without it, CodeSeek asks before execution.

## Conversation Commands

Type `/` to display command suggestions immediately, without pressing Enter first. Use the up and down arrow keys to choose a command, then press Enter. You can also keep typing a full command and its arguments.

- `/compact`: manually compact eligible older history.
- `/compact <focus>`: compact while asking the summary model to pay special attention to the supplied text.
- `/compress [focus]`: compatibility alias for `/compact`.
- `/auto`: switch the current Session to automatic approval.
- `/ask`: switch the current Session to per-call approval.
- `/approval`: display the effective approval policy.
- `/limit`: show limits.
- `/limit <output|model-calls|tool-calls|cost|time> <value>`: set a user-selected limit.
- `/limit clear <field|all>`: remove limits.
- `/memory`: inspect active and historical summary nodes.
- `/memory revise <active_id>`: create an immutable user revision for an active memory node.
- `/memory restore <active_id> <version_id>`: restore an older version as a new active version.
- `/exit`: close the terminal while keeping the session recoverable.

`/compact` does not create a user message. Its optional focus text is used only for that compaction request. Complete messages and previous memory nodes remain stored.

`/auto`, `/ask`, and `/approval` are local controls and do not create user messages or call the model. A policy selected with `/auto` or `/ask` is stored in the Session and restored later. Older Session files have no override and continue using the startup configuration until the user explicitly changes it.

Before every interactive prompt, CodeSeek recomputes and displays the current main ContextView usage:

```text
上下文 44,385 / 1,000,000（4.4%） · 剩余 955,615
```

This read-only snapshot is not written to the Session and does not create an event.

## Compatibility

The CodeSeek rename does not migrate or rewrite session files. Existing session IDs, messages, memory trees, credentials, configuration environment variables, and the original local storage directory remain valid.

## Implementation

??? note "Default configuration"

    ```yaml
    --8<-- "src/minisweagent/config/mini.yaml"
    ```

??? note "CLI source"

    ```python
    --8<-- "src/minisweagent/run/mini.py"
    ```

??? note "Agent source"

    ```python
    --8<-- "src/minisweagent/agents/assistant.py"
    ```

{% include-markdown "../_footer.md" %}
