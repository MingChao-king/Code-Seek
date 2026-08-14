"""Shared event sink for benchmark progress reporting."""

from minisweagent.agents.schema import RunEvent
from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager


class BatchProgressSink:
    def __init__(self, progress_manager: RunBatchProgressManager, instance_id: str):
        self.progress_manager = progress_manager
        self.instance_id = instance_id
        self.model_calls = 0
        self.cost = 0.0

    def emit(self, event: RunEvent) -> None:
        if event.type == "model.started":
            self.model_calls += 1
            self.progress_manager.update_instance_status(
                self.instance_id,
                f"Model {self.model_calls:3d} (${self.cost:.2f})",
            )
        elif event.type == "model.completed" and event.payload.get("cost") is not None:
            self.cost += event.payload["cost"]
        elif event.type == "tool.started":
            self.progress_manager.update_instance_status(
                self.instance_id,
                event.payload.get("call_title", event.payload.get("tool_name", "Running tool")),
            )
        elif event.type == "context.compaction.started":
            self.progress_manager.update_instance_status(self.instance_id, "Compacting context")
