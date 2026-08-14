import codecs
import os
import platform
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from minisweagent.utils.serialize import recursive_merge


class LocalEnvironmentConfig(BaseModel):
    cwd: str = ""
    env: dict[str, str] = {}
    timeout: int = 30


class LocalEnvironment:
    def __init__(self, *, config_class: type = LocalEnvironmentConfig, **kwargs):
        """This class executes bash commands directly on the local machine."""
        self.config = config_class(**kwargs)

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Execute a command in the local environment and return the result as a dict."""
        command = action.get("command", "")
        cwd = cwd or self.config.cwd or os.getcwd()
        try:
            result = _run(command, cwd, os.environ | self.config.env, timeout or self.config.timeout)
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except Exception as e:
            raw_output = getattr(e, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            output = {
                "output": raw_output,
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e)},
            }
        return output

    def execute_stream(
        self,
        action: dict,
        cwd: str = "",
        *,
        timeout: int | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        """Execute locally and publish stdout/stderr chunks while retaining the final result."""
        command = action.get("command", "")
        cwd = cwd or self.config.cwd or os.getcwd()
        try:
            result = _run_streaming(
                command,
                cwd,
                os.environ | self.config.env,
                timeout or self.config.timeout,
                on_output,
            )
            return {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except Exception as error:
            raw_output = getattr(error, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            return {
                "output": raw_output,
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {error}",
                "extra": {"exception_type": type(error).__name__, "exception": str(error)},
            }

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(self.config.model_dump(), platform.uname()._asdict(), os.environ, kwargs)

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "environment": self.config.model_dump(mode="json"),
                    "environment_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }


def _run(command: str, cwd: str, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Like subprocess.run, but kills the whole process group on timeout so no children are orphaned."""
    process = subprocess.Popen(
        command,
        shell=True,
        text=True,
        cwd=cwd,
        env=env,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL) if os.name == "posix" else process.kill()
        stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout)
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout)


def _run_streaming(
    command: str,
    cwd: str,
    env: dict[str, str],
    timeout: int,
    on_output: Callable[[str, str], None] | None,
) -> subprocess.CompletedProcess[str]:
    """Stream both pipes on POSIX without letting a silent process bypass timeout."""
    if os.name != "posix":
        result = _run(command, cwd, env, timeout)
        if on_output is not None and result.stdout:
            on_output("stdout", result.stdout)
        return result
    process = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    decoders = {
        "stdout": codecs.getincrementaldecoder("utf-8")(errors="replace"),
        "stderr": codecs.getincrementaldecoder("utf-8")(errors="replace"),
    }
    output: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout, output="".join(output))
            for key, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                stream = key.data
                if not chunk:
                    tail = decoders[stream].decode(b"", final=True)
                    if tail:
                        output.append(tail)
                        if on_output is not None:
                            on_output(stream, tail)
                    selector.unregister(key.fileobj)
                    continue
                text = decoders[stream].decode(chunk)
                if text:
                    output.append(text)
                    if on_output is not None:
                        on_output(stream, text)
        returncode = process.wait(timeout=max(deadline - time.monotonic(), 0.001))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise subprocess.TimeoutExpired(command, timeout, output="".join(output))
    finally:
        selector.close()
    return subprocess.CompletedProcess(command, returncode, stdout="".join(output))
