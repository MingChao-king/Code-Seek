# Agent implementation

* `assistant.py` - The single built-in model-driven Agent loop.
* `schema.py` - Provider-neutral messages, tool calls/results, sessions, memory, usage, and events.
* `session.py` - Atomic persistent sessions and single-writer locking.
* `tools.py` - Extensible native tool registry, Bash, and conversation-history lookup.
* `events.py` - Durable run events plus replaceable display sinks.
* `assembly.py` - Composition root for Model, Environment, tools, context, session, and Agent.
