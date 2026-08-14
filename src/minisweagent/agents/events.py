from __future__ import annotations

import logging
from typing import Any, Protocol

from rich.console import Console

from minisweagent.agents.schema import RunEvent, RunState, SessionState

logger = logging.getLogger(__name__)


class EventSink(Protocol):
    def emit(self, event: RunEvent) -> None: ...


class CompositeEventSink:
    def __init__(self, sinks: list[EventSink] | None = None):
        self.sinks = sinks or []

    def emit(self, event: RunEvent) -> None:
        for sink in self.sinks:
            try:
                sink.emit(event)
            except Exception:
                logger.exception("Event sink %r failed while handling %s", sink, event.type)


class RecordingEventSink:
    def __init__(self):
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)


class ConsoleEventSink:
    def __init__(self, console: Console | None = None):
        self.console = console or Console(highlight=False)

    def emit(self, event: RunEvent) -> None:
        payload = event.payload
        if event.type == "model.started" and payload.get("kind") == "decision":
            self.console.print("[dim]正在分析下一步…[/dim]")
        elif event.type == "context.compaction.started":
            self.console.print("[dim]正在压缩上下文…[/dim]")
        elif event.type == "context.compaction.completed":
            self.console.print("[dim]上下文压缩完成[/dim]")
        elif event.type == "tool.proposed":
            self.console.print(f"[dim]准备：{payload.get('call_title', payload.get('tool_name', '工具调用'))}[/dim]")
        elif event.type == "approval.requested":
            self.console.print(f"[yellow]等待批准：{payload.get('call_title', '工具调用')}[/yellow]")
        elif event.type == "tool.started":
            self.console.print(f"[dim]正在执行：{payload.get('call_title', payload.get('tool_name', '工具调用'))}[/dim]")
        elif event.type == "tool.resolved":
            marker = "✓" if payload.get("status") == "success" else "×"
            self.console.print(f"[dim]{marker} {payload.get('result_title', '工具步骤已结束')}[/dim]")
        elif event.type == "context.usage.updated":
            window, used = payload.get("context_window"), payload.get("input_tokens")
            if window is not None and used is not None:
                self.console.print(f"[dim]上下文 {used:,} / {window:,} · 剩余 {max(window - used, 0):,}[/dim]")


class EventBus:
    def __init__(self, session: SessionState, sink: EventSink | None = None):
        self.session = session
        self.sink = sink or CompositeEventSink()
        self.state: RunState = session.events[-1].state if session.events else "IDLE"

    def emit(
        self,
        event_type: str,
        *,
        turn_id: str | None = None,
        step_id: str | None = None,
        durable: bool = True,
        state: RunState | None = None,
        **payload: Any,
    ) -> RunEvent:
        if state is not None:
            self.state = state
        event = RunEvent(
            sequence=self.session.next_event_sequence,
            session_id=self.session.session_id,
            turn_id=turn_id,
            step_id=step_id,
            type=event_type,
            state=self.state,
            durable=durable,
            payload=payload,
        )
        self.session.next_event_sequence += 1
        if durable:
            self.session.events.append(event)
        self.sink.emit(event)
        return event
