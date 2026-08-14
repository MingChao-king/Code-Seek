# CodeSeek v0.1

CodeSeek is a continuous terminal coding agent. A single `AssistantAgent` reads the conversation, answers directly when possible, and uses registered tools when facts or actions are required.

## Start

```bash
codeseek
```

Choose a workspace:

```bash
codeseek --workspace /path/to/repository
```

Resume a saved conversation:

```bash
codeseek --resume ses_0123456789abcdef0123456789abcdef
```

## Terminal Commands

Type `/` to display command suggestions immediately, then use the up and down arrow keys to choose an action. No first Enter is required.

- `/compact [focus]` manually compacts eligible older history. Optional text tells the summary model what should receive special attention.
- `/compress [focus]` remains available as a compatibility alias.
- `/auto` enables automatic approval for the current Session.
- `/ask` restores per-call approval.
- `/approval` shows the current approval policy.
- `/limit` inspects or changes user-selected limits.
- `/memory` inspects or revises the conversation-memory tree.
- `/exit` saves and releases the current session.

## Core Behavior

- One model-driven loop handles questions, planning, execution, correction, and final reporting.
- Native tool calls are validated, approved, executed, persisted, and returned to the model.
- Complete messages remain on disk while older active context can become an inspectable summary tree.
- Sessions can be opened in separate terminals and resumed by ID.
- Context usage, model activity, tool execution, approval, and compaction produce structured runtime events.
- The current main ContextView usage and remaining capacity are shown before every input prompt.
- The `codeseek` rename preserves the existing local Session and credential directory so conversations created earlier continue to work.

See [command-line usage](usage/mini.md), [control flow](advanced/control_flow.md), and the [API reference](reference/index.md) for details.
