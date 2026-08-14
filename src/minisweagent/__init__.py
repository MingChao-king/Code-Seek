"""
This file provides:

- Path settings for global config file & relative directories
- Version numbering
- Protocols for the core components of CodeSeek.
  By the magic of protocols & duck typing, you can pretty much ignore them,
  unless you want the static type checking.
"""

__version__ = "0.1"

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import dotenv
from platformdirs import user_config_dir
from rich.console import Console

from minisweagent.utils.log import logger

package_dir = Path(__file__).resolve().parent


# Keep the existing directory and environment variable names so sessions and
# credentials created before the CodeSeek rename remain available.
global_config_dir = Path(os.getenv("MSWEA_GLOBAL_CONFIG_DIR") or user_config_dir("mini-swe-agent"))
global_config_dir.mkdir(parents=True, exist_ok=True)
global_config_file = Path(global_config_dir) / ".env"

if not os.getenv("MSWEA_SILENT_STARTUP"):
    Console().print(f"This is [bold green]codeseek[/bold green] [bold green]v{__version__}[/bold green].")
dotenv.load_dotenv(dotenv_path=global_config_file)


# === Protocols ===
# You can ignore them unless you want static type checking.


class Model(Protocol):
    """Protocol for language models."""

    config: Any
    capabilities: Any

    def query(
        self,
        messages: list[Any],
        *,
        tools: list[Any],
        max_output_tokens: int | None,
        available_output_tokens: int | None,
        timeout_seconds: float | None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> Any: ...

    def estimate_input_tokens(self, messages: list[Any], tools: list[Any]) -> int: ...


class Environment(Protocol):
    """Protocol for execution environments."""

    config: Any

    def execute(self, action: dict, cwd: str = "") -> dict[str, Any]: ...

    def get_template_vars(self, **kwargs) -> dict[str, Any]: ...

    def serialize(self) -> dict: ...


class Agent(Protocol):
    """Protocol for agents."""

    config: Any

    def receive(self, text: str) -> str: ...


__all__ = [
    "Agent",
    "Model",
    "Environment",
    "package_dir",
    "__version__",
    "global_config_file",
    "global_config_dir",
    "logger",
]
