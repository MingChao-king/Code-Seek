from __future__ import annotations

import html
import json
import math
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from minisweagent.agents.schema import (
    ContextView,
    ConversationMemory,
    MemoryBatch,
    Message,
    ModelCapabilities,
    ModelMessage,
    ModelResponse,
    ToolSpec,
    new_id,
)
from minisweagent.exceptions import ContextWindowExceeded


class SummaryFailed(RuntimeError):
    pass


class ContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_instructions: str
    compact_at_ratio: float = 0.80
    compact_to_ratio: float = 0.20
    keep_recent_turns: int = 2
    summary_token_budget: int = 2048
    safety_margin_ratio: float = 0.05

    def model_post_init(self, __context: Any) -> None:
        if not self.summary_instructions.strip():
            raise ValueError("summary_instructions must not be empty")
        if not 0 < self.compact_to_ratio < self.compact_at_ratio < 1:
            raise ValueError("Expected 0 < compact_to_ratio < compact_at_ratio < 1")
        if not 0 < self.safety_margin_ratio < 1:
            raise ValueError("safety_margin_ratio must be between 0 and 1")
        if self.keep_recent_turns <= 0 or self.summary_token_budget <= 0:
            raise ValueError("keep_recent_turns and summary_token_budget must be positive")


class ContextManager:
    """Build one provider-ready view while retaining a lossless local session.

    The model owns semantic summarization. This class owns all ranges, IDs,
    progress checks, retry sizing, the active summary frontier and exact recounts.
    """

    def __init__(
        self,
        estimate_input_tokens: Callable[[list[ModelMessage], list[ToolSpec]], int],
        *,
        split_text: Callable[[str, int], list[str]] | None = None,
        **kwargs: Any,
    ):
        self.config = ContextConfig(**kwargs)
        self.estimate_input_tokens = estimate_input_tokens
        self.split_text = split_text
        self._count_seconds = 0.0

    def validate_minimum_request(
        self,
        *,
        system: ModelMessage,
        tools: list[ToolSpec],
        capabilities: ModelCapabilities,
    ) -> None:
        """Reject only a model/tool setup that cannot carry the Agent protocol."""
        if capabilities.context_window is None:
            return
        ceiling = self._input_ceiling(capabilities.context_window)
        decision = self._raw_count([system], tools)
        summary = self._raw_count([ModelMessage(role="system", content=self.config.summary_instructions)], [])
        minimum_summary_output = min(self.config.summary_token_budget, capabilities.max_output_tokens or 16, 16)
        if decision > ceiling:
            raise ValueError("Stable instructions and tool schemas exceed the model input ceiling")
        if summary + minimum_summary_output > ceiling:
            raise ValueError("Summary instructions are incompatible with the model context window")

    def build(
        self,
        *,
        messages: list[ModelMessage],
        source_messages: list[Message],
        tools: list[ToolSpec],
        memory: ConversationMemory,
        model_capabilities: ModelCapabilities,
        user_output_limit: int | None,
        summarize: Callable[[list[ModelMessage], int, str, str, str], ModelResponse],
        accept_memory: Callable[[ConversationMemory, dict[str, Any]], None],
        report_compaction: Callable[[str, dict[str, Any]], None],
        rejected_view_ceiling: int | None = None,
        force_compact: bool = False,
        compaction_focus: str = "",
    ) -> ContextView:
        if not messages or messages[0].role != "system":
            raise ValueError("Context draft must start with one system message")
        self._count_seconds = 0.0
        system = messages[0]
        current_memory = memory.model_copy(deep=True)
        final_messages = self.compose(system, source_messages, current_memory)
        before = self._count(final_messages, tools)
        window = model_capabilities.context_window
        input_ceiling = None if window is None else self._input_ceiling(window)
        threshold = None if input_ceiling is None else math.floor(input_ceiling * self.config.compact_at_ratio)
        normal_target = None if input_ceiling is None else math.floor(input_ceiling * self.config.compact_to_ratio)
        rejection_ceiling = None if rejected_view_ceiling is None else max(rejected_view_ceiling - 1, 1)
        effective_ceiling = self._minimum_known(input_ceiling, rejection_ceiling)
        target = normal_target
        if rejection_ceiling is not None:
            target = min(target, rejection_ceiling) if target is not None else rejection_ceiling
        if force_compact:
            forced_target = max(before - 1, 1)
            target = min(target, forced_target) if target is not None else max(before // 2, 1)
        must_compact = force_compact or (threshold is not None and before >= threshold) or (
            rejected_view_ceiling is not None and before >= rejected_view_ceiling
        )
        compacted = False
        target_unreachable = False

        if must_compact:
            summary_instructions = self._summary_instructions(compaction_focus)
            compacted = True
            compaction_id = f"cmp_{time.time_ns()}"
            report_compaction(
                "context.compaction.started",
                {
                    "compaction_id": compaction_id,
                    "before_tokens": before,
                    "manual": force_compact,
                    "focus_provided": bool(compaction_focus.strip()),
                },
            )
            try:
                compaction_capabilities = model_capabilities
                if model_capabilities.context_window is None and effective_ceiling is not None:
                    inferred_window = math.ceil(effective_ceiling / (1 - self.config.safety_margin_ratio))
                    compaction_capabilities = model_capabilities.model_copy(
                        update={"context_window": inferred_window}
                    )
                current_memory, target_unreachable = self._compact(
                    system=system,
                    source_messages=source_messages,
                    tools=tools,
                    memory=current_memory,
                    capabilities=compaction_capabilities,
                    target=target or max(before // 2, 1),
                    hard_ceiling=effective_ceiling,
                    summarize=summarize,
                    accept_memory=accept_memory,
                    report=report_compaction,
                    compaction_id=compaction_id,
                    summary_instructions=summary_instructions,
                )
                final_messages = self.compose(system, source_messages, current_memory)
                after = self._count(final_messages, tools)
                if rejected_view_ceiling is not None and after >= rejected_view_ceiling:
                    raise SummaryFailed("Compaction did not produce a view smaller than the rejected request")
                if input_ceiling is not None and after > input_ceiling:
                    raise SummaryFailed("The stable protocol itself is incompatible with the provider context limit")
                report_compaction(
                    "context.compaction.completed",
                    {
                        "compaction_id": compaction_id,
                        "before_tokens": before,
                        "after_tokens": after,
                        "target_tokens": target,
                        "target_unreachable_by_retention": target_unreachable,
                        "manual": force_compact,
                    },
                )
            except Exception as error:
                report_compaction(
                    "context.compaction.failed",
                    {"compaction_id": compaction_id, "before_tokens": before, "error": str(error)},
                )
                raise

        estimated = self._count(final_messages, tools)
        remaining = None if window is None else max(window - estimated, 0)
        return ContextView(
            messages=final_messages,
            estimated_input_tokens=estimated,
            input_ceiling=input_ceiling,
            available_output_tokens=remaining,
            user_output_limit=user_output_limit,
            budget_source=model_capabilities.context_window_source,
            compacted=compacted,
            token_count_seconds=self._count_seconds,
            target_unreachable_by_retention=target_unreachable,
        )

    def _summary_instructions(self, focus: str) -> str:
        focus = focus.strip()
        if not focus:
            return self.config.summary_instructions
        return (
            f"{self.config.summary_instructions}\n\n"
            "For this manual compaction, give special attention to the user's requested preservation focus below. "
            "Treat it as summarization guidance, not as a task to execute or evidence that any action succeeded.\n"
            f"USER_PRESERVATION_FOCUS={json.dumps(focus, ensure_ascii=False)}"
        )

    def compose(
        self,
        system: ModelMessage,
        source_messages: list[Message],
        memory: ConversationMemory,
    ) -> list[ModelMessage]:
        result = [system]
        for batch_id in memory.active_batch_ids:
            result.append(self._memory_message(memory.batches[batch_id], source_messages))
        result.extend(message.to_model_message() for message in source_messages[memory.raw_compaction_cursor :])
        return result

    def _compact(
        self,
        *,
        system: ModelMessage,
        source_messages: list[Message],
        tools: list[ToolSpec],
        memory: ConversationMemory,
        capabilities: ModelCapabilities,
        target: int,
        hard_ceiling: int | None,
        summarize: Callable[[list[ModelMessage], int, str, str, str], ModelResponse],
        accept_memory: Callable[[ConversationMemory, dict[str, Any]], None],
        report: Callable[[str, dict[str, Any]], None],
        compaction_id: str,
        summary_instructions: str,
    ) -> tuple[ConversationMemory, bool]:
        keep_start = self._recent_turn_start(source_messages, self.config.keep_recent_turns)

        # Normal path: compact every complete old turn while preserving the two
        # most recent turns verbatim.
        while memory.raw_compaction_cursor < keep_start:
            end = self._select_leaf_end(
                source_messages,
                memory.raw_compaction_cursor,
                keep_start,
                capabilities,
                summary_instructions,
            )
            memory = self._accept_reducing_leaf(
                system,
                source_messages,
                tools,
                memory,
                end,
                capabilities,
                summarize,
                accept_memory,
                report,
                compaction_id,
                summary_instructions,
            )

        target_unreachable = False
        while self._count(self.compose(system, source_messages, memory), tools) > target:
            before = self._count(self.compose(system, source_messages, memory), tools)
            reduced = False
            if memory.active_batch_ids:
                candidate, batch = self._retry_reducing_merge(
                    system,
                    source_messages,
                    tools,
                    memory,
                    capabilities,
                    summarize,
                    report,
                    compaction_id,
                    summary_instructions,
                )
                after = self._count(self.compose(system, source_messages, candidate), tools)
                if after < before:
                    self._accept(candidate, batch, "merge", before, after, accept_memory, compaction_id)
                    memory = candidate
                    reduced = True
            if reduced:
                continue

            # The 20% target is intentionally softer than the provider ceiling:
            # do not destroy recent detail merely to hit a percentage.
            if hard_ceiling is None or before <= hard_ceiling:
                target_unreachable = True
                break

            # Capacity fallback: if retained turns themselves do not fit, compact
            # the oldest remaining turn. Oversized records are chunked by the
            # summary routine; original Session messages remain untouched.
            if memory.raw_compaction_cursor < len(source_messages):
                emergency_end = self._next_turn_boundary(source_messages, memory.raw_compaction_cursor)
                memory = self._accept_reducing_leaf(
                    system,
                    source_messages,
                    tools,
                    memory,
                    emergency_end,
                    capabilities,
                    summarize,
                    accept_memory,
                    report,
                    compaction_id,
                    summary_instructions,
                )
                continue
            raise SummaryFailed("Stable instructions, tools and the minimum memory record exceed the provider ceiling")
        return memory, target_unreachable

    def _accept_reducing_leaf(
        self,
        system: ModelMessage,
        source: list[Message],
        tools: list[ToolSpec],
        memory: ConversationMemory,
        end: int,
        capabilities: ModelCapabilities,
        summarize: Callable[[list[ModelMessage], int, str, str, str], ModelResponse],
        accept_memory: Callable[[ConversationMemory, dict[str, Any]], None],
        report: Callable[[str, dict[str, Any]], None],
        compaction_id: str,
        summary_instructions: str,
    ) -> ConversationMemory:
        before = self._count(self.compose(system, source, memory), tools)
        batch_id = new_id("mem")
        report(
            "context.compaction.node_started",
            {
                "compaction_id": compaction_id,
                "operation": "leaf",
                "batch_id": batch_id,
                "start_message_index": memory.raw_compaction_cursor,
                "end_message_index": end,
                "before_tokens": before,
            },
        )
        budget = self.config.summary_token_budget
        while budget >= 1:
            candidate, batch = self._create_leaf(
                source,
                memory,
                end,
                capabilities,
                summarize,
                compaction_id,
                batch_id,
                budget,
                summary_instructions,
            )
            after = self._count(self.compose(system, source, candidate), tools)
            if after < before:
                self._accept(candidate, batch, "leaf", before, after, accept_memory, compaction_id)
                return candidate
            if budget == 1:
                break
            budget = max(budget // 2, 1)
        raise SummaryFailed("A leaf summary could not make the active context smaller")

    def _retry_reducing_merge(
        self,
        system: ModelMessage,
        source: list[Message],
        tools: list[ToolSpec],
        memory: ConversationMemory,
        capabilities: ModelCapabilities,
        summarize: Callable[[list[ModelMessage], int, str, str, str], ModelResponse],
        report: Callable[[str, dict[str, Any]], None],
        compaction_id: str,
        summary_instructions: str,
    ) -> tuple[ConversationMemory, MemoryBatch]:
        before = self._count(self.compose(system, source, memory), tools)
        active, left_index, right_index = self._select_merge_children(memory)
        children = [active[left_index]] if left_index == right_index else [active[left_index], active[right_index]]
        batch_id = new_id("mem")
        report(
            "context.compaction.node_started",
            {
                "compaction_id": compaction_id,
                "operation": "merge",
                "batch_id": batch_id,
                "start_message_index": children[0].start_message_index,
                "end_message_index": children[-1].end_message_index,
                "source_batch_ids": [child.batch_id for child in children],
                "before_tokens": before,
            },
        )
        budget = self.config.summary_token_budget
        last: tuple[ConversationMemory, MemoryBatch] | None = None
        while budget >= 1:
            try:
                last = self._merge_frontier(
                    source,
                    memory,
                    capabilities,
                    summarize,
                    compaction_id,
                    batch_id,
                    left_index,
                    right_index,
                    budget,
                    summary_instructions,
                )
            except SummaryFailed:
                if last is not None:
                    return last
                raise
            candidate, _ = last
            if self._count(self.compose(system, source, candidate), tools) < before:
                return last
            if budget == 1:
                break
            budget = max(budget // 2, 1)
        assert last is not None
        return last

    def _create_leaf(
        self,
        source: list[Message],
        memory: ConversationMemory,
        end: int,
        capabilities: ModelCapabilities,
        summarize: Callable[[list[ModelMessage], int, str, str, str], ModelResponse],
        compaction_id: str,
        batch_id: str,
        output_budget: int,
        summary_instructions: str,
    ) -> tuple[ConversationMemory, MemoryBatch]:
        start = memory.raw_compaction_cursor
        summary_messages = [ModelMessage(role="system", content=summary_instructions)]
        summary_messages.extend(message.to_model_message() for message in source[start:end])
        content = self._generate_summary(
            summary_messages,
            capabilities,
            summarize,
            compaction_id,
            "leaf",
            batch_id,
            output_budget,
            summary_instructions,
        )
        batch = MemoryBatch(
            batch_id=batch_id,
            level=0,
            start_message_index=start,
            end_message_index=end,
            content=content,
        )
        candidate = memory.model_copy(deep=True)
        candidate.batches[batch.batch_id] = batch
        candidate.active_batch_ids.append(batch.batch_id)
        candidate.raw_compaction_cursor = end
        return candidate, batch

    def _merge_frontier(
        self,
        source: list[Message],
        memory: ConversationMemory,
        capabilities: ModelCapabilities,
        summarize: Callable[[list[ModelMessage], int, str, str, str], ModelResponse],
        compaction_id: str,
        batch_id: str,
        left_index: int,
        right_index: int,
        output_budget: int,
        summary_instructions: str,
    ) -> tuple[ConversationMemory, MemoryBatch]:
        active = [memory.batches[item] for item in memory.active_batch_ids]
        children = [active[left_index]] if left_index == right_index else [active[left_index], active[right_index]]
        summary_messages = [ModelMessage(role="system", content=summary_instructions)]
        summary_messages.extend(self._memory_message(batch, source) for batch in children)
        content = self._generate_summary(
            summary_messages,
            capabilities,
            summarize,
            compaction_id,
            "merge",
            batch_id,
            output_budget,
            summary_instructions,
        )
        batch = MemoryBatch(
            batch_id=batch_id,
            level=max(child.level for child in children) + 1,
            start_message_index=children[0].start_message_index,
            end_message_index=children[-1].end_message_index,
            content=content,
            source_batch_ids=[child.batch_id for child in children],
        )
        candidate = memory.model_copy(deep=True)
        candidate.batches[batch.batch_id] = batch
        candidate.active_batch_ids[left_index : right_index + 1] = [batch.batch_id]
        return candidate, batch

    def _generate_summary(
        self,
        messages: list[ModelMessage],
        capabilities: ModelCapabilities,
        summarize: Callable[[list[ModelMessage], int, str, str, str], ModelResponse],
        compaction_id: str,
        operation: str,
        batch_id: str,
        output_budget: int,
        summary_instructions: str,
        *,
        depth: int = 0,
    ) -> str:
        if depth > 64:
            raise SummaryFailed("Summary chunk reduction did not converge")
        source_tokens = self._count(messages, [])
        limit = min(
            output_budget,
            capabilities.max_output_tokens or output_budget,
            max(source_tokens // 2, 1),
        )
        ceiling = None if capabilities.context_window is None else self._input_ceiling(capabilities.context_window)
        if ceiling is not None and source_tokens + limit > ceiling:
            return self._generate_chunked_summary(
                messages,
                capabilities,
                summarize,
                compaction_id,
                operation,
                batch_id,
                limit,
                depth,
                summary_instructions,
            )

        feedback = ""
        while limit >= 1:
            request = list(messages)
            if feedback:
                request[0] = ModelMessage(
                    role="system",
                    content=f"{summary_instructions}\n\nProtocol correction: {feedback}",
                )
            try:
                response = ModelResponse.model_validate(
                    summarize(request, limit, compaction_id, operation, batch_id)
                )
            except ContextWindowExceeded:
                prompt_tokens = self._count([request[0]], [])
                desired_ceiling = max(prompt_tokens + limit + 1, (self._count(request, []) + limit) // 2)
                smaller = capabilities.model_copy(
                    update={
                        "context_window": math.ceil(
                            desired_ceiling / (1 - self.config.safety_margin_ratio)
                        )
                    }
                )
                return self._generate_chunked_summary(
                    messages,
                    smaller,
                    summarize,
                    compaction_id,
                    operation,
                    batch_id,
                    limit,
                    depth,
                    summary_instructions,
                )
            content = response.content.strip()
            output_tokens = response.usage.output_tokens
            if output_tokens is None and content:
                output_tokens = self._count([ModelMessage(role="assistant", content=content)], [])
            if content and not response.tool_calls and output_tokens is not None and output_tokens <= limit:
                return content
            feedback = "Return a non-empty plain-text summary without tool calls and within the requested output limit."
            if limit == 1:
                break
            limit = max(limit // 2, 1)
        raise SummaryFailed("Summary model repeatedly returned an invalid or oversized response")

    def _generate_chunked_summary(
        self,
        messages: list[ModelMessage],
        capabilities: ModelCapabilities,
        summarize: Callable[[list[ModelMessage], int, str, str, str], ModelResponse],
        compaction_id: str,
        operation: str,
        batch_id: str,
        output_budget: int,
        depth: int,
        summary_instructions: str,
    ) -> str:
        payload = "\n".join(
            f"SOURCE_RECORD {index + 1}/{len(messages) - 1}:\n"
            f"{json.dumps(message.model_dump(mode='json'), ensure_ascii=False, separators=(',', ':'))}"
            for index, message in enumerate(messages[1:])
        )
        chunks = self._split_payload(payload, capabilities, output_budget, summary_instructions)
        partials: list[str] = []
        for index, chunk in enumerate(chunks):
            chunk_prompt = (
                f"{summary_instructions}\n\n"
                f"This is consecutive source chunk {index + 1}/{len(chunks)}. Preserve its source order and do not "
                "assume that omitted chunks are empty."
            )
            partials.append(
                self._generate_summary(
                    [ModelMessage(role="system", content=chunk_prompt), ModelMessage(role="user", content=chunk)],
                    capabilities,
                    summarize,
                    compaction_id,
                    f"{operation}_chunk",
                    batch_id,
                    output_budget,
                    summary_instructions,
                    depth=depth + 1,
                )
            )
        if len(partials) == 1:
            return partials[0]
        return self._reduce_partial_summaries(
            partials,
            capabilities,
            summarize,
            compaction_id,
            operation,
            batch_id,
            output_budget,
            depth,
            summary_instructions,
        )

    def _reduce_partial_summaries(
        self,
        partials: list[str],
        capabilities: ModelCapabilities,
        summarize: Callable[[list[ModelMessage], int, str, str, str], ModelResponse],
        compaction_id: str,
        operation: str,
        batch_id: str,
        output_budget: int,
        depth: int,
        summary_instructions: str,
    ) -> str:
        """Merge consecutive chunk summaries without re-serializing them as source records."""
        if depth > 64:
            raise SummaryFailed("Summary chunk reduction did not converge")
        if len(partials) == 1:
            return partials[0]
        assert capabilities.context_window is not None
        ceiling = self._input_ceiling(capabilities.context_window)
        limit = min(output_budget, capabilities.max_output_tokens or output_budget)
        available = ceiling - limit
        system = ModelMessage(
            role="system",
            content=(
                f"{summary_instructions}\n\n"
                "Merge the following consecutive partial summaries into one faithful summary. Preserve source order."
            ),
        )

        groups: list[list[str]] = []
        current: list[str] = []
        for partial in partials:
            candidate = current + [partial]
            request = [system, ModelMessage(role="user", content=self._format_partials(candidate))]
            if current and self._count(request, []) > available:
                groups.append(current)
                current = [partial]
            else:
                current = candidate
        if current:
            groups.append(current)

        # A generated partial fitted the same model once, so at least two normally fit here.
        # If wrapper overhead prevents that, lower the output budget while recompressing
        # adjacent pairs; accepting an unchanged number of groups would loop forever.
        if len(groups) >= len(partials):
            groups = [partials[index : index + 2] for index in range(0, len(partials), 2)]
            limit = max(limit // 2, 1)

        reduced: list[str] = []
        for group in groups:
            request = [system, ModelMessage(role="user", content=self._format_partials(group))]
            reduced.append(
                self._generate_summary(
                    request,
                    capabilities,
                    summarize,
                    compaction_id,
                    f"{operation}_chunk_merge",
                    batch_id,
                    limit,
                    summary_instructions,
                    depth=depth + 1,
                )
            )
        if len(reduced) >= len(partials):
            raise SummaryFailed("Summary chunk reduction did not reduce the number of partial summaries")
        return self._reduce_partial_summaries(
            reduced,
            capabilities,
            summarize,
            compaction_id,
            operation,
            batch_id,
            output_budget,
            depth + 1,
            summary_instructions,
        )

    @staticmethod
    def _format_partials(partials: list[str]) -> str:
        return "\n\n".join(
            f'<partial_summary index="{index + 1}" total="{len(partials)}">\n'
            f"{html.escape(value)}\n</partial_summary>"
            for index, value in enumerate(partials)
        )

    def _split_payload(
        self,
        payload: str,
        capabilities: ModelCapabilities,
        output_budget: int,
        summary_instructions: str,
    ) -> list[str]:
        if not payload:
            return [""]
        assert capabilities.context_window is not None
        ceiling = self._input_ceiling(capabilities.context_window)
        prompt = (
            f"{summary_instructions}\n\n"
            "This is consecutive source chunk 999999/999999. Preserve its source order and do not assume that "
            "omitted chunks are empty."
        )
        base = [ModelMessage(role="system", content=prompt)]
        available = ceiling - min(output_budget, capabilities.max_output_tokens or output_budget)
        if available <= self._count(base, []):
            raise SummaryFailed("Summary prompt leaves no room for a source chunk")
        if self.split_text is not None:
            candidate = [part for part in self.split_text(payload, available) if part]
            if candidate and all(
                self._count(base + [ModelMessage(role="user", content=part)], []) <= available for part in candidate
            ):
                return candidate

        chunks: list[str] = []
        start = 0
        while start < len(payload):
            low, high = 1, len(payload) - start
            best = 0
            while low <= high:
                middle = (low + high) // 2
                request = base + [ModelMessage(role="user", content=payload[start : start + middle])]
                if self._count(request, []) <= available:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if best <= 0:
                raise SummaryFailed("Tokenizer could not fit even one source character into a summary request")
            chunks.append(payload[start : start + best])
            start += best
        return chunks

    def _select_leaf_end(
        self,
        source: list[Message],
        start: int,
        keep_start: int,
        capabilities: ModelCapabilities,
        summary_instructions: str,
    ) -> int:
        boundaries = self._turn_boundaries(source, start, keep_start)
        if not boundaries:
            raise SummaryFailed("No complete old turn is available for compaction")
        if capabilities.context_window is None:
            return boundaries[-1]
        ceiling = self._input_ceiling(capabilities.context_window)
        selected = boundaries[0]
        for boundary in boundaries:
            request = [ModelMessage(role="system", content=summary_instructions)]
            request.extend(message.to_model_message() for message in source[start:boundary])
            source_tokens = self._count(request, [])
            limit = min(
                self.config.summary_token_budget,
                capabilities.max_output_tokens or self.config.summary_token_budget,
                max(source_tokens // 2, 1),
            )
            if source_tokens + limit <= ceiling:
                selected = boundary
            else:
                break
        return selected

    @staticmethod
    def _recent_turn_start(source: list[Message], count: int) -> int:
        if not source:
            return 0
        turns: list[str] = []
        for index in range(len(source) - 1, -1, -1):
            if source[index].turn_id not in turns:
                turns.append(source[index].turn_id)
                if len(turns) > count:
                    return index + 1
        return 0

    @staticmethod
    def _next_turn_boundary(source: list[Message], start: int) -> int:
        turn_id = source[start].turn_id
        index = start + 1
        while index < len(source) and source[index].turn_id == turn_id:
            index += 1
        return index

    @staticmethod
    def _turn_boundaries(source: list[Message], start: int, end: int) -> list[int]:
        return [
            index
            for index in range(start + 1, end + 1)
            if index == end or source[index].turn_id != source[index - 1].turn_id
        ]

    @staticmethod
    def _select_merge_children(memory: ConversationMemory) -> tuple[list[MemoryBatch], int, int]:
        active = [memory.batches[batch_id] for batch_id in memory.active_batch_ids]
        if len(active) == 1:
            return active, 0, 0
        for index in range(len(active) - 1):
            if active[index].level == active[index + 1].level:
                return active, index, index + 1
        return active, 0, 1

    @staticmethod
    def _memory_message(batch: MemoryBatch, source: list[Message]) -> ModelMessage:
        return ModelMessage(
            role="assistant",
            content=(
                f'<memory_batch id="{batch.batch_id}" level="{batch.level}" origin="{batch.origin}" '
                f'covers="{source[batch.start_message_index].message_id}..'
                f'{source[batch.end_message_index - 1].message_id}">\n'
                f"{html.escape(batch.content)}\n</memory_batch>"
            ),
        )

    @staticmethod
    def _accept(
        candidate: ConversationMemory,
        batch: MemoryBatch,
        operation: str,
        before: int,
        after: int,
        accept_memory: Callable[[ConversationMemory, dict[str, Any]], None],
        compaction_id: str,
    ) -> None:
        accept_memory(
            candidate,
            {
                "compaction_id": compaction_id,
                "operation": operation,
                "batch_id": batch.batch_id,
                "level": batch.level,
                "start_message_index": batch.start_message_index,
                "end_message_index": batch.end_message_index,
                "source_batch_ids": batch.source_batch_ids,
                "before_tokens": before,
                "after_tokens": after,
            },
        )

    def _count(self, messages: list[ModelMessage], tools: list[ToolSpec]) -> int:
        started = time.monotonic()
        try:
            return self._raw_count(messages, tools)
        finally:
            self._count_seconds += time.monotonic() - started

    def _raw_count(self, messages: list[ModelMessage], tools: list[ToolSpec]) -> int:
        return max(int(self.estimate_input_tokens(messages, tools)), 1)

    def _input_ceiling(self, window: int) -> int:
        return window - math.ceil(window * self.config.safety_margin_ratio)

    @staticmethod
    def _minimum_known(left: int | None, right: int | None) -> int | None:
        values = [value for value in (left, right) if value is not None]
        return min(values) if values else None
