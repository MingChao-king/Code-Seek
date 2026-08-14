"""Prompt-only subset of DeepSeek's official V4 reference encoder.

The format and constants follow the MIT-licensed reference implementation at
https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/blob/a7aaed8/encoding/encoding_dsv4.py.
Only encoding is included because this adapter receives structured OpenAI API
responses and never parses raw V4 completion text.
"""

from __future__ import annotations

import copy
import json
from typing import Any

BOS = "<｜begin▁of▁sentence｜>"
EOS = "<｜end▁of▁sentence｜>"
USER = "<｜User｜>"
ASSISTANT = "<｜Assistant｜>"
THINK_START = "<think>"
THINK_END = "</think>"
DSML = "｜DSML｜"

TOOLS_TEMPLATE = """## Tools

You have access to a set of tools to help answer the user's question. You can invoke tools by writing a "<{dsml}tool_calls>" block like the following:

<{dsml}tool_calls>
<{dsml}invoke name="$TOOL_NAME">
<{dsml}parameter name="$PARAMETER_NAME" string="true|false">$PARAMETER_VALUE</{dsml}parameter>
...
</{dsml}invoke>
<{dsml}invoke name="$TOOL_NAME2">
...
</{dsml}invoke>
</{dsml}tool_calls>

String parameters should be specified as is and set `string="true"`. For all other types (numbers, booleans, arrays, objects), pass the value in JSON format and set `string="false"`.

If thinking_mode is enabled (triggered by {think_start}), you MUST output your complete reasoning inside {think_start}...{think_end} BEFORE any tool calls or final response.

Otherwise, output directly after {think_end} with tool calls or final response.

### Available Tool Schemas

{schemas}

You MUST strictly follow the above defined tool name and parameter schemas to invoke tool calls.
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_tools(tools: list[dict[str, Any]]) -> str:
    schemas = [_json(tool["function"]) for tool in tools]
    return TOOLS_TEMPLATE.format(
        dsml=DSML,
        think_start=THINK_START,
        think_end=THINK_END,
        schemas="\n".join(schemas),
    )


def _render_arguments(raw_arguments: str) -> str:
    try:
        arguments = json.loads(raw_arguments)
    except Exception:
        arguments = {"arguments": raw_arguments}
    rendered = []
    for key, value in arguments.items():
        rendered.append(
            f'<{DSML}parameter name="{key}" string="{str(isinstance(value, str)).lower()}">'
            f"{value if isinstance(value, str) else _json(value)}</{DSML}parameter>"
        )
    return "\n".join(rendered)


def _render_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    calls = []
    for call in tool_calls:
        function = call["function"]
        calls.append(
            f'<{DSML}invoke name="{function["name"]}">\n'
            f'{_render_arguments(function.get("arguments") or "{}")}\n'
            f"</{DSML}invoke>"
        )
    return f"\n\n<{DSML}tool_calls>\n" + "\n".join(calls) + f"\n</{DSML}tool_calls>"


def _merge_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for source in messages:
        message = copy.deepcopy(source)
        role = message.get("role")
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                "content": message.get("content", ""),
            }
            if merged and merged[-1].get("role") == "user" and "content_blocks" in merged[-1]:
                merged[-1]["content_blocks"].append(block)
            else:
                merged.append({"role": "user", "content_blocks": [block]})
        elif role == "user":
            block = {"type": "text", "text": message.get("content", "")}
            if merged and merged[-1].get("role") == "user" and "content_blocks" in merged[-1]:
                merged[-1]["content_blocks"].append(block)
            else:
                merged.append({"role": "user", "content": message.get("content", ""), "content_blocks": [block]})
        else:
            merged.append(message)
    return merged


def _sort_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    call_order: dict[str, int] = {}
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            call_order = {call.get("id", ""): index for index, call in enumerate(message["tool_calls"])}
        elif message.get("role") == "user" and message.get("content_blocks"):
            tool_blocks = [block for block in message["content_blocks"] if block.get("type") == "tool_result"]
            if len(tool_blocks) > 1 and call_order:
                ordered = iter(sorted(tool_blocks, key=lambda block: call_order.get(block.get("tool_use_id", ""), 0)))
                message["content_blocks"] = [next(ordered) if block.get("type") == "tool_result" else block for block in message["content_blocks"]]
    return messages


def encode_messages(messages: list[dict[str, Any]], *, thinking_mode: str) -> str:
    """Encode OpenAI-format messages exactly far enough for input token counting."""
    if thinking_mode not in {"chat", "thinking"}:
        raise ValueError("thinking_mode must be chat or thinking")
    prepared = _sort_tool_results(_merge_tool_messages(messages))
    last_user = max(
        (index for index, message in enumerate(prepared) if message.get("role") == "user"),
        default=-1,
    )
    has_tools = any(message.get("tools") for message in prepared)
    prompt = BOS
    for index, message in enumerate(prepared):
        role = message.get("role")
        content = message.get("content") or ""
        if role == "system":
            prompt += content
            if message.get("tools"):
                prompt += "\n\n" + _render_tools(message["tools"])
        elif role == "user":
            prompt += USER
            blocks = message.get("content_blocks")
            if blocks:
                rendered = []
                for block in blocks:
                    if block.get("type") == "text":
                        rendered.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        rendered.append(f'<tool_result>{block.get("content", "")}</tool_result>')
                prompt += "\n\n".join(rendered)
            else:
                prompt += content
        elif role == "assistant":
            reasoning = ""
            if thinking_mode == "thinking" and (has_tools or index > last_user):
                reasoning = (message.get("reasoning_content") or "") + THINK_END
            prompt += reasoning + content
            if message.get("tool_calls"):
                prompt += _render_tool_calls(message["tool_calls"])
            prompt += EOS
        else:
            raise ValueError(f"Unsupported DeepSeek V4 role: {role}")

        if index + 1 < len(prepared) and prepared[index + 1].get("role") not in {"assistant", "latest_reminder"}:
            continue
        if role == "user":
            prompt += ASSISTANT
            prompt += THINK_START if thinking_mode == "thinking" and index >= last_user else THINK_END
    return prompt
