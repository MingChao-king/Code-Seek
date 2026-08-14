#!/usr/bin/env python3

"""Run the continuous CodeSeek conversation in a local environment."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from prompt_toolkit import HTML, PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from rich.console import Console

from minisweagent.agents import AssistantAgent, build_assistant
from minisweagent.agents.events import ConsoleEventSink
from minisweagent.agents.schema import ContextUsage, RunEvent, SessionLimits, ToolCall
from minisweagent.agents.session import SessionStore
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.run.utilities.config import configure_if_first_time
from minisweagent.utils.serialize import UNSET, recursive_merge

DEFAULT_CONFIG_FILE = Path(os.getenv("MSWEA_MINI_CONFIG_PATH", builtin_config_dir / "mini.yaml"))

console = Console(highlight=False)
app = typer.Typer(rich_markup_mode="rich", add_completion=False)


@dataclass(frozen=True)
class ControlCommand:
    name: str
    description: str


CONTROL_COMMANDS = (
    ControlCommand("/compact", "立即压缩旧对话；可在后面补充需要重点保留的内容"),
    ControlCommand("/auto", "切换为自动批准合法的副作用工具调用"),
    ControlCommand("/ask", "切换为每次副作用工具调用都询问批准"),
    ControlCommand("/approval", "查看当前审批策略"),
    ControlCommand("/limit", "查看或设置输出、调用次数、费用和时间限制"),
    ControlCommand("/memory", "查看或修订当前会话的摘要树"),
    ControlCommand("/exit", "保存当前会话并退出"),
)


class ControlCommandCompleter(Completer):
    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or any(character.isspace() for character in text):
            return
        for command in CONTROL_COMMANDS:
            if command.name.startswith(text):
                yield Completion(
                    command.name,
                    start_position=-len(text),
                    display=command.name,
                    display_meta=command.description,
                )


def _create_input_session(*, input=None, output=None) -> PromptSession:
    bindings = KeyBindings()

    @bindings.add("/")
    def show_commands(event) -> None:
        buffer = event.current_buffer
        buffer.insert_text("/")
        if buffer.text == "/" and buffer.cursor_position == 1:
            buffer.start_completion(select_first=True)

    @bindings.add("enter", eager=True)
    def accept_input(event) -> None:
        buffer = event.current_buffer
        if buffer.complete_state is not None and buffer.complete_state.current_completion is not None:
            buffer.apply_completion(buffer.complete_state.current_completion)
        buffer.validate_and_handle()

    app_kwargs = {}
    if input is not None:
        app_kwargs["input"] = input
    if output is not None:
        app_kwargs["output"] = output
    return PromptSession(
        HTML("<b>你想做什么？</b> "),
        completer=ControlCommandCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        reserve_space_for_menu=len(CONTROL_COMMANDS),
        key_bindings=bindings,
        **app_kwargs,
    )


def _approve(call: ToolCall, title: str) -> tuple[bool, str]:
    console.print(f"\n[yellow]{title}[/yellow]")
    console.print_json(data=call.arguments)
    answer = console.input("[bold]回车批准；输入意见拒绝：[/bold] ").strip()
    return (True, "") if not answer else (False, answer)


def _select_session(store: SessionStore) -> str | None:
    if not sys.stdin.isatty():
        raise typer.BadParameter("--sessions requires an interactive terminal; use --resume <session_id>")
    sessions = store.list_recent()
    if not sessions:
        console.print("尚无可恢复会话。")
        return None
    for index, item in enumerate(sessions, 1):
        preview = item.last_user_message or "(empty)"
        console.print(f"{index:>2}. {item.session_id}  {item.updated_at:%Y-%m-%d %H:%M}  {item.workspace}\n    {preview}")
    answer = console.input("选择会话序号，直接回车取消：").strip()
    if not answer:
        return None
    try:
        return sessions[int(answer) - 1].session_id
    except (ValueError, IndexError) as error:
        raise typer.BadParameter("Invalid session selection") from error


def _show_limits(limits: SessionLimits) -> None:
    values = {
        "output": limits.max_output_tokens,
        "model-calls": limits.model_calls,
        "tool-calls": limits.tool_calls,
        "cost": limits.cost_usd,
        "time": limits.wall_time_seconds,
    }
    console.print("  ".join(f"{key}={value if value is not None else 'off'}" for key, value in values.items()))


def _format_context_usage(usage: ContextUsage) -> str:
    if usage.input_tokens is None:
        return "上下文：尚未计算"
    if usage.context_window is None:
        return f"上下文 {usage.input_tokens:,} tokens · 总窗口未知"
    ratio = usage.usage_ratio if usage.usage_ratio is not None else usage.input_tokens / usage.context_window
    remaining = usage.remaining_tokens
    if remaining is None:
        remaining = max(usage.context_window - usage.input_tokens, 0)
    return (
        f"上下文 {usage.input_tokens:,} / {usage.context_window:,}（{ratio:.1%}）"
        f" · 剩余 {remaining:,}"
    )


def _show_context_usage(agent: AssistantAgent) -> None:
    console.print(f"[dim]{_format_context_usage(agent.context_usage_snapshot())}[/dim]")


def _parse_duration(value: str) -> float:
    suffixes = {"s": 1, "m": 60, "h": 3600}
    if value[-1:].lower() in suffixes:
        return float(value[:-1]) * suffixes[value[-1].lower()]
    return float(value)


def _handle_control(agent: AssistantAgent, text: str) -> bool:
    parts = text.split()
    if parts[0] in {"/compact", "/compress"}:
        focus = text[len(parts[0]) :].strip()
        result = agent.compress(focus)
        if result.changed:
            console.print(f"上下文压缩完成：{result.before_tokens:,} → {result.after_tokens:,} tokens")
        else:
            console.print("当前没有可进一步压缩的旧对话；最近两轮继续保留原文。")
        return True
    if parts[0] in {"/auto", "/ask", "/approval"}:
        if len(parts) != 1:
            raise typer.BadParameter(f"Use {parts[0]}")
        if parts[0] == "/auto":
            agent.set_approval_policy("auto")
            console.print("审批策略：auto（合法的副作用工具调用将自动批准）")
        elif parts[0] == "/ask":
            agent.set_approval_policy("ask")
            console.print("审批策略：ask（副作用工具调用将逐次询问）")
        else:
            console.print(f"审批策略：{agent.approval_policy}")
        return True
    if parts[0] == "/limit":
        if len(parts) == 1:
            _show_limits(agent.session.limits)
            return True
        if parts[1] == "clear":
            if len(parts) != 3:
                raise typer.BadParameter("Use /limit clear <field|all>")
            if parts[2] == "all":
                agent.clear_limits()
            else:
                agent.update_limit(parts[2], None)
            _show_limits(agent.session.limits)
            return True
        if len(parts) != 3:
            raise typer.BadParameter("Use /limit <output|model-calls|tool-calls|cost|time> <value>")
        field, raw = parts[1], parts[2]
        value: int | float = _parse_duration(raw) if field == "time" else float(raw) if field == "cost" else int(raw)
        agent.update_limit(field, value)
        _show_limits(agent.session.limits)
        return True
    if parts[0] == "/memory":
        if len(parts) == 1:
            console.print_json(data=agent.memory_snapshot())
            return True
        if parts[1] == "revise" and len(parts) == 3:
            content = console.input("新的摘要正文：")
            batch_id = agent.revise_memory(parts[2], content, agent.session.next_event_sequence - 1)
            console.print(f"已创建摘要版本 {batch_id}")
            return True
        if parts[1] == "restore" and len(parts) == 4:
            batch_id = agent.restore_memory(parts[2], parts[3], agent.session.next_event_sequence - 1)
            console.print(f"已恢复为新摘要版本 {batch_id}")
            return True
        raise typer.BadParameter("Use /memory, /memory revise <active_id>, or /memory restore <active_id> <version_id>")
    return False


def _read_user_input(input_session: PromptSession | None) -> str:
    if input_session is None:
        return console.input("\n[bold yellow]你想做什么？[/bold yellow] ")
    return input_session.prompt()


def _run_input_loop(agent: AssistantAgent, initial_task: str | None) -> None:
    pending = initial_task
    input_session = _create_input_session() if sys.stdin.isatty() else None
    while True:
        if pending is not None:
            text, pending = pending, None
        else:
            try:
                _show_context_usage(agent)
                text = _read_user_input(input_session).strip()
            except EOFError:
                break
        if not text:
            continue
        if text == "/":
            continue
        if text == "/exit":
            break
        try:
            if text.startswith("/") and _handle_control(agent, text):
                continue
            answer = agent.receive(text)
            console.print("\n[bold green]Agent[/bold green]")
            console.print(answer, markup=False)
        except Exception as error:
            console.print(f"[bold red]{type(error).__name__}:[/bold red] {error}")
        if initial_task is not None and not sys.stdin.isatty():
            break


@app.command(help=__doc__)
def main(
    model_name: str | None = typer.Option(None, "-m", "--model", help="Model to use"),
    model_class: str | None = typer.Option(None, "--model-class", help="Model adapter class"),
    environment_class: str | None = typer.Option(None, "--environment-class", help="Environment class"),
    task: str | None = typer.Option(None, "-t", "--task", help="Initial user message", show_default=False),
    auto_approve: bool = typer.Option(False, "--auto-approve", help="Approve valid side-effecting tools automatically"),
    resume: str | None = typer.Option(None, "--resume", help="Resume a session by ID"),
    sessions: bool = typer.Option(False, "--sessions", help="Choose from recent sessions"),
    workspace: Path | None = typer.Option(None, "--workspace", help="Workspace for a new session"),
    config_spec: list[str] = typer.Option([str(DEFAULT_CONFIG_FILE)], "-c", "--config", help="Configuration sources"),
) -> Any:
    if resume and sessions:
        raise typer.BadParameter("--resume and --sessions are mutually exclusive")
    configure_if_first_time()
    configs = [get_config_from_spec(spec) for spec in config_spec]
    configs.append(
        {
            "agent": {"approval_policy": "auto" if auto_approve else UNSET},
            "model": {"model_name": model_name or UNSET, "model_class": model_class or UNSET},
            "environment": {"environment_class": environment_class or UNSET},
        }
    )
    config = recursive_merge(*configs)
    store = SessionStore(config.get("session", {}).get("directory"))
    session_id = _select_session(store) if sessions else resume
    if sessions and session_id is None:
        return None
    lease = store.resume(session_id) if session_id else store.create(str(workspace or Path.cwd()))
    with lease as session:
        if workspace is not None and str(workspace.expanduser().resolve()) != session.workspace:
            if not sys.stdin.isatty() or console.input(f"将 workspace 从 {session.workspace} 改为 {workspace.resolve()}？[y/N] ").lower() != "y":
                raise typer.BadParameter("Workspace change was not confirmed")
            old_workspace = session.workspace
            session.workspace = str(workspace.expanduser().resolve())
            session.events.append(
                RunEvent(
                    sequence=session.next_event_sequence,
                    session_id=session.session_id,
                    type="session.workspace_changed",
                    state="IDLE",
                    payload={"old_workspace": old_workspace, "new_workspace": session.workspace},
                )
            )
            session.next_event_sequence += 1
            store.save(session)
            console.print(f"Workspace: {old_workspace} → {session.workspace}")
        model = get_model(config=config.get("model", {}))
        environment_config = dict(config.get("environment", {}))
        environment_config["cwd"] = session.workspace
        environment = get_environment(environment_config, default_type="local")
        sinks = [ConsoleEventSink(console)] if "console" in config.get("events", {}).get("sinks", []) else []
        agent = build_assistant(
            model,
            environment,
            session,
            store,
            config,
            event_sinks=sinks,
            approve=_approve,
        )
        console.print(f"Session: [bold green]{session.session_id}[/bold green]")
        console.print(f"Resume later: codeseek --resume {session.session_id}")
        _run_input_loop(agent, task)
        console.print(f"\nResume later: codeseek --resume {session.session_id}")
        return agent


if __name__ == "__main__":
    app()
