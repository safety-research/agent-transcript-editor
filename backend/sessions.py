"""
Server-side session management for agent conversations.

A session is 1:1 with a file_key (e.g. "default/attack-v2.jsonl").
Multiple clients (UI, CLI) can subscribe to the same session via SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRANSCRIPTS_DIR = Path(os.getenv("TRANSCRIPTS_DIR", "./transcripts"))

# Load shared settings (prompt_variants, etc.) from settings.json
_SETTINGS_PATH = Path(__file__).parent / "settings.json"
_SHARED_SETTINGS: dict[str, Any] = {}
if _SETTINGS_PATH.exists():
    with open(_SETTINGS_PATH) as f:
        _SHARED_SETTINGS = json.load(f)

DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "claude-opus-4-6")
DEFAULT_PROMPT_VARIANTS: list[str] = _SHARED_SETTINGS.get("prompt_variants", ["control_arena"])
DEFAULT_SOUL_DOCUMENT: str = _SHARED_SETTINGS.get("soul_document", "SOUL.md")
VALID_PROMPT_MODES = {"soul", "executor", "both"}


def _to_base36(n: int) -> str:
    """Convert unsigned integer to base-36 string (matching JS Number.toString(36))."""
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    result: list[str] = []
    while n > 0:
        result.append(digits[n % 36])
        n //= 36
    return "".join(reversed(result))


def _djb2(s: str) -> str:
    """DJB2 hash matching the JavaScript implementation in hashMessages.ts.

    Uses signed 32-bit integer overflow semantics to match JS bitwise ops:
      hash = ((hash << 5) + hash + charCode) | 0
    Returns base-36 string of the unsigned 32-bit result (hash >>> 0).
    """
    h = 5381
    for ch in s:
        # Compute with arbitrary precision, then truncate to signed 32-bit
        h = (h << 5) + h + ord(ch)
        # Simulate JS `| 0`: truncate to signed 32-bit
        h = h & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    # Simulate JS `>>> 0`: convert to unsigned 32-bit
    return _to_base36(h & 0xFFFFFFFF)


@dataclass
class Session:
    file_key: str  # "project/filename.jsonl"
    messages: list[dict[str, Any]]  # transcript messages (source of truth)
    content_blocks: list[dict[str, Any]]  # streaming blocks for UI rendering
    chat_history: list[dict[str, Any]]  # Anthropic message format
    is_streaming: bool = False
    hit_limit: bool = False
    child_locked: bool = False
    consistency_suppressions: list[dict[str, str]] = field(default_factory=list)

    # Metadata (loaded from sidecar)
    outcome: str | None = None
    scenario: str | None = None
    mechanism: dict[int, str] | None = None
    summary: dict[int, str] | None = None
    sidecar_metadata: dict[str, Any] = field(default_factory=dict)  # Full sidecar for frontend (scores, evals, etc.)
    auto_fixed: dict[str, Any] = field(default_factory=dict)  # Auto-fixes applied on load
    mode: str = "edit"  # 'edit' or 'create'

    # Transcript hash (DJB2 of messages + metadata, matches frontend hashTranscriptState)
    transcript_hash: str | None = None

    # Per-session settings
    model: str = field(default_factory=lambda: DEFAULT_MODEL)
    api_key_id: str = "default"
    soul_document: str | None = None  # Per-session override; None = use global
    prompt_mode: str | None = None  # Per-session override; None = use global ("soul", "executor", "both")

    # Connected subscribers: subscriber_id → asyncio.Queue
    subscribers: dict[str, asyncio.Queue] = field(default_factory=dict)

    # Prevents two prompts from running simultaneously
    agent_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Set by "stop" to cancel a running agent mid-execution
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Last agent error (persisted across reconnects so new clients see it)
    last_error: str | None = None

    # Monitor evaluation state
    monitor_eval_id: str | None = None
    monitor_eval_status: str = "idle"  # idle, running, done, error
    monitor_eval_scores: dict[str, Any] = field(default_factory=dict)
    monitor_eval_error: str | None = None

    # True when messages have been modified in-memory and need flushing to disk.
    # Prevents stale cache from overwriting external edits on cleanup.
    dirty: bool = False

    # Cleanup timer handle
    _cleanup_task: asyncio.Task | None = field(default=None, repr=False)

    def compute_hash(self) -> str:
        """Compute transcript hash matching frontend hashTranscriptState.

        Uses DJB2 hash of JSON.stringify(messages) + '\\0' + JSON.stringify([outcome, scenario, mechanism]).
        Must exactly match the JavaScript implementation in src/hashMessages.ts.
        """
        # Build the same string as the frontend:
        # JSON.stringify(messages) + '\0' + JSON.stringify([outcome ?? null, scenario ?? null, mechanism ?? null])
        messages_json = json.dumps(self.messages, separators=(",", ":"), ensure_ascii=False)
        meta_array = [
            self.outcome if self.outcome is not None else None,
            self.scenario if self.scenario is not None else None,
            self.mechanism if self.mechanism is not None else None,
        ]
        meta_json = json.dumps(meta_array, separators=(",", ":"), ensure_ascii=False)
        content = messages_json + "\0" + meta_json
        return _djb2(content)

    def get_metadata(self) -> dict[str, Any]:
        """Build metadata dict from session state, excluding None/empty values."""
        meta: dict[str, Any] = {}
        if self.outcome is not None:
            meta["outcome"] = self.outcome
        if self.scenario is not None:
            meta["scenario"] = self.scenario
        if self.mechanism is not None:
            meta["mechanism"] = self.mechanism
        if self.summary is not None:
            meta["summary"] = self.summary
        if self.consistency_suppressions:
            meta["consistency_suppressions"] = self.consistency_suppressions
        return meta


def _parse_file_key(file_key: str) -> tuple[str, str]:
    """Split file_key into (project_dir_name, file_name)."""
    slash = file_key.index("/")
    return file_key[:slash], file_key[slash + 1 :]


def _load_transcript(
    file_key: str, transcripts_dir: Path | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Load transcript messages and sidecar metadata from disk.

    Also applies auto-fixes (minimization, tool ID fixing) and saves back if needed.
    Returns (messages, metadata, auto_fixed).
    """
    base_dir = transcripts_dir or TRANSCRIPTS_DIR
    project_dir, file_name = _parse_file_key(file_key)
    transcript_path = base_dir / project_dir / file_name

    messages: list[dict[str, Any]] = []
    if transcript_path.exists():
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    messages.append(json.loads(line))

    # Auto-detect full Claude Code format and minimize on the fly
    auto_fixed: dict[str, Any] = {}
    is_full = any(
        (
            "uuid" in m
            or "parentUuid" in m
            or "sessionId" in m
            or m.get("type") in ("summary", "file-history-snapshot", "queue-operation")
        )
        for m in messages[:10]
    )
    if is_full:
        try:
            from minimize import minimize_transcript as _minimize

            input_lines = [json.dumps(m) for m in messages]
            messages = _minimize(input_lines, keep_thinking=True, preserve_cwd=True)
            auto_fixed["minimized"] = True
        except ImportError:
            pass

    # Auto-fix invalid tool IDs
    try:
        from fix_ids import fix_transcript

        input_lines = [json.dumps(m) for m in messages]
        fixed_entries, id_map = fix_transcript(input_lines)
        ids_replaced = sum(1 for old, new in id_map.items() if old != new)
        if ids_replaced > 0:
            messages = fixed_entries
            auto_fixed["tool_ids_fixed"] = ids_replaced
    except ImportError:
        pass

    # Save back if any auto-fixes were applied
    if auto_fixed and transcript_path.exists():
        with open(transcript_path, "w") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    # Load sidecar metadata
    meta_path = transcript_path.with_suffix(".meta.json")
    metadata: dict[str, Any] = {}
    if meta_path.exists():
        with open(meta_path) as f:
            metadata = json.load(f)

    return messages, metadata, auto_fixed


