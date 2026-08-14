from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import IO

from pydantic import ValidationError

from minisweagent import global_config_dir
from minisweagent.agents.schema import SessionInfo, SessionState, ToolResult, utc_now

_SESSION_ID = re.compile(r"^ses_[0-9a-f]{32}$")


class SessionError(RuntimeError):
    pass


class SessionNotFound(SessionError):
    pass


class SessionInUse(SessionError):
    pass


class SessionFormatError(SessionError):
    pass


class SessionSaveError(SessionError):
    pass


class _SessionLease(AbstractContextManager[SessionState]):
    def __init__(self, store: SessionStore, session_id: str, *, create: SessionState | None = None):
        self.store = store
        self.session_id = session_id
        self.create_state = create
        self.session: SessionState | None = None

    def __enter__(self) -> SessionState:
        self.store._acquire(self.session_id)
        try:
            if self.create_state is not None:
                self.session = self.create_state
                self.store.save(self.session)
            else:
                self.session = self.store._load(self.session_id)
            return self.session
        except Exception:
            self.store._release(self.session_id)
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.store._release(self.session_id)


class SessionStore:
    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory is not None else global_config_dir / "sessions"
        if self.directory.exists() and not self.directory.is_dir():
            raise SessionError(f"Session directory is not a directory: {self.directory}")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self._locks: dict[str, IO[str]] = {}

    def create(self, workspace: str) -> AbstractContextManager[SessionState]:
        workspace_path = Path(workspace).expanduser().resolve()
        while True:
            session = SessionState(workspace=str(workspace_path))
            if not self._path(session.session_id).exists():
                return _SessionLease(self, session.session_id, create=session)

    def resume(self, session_id: str) -> AbstractContextManager[SessionState]:
        self._validate_id(session_id)
        if not self._path(session_id).is_file():
            raise SessionNotFound(f"Session not found: {session_id}")
        return _SessionLease(self, session_id)

    def list_recent(self, limit: int = 20) -> list[SessionInfo]:
        if limit <= 0:
            return []
        sessions = []
        for path in self.directory.glob("ses_*.json"):
            try:
                session = self._load(path.stem)
            except SessionError:
                continue
            last_user = next((m.content for m in reversed(session.messages) if m.role == "user"), None)
            if last_user is not None:
                last_user = " ".join(last_user.split())[:120]
            sessions.append(
                SessionInfo(
                    session_id=session.session_id,
                    workspace=session.workspace,
                    updated_at=session.updated_at,
                    last_user_message=last_user,
                )
            )
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)[:limit]

    def save(self, session: SessionState) -> None:
        if session.session_id not in self._locks:
            raise SessionSaveError(f"Session is not locked by this process: {session.session_id}")
        self._validate_messages(session)
        self._validate_memory(session)
        self._validate_events(session)
        session.updated_at = utc_now()
        target = self._path(session.session_id)
        try:
            fd, raw_path = tempfile.mkstemp(prefix=f".{session.session_id}.", suffix=".tmp", dir=self.directory)
            temp_path = Path(raw_path)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(session.model_dump(mode="json"), file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp_path, target)
                target.chmod(0o600)
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temp_path.unlink(missing_ok=True)
        except Exception as error:
            raise SessionSaveError(f"Could not save session {session.session_id}: {error}") from error

    def _path(self, session_id: str) -> Path:
        self._validate_id(session_id)
        return self.directory / f"{session_id}.json"

    def _lock_path(self, session_id: str) -> Path:
        self._validate_id(session_id)
        return self.directory / f"{session_id}.lock"

    @staticmethod
    def _validate_id(session_id: str) -> None:
        if not _SESSION_ID.fullmatch(session_id):
            raise SessionFormatError(f"Invalid session ID: {session_id!r}")

    def _acquire(self, session_id: str) -> None:
        self._validate_id(session_id)
        if session_id in self._locks:
            raise SessionInUse(f"Session is already open in this process: {session_id}")
        lock_path = self._lock_path(session_id)
        lock_file = lock_path.open("a+", encoding="utf-8")
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise SessionInUse(f"Session is in use by another process: {session_id}") from error
        self._locks[session_id] = lock_file

    def _release(self, session_id: str) -> None:
        lock_file = self._locks.pop(session_id, None)
        if lock_file is None:
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    def _load(self, session_id: str) -> SessionState:
        self._validate_id(session_id)
        try:
            payload = json.loads(self._path(session_id).read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                raise SessionFormatError(f"Unsupported session schema: {payload.get('schema_version')!r}")
            session = SessionState.model_validate(payload)
            self._validate_messages(session)
            self._validate_memory(session)
            self._validate_events(session)
            return session
        except SessionFormatError:
            raise
        except (OSError, json.JSONDecodeError, ValidationError, TypeError) as error:
            raise SessionFormatError(f"Invalid session {session_id}: {error}") from error

    @staticmethod
    def _validate_messages(session: SessionState) -> None:
        message_ids: set[str] = set()
        tool_call_ids: set[str] = set()
        closed_turn_ids: set[str] = set()
        current_turn: str | None = None
        pending: dict[str, str] = {}
        for index, message in enumerate(session.messages):
            if message.message_id in message_ids:
                raise SessionFormatError(f"Duplicate message ID: {message.message_id}")
            message_ids.add(message.message_id)
            if message.turn_id != current_turn:
                if current_turn is not None:
                    if pending:
                        raise SessionFormatError(f"Turn {current_turn} has unresolved tool calls before a later turn")
                    closed_turn_ids.add(current_turn)
                if message.turn_id in closed_turn_ids:
                    raise SessionFormatError(f"Turn {message.turn_id} is not contiguous")
                if message.role != "user":
                    raise SessionFormatError(f"Turn {message.turn_id} does not start with a user message")
                current_turn = message.turn_id
            elif index > 0 and message.role == "user":
                raise SessionFormatError(f"Turn {message.turn_id} contains more than one user message")

            if message.role == "assistant":
                if pending:
                    raise SessionFormatError("Assistant message appeared before all prior tool calls were resolved")
                for call in message.tool_calls:
                    if not call.id.strip() or call.id in tool_call_ids:
                        raise SessionFormatError(f"Duplicate or empty tool call ID: {call.id!r}")
                    tool_call_ids.add(call.id)
                    pending[call.id] = call.name
                if message.tool_call_id is not None:
                    raise SessionFormatError("Assistant message cannot contain tool_call_id")
            elif message.role == "tool":
                if message.tool_call_id not in pending:
                    raise SessionFormatError(f"Orphan or duplicate tool result: {message.tool_call_id!r}")
                try:
                    result = ToolResult.model_validate_json(message.content)
                except ValidationError as error:
                    raise SessionFormatError(f"Invalid ToolResult for {message.tool_call_id}: {error}") from error
                if result.tool_call_id != message.tool_call_id or result.name != pending[message.tool_call_id]:
                    raise SessionFormatError(f"ToolResult does not match call {message.tool_call_id}")
                del pending[message.tool_call_id]
                if message.tool_calls:
                    raise SessionFormatError("Tool message cannot contain tool_calls")
            elif message.tool_calls or message.tool_call_id is not None:
                raise SessionFormatError("User message cannot contain tool protocol fields")

    @staticmethod
    def _validate_memory(session: SessionState) -> None:
        memory = session.memory
        if not 0 <= memory.raw_compaction_cursor <= len(session.messages):
            raise SessionFormatError("Memory cursor is outside the message list")
        if memory.raw_compaction_cursor < len(session.messages) and session.messages[memory.raw_compaction_cursor].role != "user":
            raise SessionFormatError("Memory cursor must stop at a turn boundary")
        for key, batch in memory.batches.items():
            if key != batch.batch_id:
                raise SessionFormatError(f"Memory key does not match batch ID: {key}")
            if not 0 <= batch.start_message_index < batch.end_message_index <= len(session.messages):
                raise SessionFormatError(f"Invalid memory range for {batch.batch_id}")
            if batch.revises_batch_id is not None:
                revised = memory.batches.get(batch.revises_batch_id)
                if revised is None or (
                    revised.start_message_index,
                    revised.end_message_index,
                ) != (batch.start_message_index, batch.end_message_index):
                    raise SessionFormatError(f"Invalid revision link for {batch.batch_id}")
            children = [memory.batches.get(child_id) for child_id in batch.source_batch_ids]
            if any(child is None for child in children):
                raise SessionFormatError(f"Missing child batch for {batch.batch_id}")
            if children:
                expected = batch.start_message_index
                for child in children:
                    assert child is not None
                    if child.start_message_index != expected or child.level >= batch.level:
                        raise SessionFormatError(f"Invalid child ordering or level for {batch.batch_id}")
                    expected = child.end_message_index
                if expected != batch.end_message_index:
                    raise SessionFormatError(f"Children do not cover batch {batch.batch_id}")
            elif batch.level != 0:
                raise SessionFormatError(f"Non-leaf memory batch has no children: {batch.batch_id}")
        active = [memory.batches.get(batch_id) for batch_id in memory.active_batch_ids]
        if any(batch is None for batch in active):
            raise SessionFormatError("Active memory contains an unknown batch")
        expected = 0
        for batch in active:
            assert batch is not None
            if batch.start_message_index != expected:
                raise SessionFormatError("Active memory ranges are not continuous")
            expected = batch.end_message_index
        if expected != memory.raw_compaction_cursor:
            raise SessionFormatError("Active memory does not cover the compacted prefix")

    @staticmethod
    def _validate_events(session: SessionState) -> None:
        previous = 0
        for event in session.events:
            if event.session_id != session.session_id:
                raise SessionFormatError("Event belongs to a different session")
            if not event.durable:
                raise SessionFormatError("Non-durable events must not be stored in the session")
            if event.sequence <= previous:
                raise SessionFormatError("Stored event sequences must be strictly increasing")
            previous = event.sequence
        if session.next_event_sequence <= previous:
            raise SessionFormatError("next_event_sequence must be greater than every stored event")
