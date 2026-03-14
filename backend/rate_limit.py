"""
Per-key input-token-per-minute (TPM) rate limiting.

Tracks input tokens consumed per API key over a rolling 60-second window.
Before each API call, checks if the key has budget remaining; if not, sleeps
until enough entries expire.

Each key ("default", "alt") has its own independent limit.
"""

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# ── Per-key state ────────────────────────────────────────────────────────

WINDOW_SECONDS = 60


@dataclass
class _KeyState:
    limit_tpm: int
    # Each entry: (timestamp, input_tokens)
    history: deque[tuple[float, int]] = field(default_factory=deque)
    # Running sum of tokens in the current window (avoids O(n) recount)
    _running_sum: int = 0
    queued: int = 0


# Initialised lazily on first access (needs event loop)
_keys: dict[str, _KeyState] = {}

# Default TPM limits — can be changed at runtime
_default_limits: dict[str, int] = {
    "default": 2_000_000,
    "alt": 2_000_000,
}

# Global enable/disable — when False, acquire() is a no-op
_enabled: bool = False

# Optional callback for broadcasting rate limit wait status to UI
# Signature: async (key_id: str, wait_seconds: float, used: int, limit: int) -> None
_on_rate_limit_wait: Callable[[str, float, int, int], Coroutine[Any, Any, None]] | None = None


def set_on_rate_limit_wait(callback: Callable[[str, float, int, int], Coroutine[Any, Any, None]] | None) -> None:
    """Set a callback that fires when the rate limiter starts waiting."""
    global _on_rate_limit_wait
    _on_rate_limit_wait = callback


def _get_key_state(key_id: str) -> _KeyState:
    if key_id not in _keys:
        limit = _default_limits.get(key_id, 2_000_000)
        _keys[key_id] = _KeyState(limit_tpm=limit)
    return _keys[key_id]


def _purge_old(state: _KeyState, now: float) -> None:
    """Remove entries older than the window, updating the running sum."""
    cutoff = now - WINDOW_SECONDS
    while state.history and state.history[0][0] < cutoff:
        _, tokens = state.history.popleft()
        state._running_sum -= tokens


def _tokens_in_window(state: _KeyState, now: float) -> int:
    _purge_old(state, now)
    return state._running_sum


# ── Public API ───────────────────────────────────────────────────────────


@asynccontextmanager
async def acquire(key_id: str = "default", cancel_event: asyncio.Event | None = None):
    """Wait until the key has TPM budget, then yield.

    Usage:
        async with acquire("default", cancel_event=session.cancel_event):
            response = await client.messages.create(...)

    If cancel_event is set during the wait, raises asyncio.CancelledError.
    After the call, use `record()` to log actual input tokens consumed.
    """
    if not _enabled:
        yield
        return

    state = _get_key_state(key_id)
    state.queued += 1
    try:
        while True:
            now = time.monotonic()
            used = _tokens_in_window(state, now)
            if used < state.limit_tpm:
                break
            # Check cancellation before sleeping
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Cancelled during rate limit wait")
            # Calculate how long until enough budget frees up
            needed = used - state.limit_tpm + 1
            freed = 0
            wait_until = now
            for ts, tokens in state.history:
                freed += tokens
                wait_until = ts + WINDOW_SECONDS
                if freed >= needed:
                    break
            sleep_for = max(0.1, wait_until - now)
            logger.info(
                f"[rate_limit] key={key_id} over budget ({used:,}/{state.limit_tpm:,} TPM), "
                f"sleeping {sleep_for:.1f}s"
            )
            # Fire the broadcast callback (non-blocking)
            if _on_rate_limit_wait is not None:
                try:
                    await _on_rate_limit_wait(key_id, sleep_for, used, state.limit_tpm)
                except Exception:
                    pass  # Don't let callback failures break rate limiting
            # Sleep — use a single await instead of busy-wait loop
            if cancel_event:
                # Sleep in chunks so we can check cancel_event
                remaining = sleep_for
                while remaining > 0:
                    if cancel_event.is_set():
                        raise asyncio.CancelledError("Cancelled during rate limit wait")
                    chunk = min(2.0, remaining)
                    await asyncio.sleep(chunk)
                    remaining -= chunk
            else:
                await asyncio.sleep(sleep_for)
    finally:
        state.queued -= 1

    yield


def record(key_id: str, input_tokens: int) -> None:
    """Record input tokens consumed by an API call. Call after response is received."""
    state = _get_key_state(key_id)
    now = time.monotonic()
    state.history.append((now, input_tokens))
    state._running_sum += input_tokens


# ── Config / status ──────────────────────────────────────────────────────


def get_status(key_id: str) -> dict:
    state = _get_key_state(key_id)
    now = time.monotonic()
    used = _tokens_in_window(state, now)
    return {
        "key_id": key_id,
        "limit_tpm": state.limit_tpm,
        "used_tpm": used,
        "remaining_tpm": max(0, state.limit_tpm - used),
        "queued": state.queued,
        "calls_in_window": len(state.history),
    }


def get_all_status() -> dict:
    """Return status for all known keys."""
    result = {}
    for key_id in set(_keys.keys()) | set(_default_limits.keys()):
        result[key_id] = get_status(key_id)
    return result


def set_limit(key_id: str, tpm: int) -> None:
    _default_limits[key_id] = tpm
    state = _get_key_state(key_id)
    state.limit_tpm = tpm


def set_enabled(enabled: bool) -> None:
    global _enabled
    _enabled = enabled


def is_enabled() -> bool:
    return _enabled
