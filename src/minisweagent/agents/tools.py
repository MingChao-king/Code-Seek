from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal, Protocol

from minisweagent import Environment
from minisweagent.agents.schema import Message, SessionState, ToolCall, ToolResult, ToolSpec


class ToolValidationError(ValueError):
    pass


class Tool(Protocol):
    spec: ToolSpec
    requires_approval: bool

    def validate(self, arguments: dict[str, Any]) -> None: ...

    def describe_call(self, call: ToolCall) -> str: ...

    def describe_result(self, call: ToolCall, result: ToolResult) -> str: ...

    def execute(
        self,
        call: ToolCall,
        on_output: Callable[[Literal["stdout", "stderr"], str], None] | None = None,
    ) -> ToolResult: ...


def _truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    half = max((limit - 80) // 2, 1)
    return f"{value[:half]}\n... {len(value) - half * 2} characters omitted ...\n{value[-half:]}", True


class BashTool:
    requires_approval = True
    spec = ToolSpec(
        name="bash",
        description="在当前环境中执行 Bash 命令，用于检查文件、修改代码和运行验证",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "purpose": {"type": "string", "description": "这条命令的简短用户可见目的"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )

    def __init__(self, environment: Environment, workspace: Callable[[], str], max_result_chars: int = 10000):
        self.environment = environment
        self.workspace = workspace
        self.max_result_chars = max_result_chars

    def validate(self, arguments: dict[str, Any]) -> None:
        if set(arguments) - {"command", "purpose"}:
            raise ToolValidationError("bash received unknown arguments")
        if not isinstance(arguments.get("command"), str) or not arguments["command"].strip():
            raise ToolValidationError("bash.command must be a non-empty string")
        if "purpose" in arguments and not isinstance(arguments["purpose"], str):
            raise ToolValidationError("bash.purpose must be a string")

    def describe_call(self, call: ToolCall) -> str:
        purpose = call.arguments.get("purpose")
        return purpose.strip() if isinstance(purpose, str) and purpose.strip() else "运行 Bash 命令"

    def describe_result(self, call: ToolCall, result: ToolResult) -> str:
        if not result.executed:
            return f"未执行 Bash：{result.content}"
        if result.status == "success":
            return f"Bash 命令执行完成（exit {result.exit_code}）"
        return f"Bash 命令执行失败（exit {result.exit_code}）"

    def execute(
        self,
        call: ToolCall,
        on_output: Callable[[Literal["stdout", "stderr"], str], None] | None = None,
    ) -> ToolResult:
        self.validate(call.arguments)
        try:
            execute_stream = getattr(self.environment, "execute_stream", None)
            if callable(execute_stream):
                output = execute_stream(
                    {"command": call.arguments["command"]},
                    cwd=self.workspace(),
                    on_output=on_output,
                )
                streamed = True
            else:
                output = self.environment.execute({"command": call.arguments["command"]}, cwd=self.workspace())
                streamed = False
            content = str(output.get("output", ""))
            if exception := output.get("exception_info"):
                content = f"{content}\n{exception}".strip()
            content, truncated = _truncate(content, self.max_result_chars)
            if on_output is not None and content and not streamed:
                on_output("stdout", content)
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                status="success" if output.get("returncode") == 0 else "error",
                content=content,
                exit_code=output.get("returncode"),
                truncated=truncated,
            )
        except Exception as error:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                status="error",
                content=f"Tool execution failed: {type(error).__name__}: {error}",
                executed=True,
            )


