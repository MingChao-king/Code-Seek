# CodeSeek v0.1

CodeSeek is a persistent terminal coding agent. It keeps one continuous conversation, lets the model answer directly or call tools, and preserves the complete session while compacting older context into inspectable memory.

## Run

From this repository:

```bash
cd /Users/yuanyu.cao/Desktop/agent/mini-swe-agent
.venv/bin/codeseek
```

Choose a workspace explicitly:

```bash
codeseek --workspace /path/to/repository
```

Resume an existing conversation:

```bash
codeseek --resume ses_0123456789abcdef0123456789abcdef
```

Select from recent conversations:

```bash
codeseek --sessions
```

Add `--auto-approve` when valid side-effecting tool calls should run without pausing for confirmation.

## Conversation Commands

Type `/` to display command suggestions immediately; no first Enter is required. Use the up and down arrow keys to choose a command, then press Enter to execute it. Commands with arguments can still be typed in full.

- `/compact [focus]`: compact eligible older conversation. Optional text tells the summary model what must receive special attention.
- `/compress [focus]`: compatibility alias for `/compact`.
- `/auto`: automatically approve valid side-effecting tool calls from this point onward.
- `/ask`: require approval for each side-effecting tool call.
- `/approval`: display the current approval policy.
- `/limit`: inspect or change user-selected output, model-call, tool-call, cost, and time limits.
- `/memory`: inspect or revise the active conversation-memory tree.
- `/exit`: save the session and leave CodeSeek.

Approval changes are saved with the Session and restored in later terminals. Sessions created before this feature continue using the startup policy until `/auto` or `/ask` is explicitly selected.

New sessions have no user policy limits. Automatic context compaction still protects the provider context window.

Before every interactive input prompt, CodeSeek displays the current main ContextView usage, percentage, and remaining window capacity.

## Features

- **One loop, no rigid pipelines** — there is no fixed question/plan/execute/review workflow. The model decides between answering directly and calling a tool; the scaffold enforces protocol correctness, approvals, limits, and persistence.
- **Lossless sessions, bounded model views** — every message stays on disk while older turns are compacted into an immutable, inspectable memory tree with a strict "must shrink" invariant (leaf summaries, then level-merged batches).
- **Native tool calling** — `bash` and `conversation_history` share one structured protocol and can be extended through the tool registry.
- **Human-in-the-loop** — side-effecting tool calls pause for per-call approval (ask/auto); every model and tool step leaves a durable event checkpoint, so interrupted turns are never silently replayed.
- **DeepSeek V4 ready** — includes a V4 tokenizer encoding adapter and defaults to `deepseek/deepseek-v4-flash` (litellm-backed; OpenRouter, Portkey, and Requesty adapters also ship).
- **Portable execution** — the same `Environment` protocol backs local execution, docker/podman, singularity/apptainer, bubblewrap, contree, and swerex sandboxes.

## Project layout

```
src/minisweagent/
  __init__.py          # Protocols (Model, Environment, Agent) + global config
  agents/              # AssistantAgent loop, tools, session store, events
  context.py           # ContextManager: token counting and memory-tree compaction
  environments/        # local, docker, singularity, and extra sandboxes
  models/              # litellm, openrouter, portkey, requesty adapters
  run/                 # codeseek CLI, hello_world, benchmark runners
docs/                  # MkDocs documentation
tests/                 # pytest suite
```

## Compatibility

- Python >= 3.10 (tested on 3.12).
- Sessions and credentials created before the CodeSeek rename remain in the original local configuration directory and can still be resumed.
- `mini`, `mini-swe-agent`, `mini-extra` are retained as CLI aliases.
- MIT licensed.

## Design

CodeSeek has one built-in `AssistantAgent`:

- The model interprets natural language and decides whether to answer or use tools.
- The Agent validates tool calls, applies approval, records events, and continues until the model returns a final answer.
- `SessionStore` persists messages, tool results, events, limits, and memory with single-writer locking.
- `ContextManager` keeps recent turns verbatim and summarizes older turns into an immutable, inspectable tree.
- Existing sessions and credentials remain in the original local configuration directory so conversations created before the CodeSeek rename can still be resumed.

## Verify

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/mkdocs build --strict
```