def _save_transcript(file_key: str, messages: list[dict[str, Any]], transcripts_dir: Path | None = None) -> None:
    """Save transcript messages to disk as JSONL."""
    base_dir = transcripts_dir or TRANSCRIPTS_DIR
    project_dir, file_name = _parse_file_key(file_key)
    transcript_path = base_dir / project_dir / file_name
    transcript_path.parent.mkdir(parents=True, exist_ok=True)

    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def _save_metadata(file_key: str, metadata: dict[str, Any], transcripts_dir: Path | None = None) -> None:
    """Save sidecar metadata to disk."""
    base_dir = transcripts_dir or TRANSCRIPTS_DIR
    project_dir, file_name = _parse_file_key(file_key)
    transcript_path = base_dir / project_dir / file_name
    meta_path = transcript_path.with_suffix(".meta.json")
    meta_path.parent.mkdir(parents=True, exist_ok=True)

    # Merge with existing metadata
    existing: dict[str, Any] = {}
    if meta_path.exists():
        with open(meta_path) as f:
            existing = json.load(f)

    existing.update(metadata)

    with open(meta_path, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def _chat_path(file_key: str, transcripts_dir: Path | None = None) -> Path:
    """Return path to the .chat.json sidecar for a given file_key."""
    base_dir = transcripts_dir or TRANSCRIPTS_DIR
    project_dir, file_name = _parse_file_key(file_key)
    transcript_path = base_dir / project_dir / file_name
    return transcript_path.with_suffix(".chat.json")


def _save_chat(
    file_key: str,
    content_blocks: list[dict[str, Any]],
    chat_history: list[dict[str, Any]],
    transcripts_dir: Path | None = None,
) -> None:
    """Persist chat state to a .chat.json sidecar file."""
    if not content_blocks and not chat_history:
        return
    path = _chat_path(file_key, transcripts_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"content_blocks": content_blocks, "chat_history": chat_history}, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save chat for {file_key}: {e}")


def _load_chat(file_key: str, transcripts_dir: Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load chat state from .chat.json sidecar. Returns (content_blocks, chat_history)."""
    path = _chat_path(file_key, transcripts_dir)
    if not path.exists():
        return [], []
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("content_blocks", []), data.get("chat_history", [])
    except Exception as e:
        logger.error(f"Failed to load chat for {file_key}: {e}")
        return [], []


def _delete_chat(file_key: str, transcripts_dir: Path | None = None) -> None:
    """Remove the .chat.json sidecar file."""
    path = _chat_path(file_key, transcripts_dir)
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Failed to delete chat for {file_key}: {e}")


class SessionManager:
    """Manages active sessions. Thread-safe via asyncio locks."""

    # Keys persisted to global_settings.json (subset of global settings worth saving)
    _PERSISTED_KEYS = {
        "monitor_api_key_id",
        "n_evals",
        "n_evals_other",
        "prompt_variants",
        "eval_egregiousness",
        "eval_incriminating",
        "eval_effectiveness",
        "eval_confidence",
        "eval_realism",
        "goal_score",
        "prompt_mode",
        "llm_model",
        "llm_api_key_id",
        "auto_eval_enabled",
        "auto_eval_on_load",
        "tpm_default",
        "tpm_alt",
    }

    def __init__(self, transcripts_dir: Path | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()  # Light lock for _sessions dict mutations only
        self._key_locks: dict[str, asyncio.Lock] = {}  # Per-key locks for session loading
        self.transcripts_dir = transcripts_dir or TRANSCRIPTS_DIR

        # Global settings (shared across all sessions)
        self.soul_document: str = DEFAULT_SOUL_DOCUMENT
        self.prompt_mode: str = "soul"  # "soul", "executor", "both"
        self.child_lock_enabled: bool = False
        self.lock_first_message: bool = False
        self.n_evals: int = 5
        self.n_evals_other: int = 1
        self.prompt_variants: list[str] = list(DEFAULT_PROMPT_VARIANTS)
        self.eval_egregiousness: bool = True
        self.eval_incriminating: bool = True
        self.eval_effectiveness: bool = True
        self.eval_confidence: bool = True
        self.eval_realism: bool = True
        self.monitor_api_key_id: str = "default"
        self.goal_score: int = 10

        # LLM settings (shared across sessions)
        self.llm_model: str = DEFAULT_MODEL
        self.llm_api_key_id: str = "default"

        # Auto-eval settings
        self.auto_eval_enabled: bool = False
        self.auto_eval_on_load: bool = False

        # Rate limit TPM settings
        self.tpm_default: int = 2_000_000
        self.tpm_alt: int = 2_000_000

        # Load persisted settings from disk (overrides defaults above)
        self._load_persisted_settings()

        # Apply persisted TPM settings to rate_limit module
        self._apply_tpm_settings()

    @property
    def _settings_file(self) -> Path:
        return self.transcripts_dir / "global_settings.json"

    def _apply_tpm_settings(self) -> None:
        """Apply persisted TPM settings to the rate_limit module."""
        try:
            from rate_limit import set_limit

            set_limit("default", self.tpm_default)
            set_limit("alt", self.tpm_alt)
        except Exception as e:
            logger.warning(f"Failed to apply TPM settings: {e}")

    def _load_persisted_settings(self) -> None:
        """Load persisted global settings from disk."""
        if not self._settings_file.exists():
            return
        try:
            with open(self._settings_file) as f:
                saved = json.load(f)
            for key, value in saved.items():
                if hasattr(self, key):
                    setattr(self, key, value)
            logger.info(f"Loaded persisted global settings from {self._settings_file}")
        except Exception as e:
            logger.warning(f"Failed to load persisted settings: {e}")

    def _save_persisted_settings(self) -> None:
        """Save global settings to disk."""
        data = {k: getattr(self, k) for k in self._PERSISTED_KEYS if hasattr(self, k)}
        try:
            with open(self._settings_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save persisted settings: {e}")

    async def get_or_create(self, file_key: str) -> Session:
        """Get existing session or create one by loading from disk.

        Uses per-key locks so loading session A doesn't block session B.
        """
        # Fast path: session already exists (no lock needed for dict read)
        session = self._sessions.get(file_key)
        if session is not None:
            if session._cleanup_task and not session._cleanup_task.done():
                session._cleanup_task.cancel()
                session._cleanup_task = None
            return session

        # Get or create a per-key lock so only one task loads a given file
        async with self._lock:
            # Re-check under lock
            if file_key in self._sessions:
                session = self._sessions[file_key]
                if session._cleanup_task and not session._cleanup_task.done():
                    session._cleanup_task.cancel()
                    session._cleanup_task = None
                return session
            if file_key not in self._key_locks:
                self._key_locks[file_key] = asyncio.Lock()
            key_lock = self._key_locks[file_key]

        # Per-key lock: only one task loads this file; other files load in parallel
        async with key_lock:
            # Re-check after acquiring per-key lock
            if file_key in self._sessions:
                return self._sessions[file_key]

            # Load from disk in a thread (doesn't hold the global lock)
            transcripts_dir = self.transcripts_dir
            messages, metadata, auto_fixed = await asyncio.to_thread(_load_transcript, file_key, transcripts_dir)
            content_blocks, chat_history = await asyncio.to_thread(_load_chat, file_key, transcripts_dir)

            session = Session(
                file_key=file_key,
                messages=messages,
                content_blocks=content_blocks,
                chat_history=chat_history,
                outcome=metadata.get("outcome"),
                scenario=metadata.get("scenario"),
                mechanism=metadata.get("mechanism"),
                summary=metadata.get("summary"),
                sidecar_metadata=metadata,
                auto_fixed=auto_fixed,
                consistency_suppressions=metadata.get("consistency_suppressions", []),
            )
            session.transcript_hash = session.compute_hash()

            async with self._lock:
                self._sessions[file_key] = session
                # Clean up the per-key lock (session is now in _sessions, fast path will hit)
                self._key_locks.pop(file_key, None)

            logger.info(f"Created session for {file_key} ({len(messages)} messages, hash={session.transcript_hash})")
            return session

    async def subscribe(self, file_key: str) -> tuple[str, asyncio.Queue]:
        """Add a subscriber to a session. Returns (subscriber_id, queue).

        Cancels cleanup timer if one is pending.
        """
        session = await self.get_or_create(file_key)
        subscriber_id = uuid.uuid4().hex[:12]
        queue: asyncio.Queue = asyncio.Queue()
        session.subscribers[subscriber_id] = queue
        logger.info(f"Subscriber {subscriber_id} joined {file_key} ({len(session.subscribers)} subscribers)")
        return subscriber_id, queue

    async def unsubscribe(self, file_key: str, subscriber_id: str) -> None:
        """Remove a subscriber from a session."""
        async with self._lock:
            session = self._sessions.get(file_key)
            if not session:
                return

            session.subscribers.pop(subscriber_id, None)
            logger.info(f"Subscriber {subscriber_id} left {file_key} ({len(session.subscribers)} subscribers)")

            # Schedule cleanup if no subscribers remain
            if not session.subscribers:
                session._cleanup_task = asyncio.create_task(self._delayed_cleanup(file_key))

    async def _delayed_cleanup(self, file_key: str, delay: float = 60.0) -> None:
        """Flush to disk and remove session after delay with no subscribers."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        async with self._lock:
            session = self._sessions.get(file_key)
            if not session or session.subscribers:
                return  # Subscribers reconnected

            if session.is_streaming:
                # Agent still running (e.g. fork with no SSE subscribers), reschedule
                session._cleanup_task = asyncio.create_task(self._delayed_cleanup(file_key))
                return

            # Flush to disk
            await self._flush_session(session)

            del self._sessions[file_key]
            logger.info(f"Cleaned up session {file_key}")

    async def destroy_session(self, file_key: str) -> None:
        """Destroy a session without flushing to disk (used when file is deleted)."""
        async with self._lock:
            session = self._sessions.pop(file_key, None)
            if not session:
                return

            # Cancel any running agent
            session.cancel_event.set()

            # Cancel cleanup timer
            if session._cleanup_task and not session._cleanup_task.done():
                session._cleanup_task.cancel()

            # Notify subscribers that the session is gone
            for queue in session.subscribers.values():
                try:
                    queue.put_nowait({"type": "session_destroyed"})
                except asyncio.QueueFull:
                    pass
            session.subscribers.clear()

            logger.info(f"Destroyed session {file_key} (file deleted)")

    async def _flush_session(self, session: Session) -> None:
        """Save current session state to disk (async, runs I/O in threads).

        Skips transcript write if session was never modified (dirty=False),
        preventing stale cache from overwriting external file edits.
        """
        if session.dirty:
            try:
                await asyncio.to_thread(_save_transcript, session.file_key, session.messages, self.transcripts_dir)
                session.dirty = False
            except Exception as e:
                logger.error(f"Failed to save transcript {session.file_key}: {e}")
        else:
            logger.debug(f"Skipping transcript flush for {session.file_key} (not dirty)")

        meta = session.get_metadata()
        if meta:
            try:
                await asyncio.to_thread(_save_metadata, session.file_key, meta, self.transcripts_dir)
            except Exception as e:
                logger.error(f"Failed to save metadata {session.file_key}: {e}")

        # Persist chat state
        await asyncio.to_thread(
            _save_chat, session.file_key, session.content_blocks, session.chat_history, self.transcripts_dir
        )

    async def broadcast(self, session: Session, event: dict[str, Any]) -> None:
        """Send a JSON event to all subscriber queues."""
        if not session.subscribers:
            return

        for queue in session.subscribers.values():
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(f"Subscriber queue full for {session.file_key}, dropping event")

    async def rekey_session(self, old_file_key: str, new_file_key: str) -> None:
        """Update the dictionary key when a session's file is renamed."""
        async with self._lock:
            session = self._sessions.pop(old_file_key, None)
            if session:
                self._sessions[new_file_key] = session

    def get_session(self, file_key: str) -> Session | None:
        """Get session if it exists (no creation)."""
        return self._sessions.get(file_key)

    def get_session_state(self, session: Session) -> dict[str, Any]:
        """Build full session state for a newly connected client."""
        return {
            "type": "session_state",
            "file_key": session.file_key,
            "message_count": len(session.messages),
            "messages": session.messages,
            "content_blocks": session.content_blocks,
            "chat_history_length": len(session.chat_history),
            "is_streaming": session.is_streaming,
            "hit_limit": session.hit_limit,
            "outcome": session.outcome,
            "scenario": session.scenario,
            "mechanism": session.mechanism,
            "summary": session.summary,
            "metadata": session.sidecar_metadata if session.sidecar_metadata else None,
            "auto_fixed": session.auto_fixed if session.auto_fixed else None,
            "transcript_hash": session.transcript_hash,
            "mode": session.mode,
            "model": session.model,
            # Global settings (from SessionManager)
            "lock_first_message": self.lock_first_message,
            "child_lock_enabled": self.child_lock_enabled,
            "last_error": session.last_error,
            "n_evals": self.n_evals,
            "n_evals_other": self.n_evals_other,
            "prompt_variants": self.prompt_variants,
            "eval_egregiousness": self.eval_egregiousness,
            "eval_incriminating": self.eval_incriminating,
            "eval_effectiveness": self.eval_effectiveness,
            "eval_confidence": self.eval_confidence,
            "eval_realism": self.eval_realism,
            "monitor_api_key_id": self.monitor_api_key_id,
            "goal_score": self.goal_score,
            "llm_model": self.llm_model,
            "llm_api_key_id": self.llm_api_key_id,
            "auto_eval_enabled": self.auto_eval_enabled,
            "auto_eval_on_load": self.auto_eval_on_load,
            "tpm_default": self.tpm_default,
            "tpm_alt": self.tpm_alt,
            "monitor_eval_id": session.monitor_eval_id,
            "monitor_eval_status": session.monitor_eval_status,
            "monitor_eval_scores": session.monitor_eval_scores,
            "monitor_eval_error": session.monitor_eval_error,
        }

    def get_global_settings(self) -> dict[str, Any]:
        """Return all global settings as a dict."""
        return {
            "soul_document": self.soul_document,
            "prompt_mode": self.prompt_mode,
            "child_lock_enabled": self.child_lock_enabled,
            "lock_first_message": self.lock_first_message,
            "n_evals": self.n_evals,
            "n_evals_other": self.n_evals_other,
            "prompt_variants": self.prompt_variants,
            "eval_egregiousness": self.eval_egregiousness,
            "eval_incriminating": self.eval_incriminating,
            "eval_effectiveness": self.eval_effectiveness,
            "eval_confidence": self.eval_confidence,
            "eval_realism": self.eval_realism,
            "monitor_api_key_id": self.monitor_api_key_id,
            "goal_score": self.goal_score,
            "llm_model": self.llm_model,
            "llm_api_key_id": self.llm_api_key_id,
            "auto_eval_enabled": self.auto_eval_enabled,
            "auto_eval_on_load": self.auto_eval_on_load,
            "tpm_default": self.tpm_default,
            "tpm_alt": self.tpm_alt,
        }

    async def flush_all(self) -> None:
        """Flush all sessions to disk (for graceful shutdown)."""
        async with self._lock:
            for session in self._sessions.values():
                await self._flush_session(session)


# Global singleton
session_manager = SessionManager()