class ConversationHistoryTool:
    requires_approval = False
    spec = ToolSpec(
        name="conversation_history",
        description="查看当前会话的摘要批次结构，或分页读取某个摘要批次覆盖的原始消息",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["inspect", "read"]},
                "batch_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "content_offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "required": ["action", "batch_id"],
            "additionalProperties": False,
        },
    )

    def __init__(self, session: Callable[[], SessionState], max_result_chars: int = 20000):
        self.session = session
        self.max_result_chars = max_result_chars

    def validate(self, arguments: dict[str, Any]) -> None:
        allowed = {"action", "batch_id", "offset", "content_offset", "limit"}
        if set(arguments) - allowed:
            raise ToolValidationError("conversation_history received unknown arguments")
        if arguments.get("action") not in {"inspect", "read"}:
            raise ToolValidationError("action must be inspect or read")
        if not isinstance(arguments.get("batch_id"), str) or not arguments["batch_id"]:
            raise ToolValidationError("batch_id must be a non-empty string")
        for field in ("offset", "content_offset"):
            if field in arguments and (not isinstance(arguments[field], int) or arguments[field] < 0):
                raise ToolValidationError(f"{field} must be a non-negative integer")
        limit = arguments.get("limit", 10)
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ToolValidationError("limit must be between 1 and 20")

    def describe_call(self, call: ToolCall) -> str:
        return "查看摘要结构" if call.arguments.get("action") == "inspect" else "回查原始会话"

    def describe_result(self, call: ToolCall, result: ToolResult) -> str:
        return "已读取会话历史" if result.status == "success" else f"会话历史读取失败：{result.content}"

    def execute(
        self,
        call: ToolCall,
        on_output: Callable[[Literal["stdout", "stderr"], str], None] | None = None,
    ) -> ToolResult:
        try:
            self.validate(call.arguments)
            batch = self.session().memory.batches.get(call.arguments["batch_id"])
            if batch is None:
                raise ToolValidationError(f"Unknown memory batch: {call.arguments['batch_id']}")
            payload = self._inspect(batch.batch_id) if call.arguments["action"] == "inspect" else self._read(call)
            content = json.dumps(payload, ensure_ascii=False)
            if on_output is not None:
                on_output("stdout", content)
            return ToolResult(tool_call_id=call.id, name=call.name, status="success", content=content)
        except ToolValidationError as error:
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                status="error",
                content=str(error),
                executed=False,
            )

    def _inspect(self, batch_id: str) -> dict[str, Any]:
        memory = self.session().memory
        batch = memory.batches[batch_id]
        return {
            "batch": batch.model_dump(mode="json"),
            "active": batch_id in memory.active_batch_ids,
            "children": [memory.batches[child].model_dump(mode="json") for child in batch.source_batch_ids],
        }

    def _read(self, call: ToolCall) -> dict[str, Any]:
        session = self.session()
        batch = session.memory.batches[call.arguments["batch_id"]]
        offset = call.arguments.get("offset", 0)
        content_offset = call.arguments.get("content_offset", 0)
        limit = call.arguments.get("limit", 10)
        size = batch.end_message_index - batch.start_message_index
        if offset >= size:
            raise ToolValidationError("offset is outside this memory batch")
        messages = session.messages[batch.start_message_index + offset : batch.end_message_index]
        records: list[dict[str, Any]] = []
        used = 0
        next_offset = offset
        next_content_offset = 0
        for message in messages[:limit]:
            record = self._record(message, batch.start_message_index + next_offset)
            content = record["content"]
            if content_offset:
                if content_offset >= len(content):
                    raise ToolValidationError("content_offset is outside the selected message")
                content = content[content_offset:]
            record["content"] = content
            encoded = json.dumps(record, ensure_ascii=False)
            if used + len(encoded) > self.max_result_chars:
                if records:
                    break
                available = max(self.max_result_chars - 500, 1)
                record["content"] = content[:available]
                record["content_start"] = content_offset
                record["content_end"] = content_offset + len(record["content"])
                record["content_complete"] = record["content_end"] >= len(message.content)
                next_content_offset = 0 if record["content_complete"] else record["content_end"]
                records.append(record)
                if record["content_complete"]:
                    next_offset += 1
                break
            records.append(record)
            used += len(encoded)
            next_offset += 1
            content_offset = 0
        return {
            "batch_id": batch.batch_id,
            "messages": records,
            "next_offset": next_offset,
            "next_content_offset": next_content_offset,
            "has_more": next_offset < size or next_content_offset > 0,
        }

    @staticmethod
    def _record(message: Message, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "message_id": message.message_id,
            "turn_id": message.turn_id,
            "role": message.role,
            "content": message.content,
            "tool_calls": [call.model_dump(mode="json") for call in message.tool_calls],
            "tool_call_id": message.tool_call_id,
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        names = [tool.spec.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique")
        self._tools = {tool.spec.name: tool for tool in tools}

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def describe_call_or_fallback(self, call: ToolCall) -> str:
        tool = self.get(call.name)
        if tool is None:
            return f"请求未知工具 {call.name}"
        try:
            return tool.describe_call(call).strip().replace("\n", " ")[:160]
        except Exception:
            return f"调用 {call.name}"

    def describe_result_or_fallback(self, call: ToolCall, result: ToolResult) -> str:
        tool = self.get(call.name)
        if tool is None:
            return f"未知工具 {call.name} 未执行"
        try:
            return tool.describe_result(call, result).strip().replace("\n", " ")[:200]
        except Exception:
            return f"{call.name}：{result.status}"
