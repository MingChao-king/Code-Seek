# Session and benchmark output

## Persistent sessions

The terminal stores one JSON document per conversation in the configured session directory. Files use mode `0600`; the directory uses `0700`. Writes use a temporary file, `fsync`, and atomic replacement while a per-session lock enforces one writer.

```json
{
  "schema_version": 1,
  "session_id": "ses_...",
  "workspace": "/absolute/workspace",
  "messages": [
    {"message_id": "msg_...", "turn_id": "turn_...", "role": "user", "content": "..."},
    {
      "message_id": "msg_...",
      "turn_id": "turn_...",
      "role": "assistant",
      "content": "I will inspect it.",
      "tool_calls": [{"id": "call_...", "name": "bash", "arguments": {"command": "ls"}}]
    },
    {
      "message_id": "msg_...",
      "turn_id": "turn_...",
      "role": "tool",
      "tool_call_id": "call_...",
      "content": "{\"tool_call_id\":\"call_...\",\"name\":\"bash\",\"status\":\"success\",...}"
    }
  ],
  "memory": {
    "raw_compaction_cursor": 0,
    "active_batch_ids": [],
    "batches": {}
  },
  "usage": {"model_calls": 2, "tool_calls": 1, "cost": 0.0, "unknown_cost_calls": 0},
  "limits": {
    "max_output_tokens": null,
    "model_calls": null,
    "tool_calls": null,
    "cost_usd": null,
    "wall_time_seconds": null
  },
  "events": [],
  "next_event_sequence": 1,
  "context_usage": {}
}
```

`messages` is the complete lossless conversation. Context compression never deletes it. `memory` stores immutable summary nodes and the active summary frontier used to build the provider-visible ContextView. `events` stores durable lifecycle boundaries; output deltas are intentionally not persisted.

## Benchmark trajectories

SWE-bench and ProgramBench save the same Session fields in each `.traj.json`, plus benchmark-specific `instance_id` and `info`. SWE-bench obtains the submitted patch from `git diff`; it does not depend on a magic submission command or an Agent subclass.

`preds.json` remains SWE-bench compatible:

```json
{
  "owner__repo__123": {
    "model_name_or_path": "deepseek/deepseek-v4-flash",
    "instance_id": "owner__repo__123",
    "model_patch": "diff --git ..."
  }
}
```

Use the [inspector](inspector.md) to browse either a legacy message list or the `messages` array inside a Session/benchmark object.

{% include-markdown "../_footer.md" %}
