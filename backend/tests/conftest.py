"""Shared fixtures for transcript editor integration tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Add backend to path so imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Test Data ────────────────────────────────────────────────────────────

SAMPLE_MESSAGES = [
    {
        "role": "user",
        "content": [{"type": "text", "text": "Please refactor the auth module"}],
        "cwd": "/home/user/project",
    },
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "I'll start by reading the auth module."},
            {
                "type": "tool_use",
                "id": "toolu_01ABC123",
                "name": "Read",
                "input": {"file_path": "/home/user/project/auth.py"},
            },
        ],
        "cwd": "/home/user/project",
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01ABC123",
                "content": "def login(user, password):\n    return True\n",
            }
        ],
        "cwd": "/home/user/project",
    },
    {
        "role": "assistant",
        "content": [{"type": "text", "text": "I've read the auth module. Let me refactor it."}],
        "cwd": "/home/user/project",
    },
]


def write_test_transcript(path: Path, messages: list[dict[str, Any]] | None = None) -> None:
    """Write a test transcript JSONL file."""
    msgs = messages or SAMPLE_MESSAGES
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for msg in msgs:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def read_transcript(path: Path) -> list[dict[str, Any]]:
    """Read a transcript JSONL file."""
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


# ── Mock Anthropic Streaming ─────────────────────────────────────────────


class MockContentBlock:
    """Mimics anthropic content block objects."""

    def __init__(self, block_type: str, **kwargs):
        self.type = block_type
        for k, v in kwargs.items():
            setattr(self, k, v)


class MockDelta:
    """Mimics anthropic delta objects."""

    def __init__(self, delta_type: str, **kwargs):
        self.type = delta_type
        for k, v in kwargs.items():
            setattr(self, k, v)


class MockStreamEvent:
    """Mimics anthropic stream events."""

    def __init__(self, event_type: str, **kwargs):
        self.type = event_type
        for k, v in kwargs.items():
            setattr(self, k, v)


def make_text_events(text: str) -> list[MockStreamEvent]:
    """Create events for a text content block."""
    return [
        MockStreamEvent(
            "content_block_start",
            content_block=MockContentBlock("text"),
        ),
        MockStreamEvent(
            "content_block_delta",
            delta=MockDelta("text_delta", text=text),
        ),
        MockStreamEvent("content_block_stop"),
    ]


def make_thinking_events(text: str) -> list[MockStreamEvent]:
    """Create events for a thinking content block."""
    return [
        MockStreamEvent(
            "content_block_start",
            content_block=MockContentBlock("thinking"),
        ),
        MockStreamEvent(
            "content_block_delta",
            delta=MockDelta("thinking_delta", thinking=text),
        ),
        MockStreamEvent("content_block_stop"),
    ]


def make_tool_call_events(name: str, tool_input: dict[str, Any], tool_id: str | None = None) -> list[MockStreamEvent]:
    """Create the 3-event sequence for a tool call: start → delta → stop."""
    tid = tool_id or f"toolu_test_{name}"
    input_json = json.dumps(tool_input, ensure_ascii=False)
    return [
        MockStreamEvent(
            "content_block_start",
            content_block=MockContentBlock("tool_use", id=tid, name=name),
        ),
        MockStreamEvent(
            "content_block_delta",
            delta=MockDelta("input_json_delta", partial_json=input_json),
        ),
        MockStreamEvent("content_block_stop"),
    ]


class MockUsage:
    """Mock usage object for rate limiting."""

    input_tokens = 100
    output_tokens = 50
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class MockFinalMessage:
    """Mock final message returned by get_final_message()."""

    usage = MockUsage()


class MockAnthropicStream:
    """Async context manager that yields scripted stream events."""

    def __init__(self, events: list[MockStreamEvent], gate: "asyncio.Event | None" = None):
        self._events = events
        self._gate = gate

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self._iter_events()

    async def _iter_events(self):
        if self._gate is not None:
            await self._gate.wait()
        for event in self._events:
            yield event

    async def get_final_message(self):
        return MockFinalMessage()


def make_mock_anthropic_client(*iterations: list[MockStreamEvent]):
    """Create a mock Anthropic client that returns scripted streams.

    Each argument is a list of events for one iteration of the agent loop.
    The mock will return streams in order for successive calls.
    """
    streams = [MockAnthropicStream(events) for events in iterations]
    call_count = 0

    mock_client = MagicMock()
    mock_messages = MagicMock()
    mock_client.messages = mock_messages

    def stream_side_effect(**kwargs):
        nonlocal call_count
        if call_count >= len(streams):
            # Return an empty stream (no tool calls → agent stops)
            return MockAnthropicStream([])
        s = streams[call_count]
        call_count += 1
        return s

    mock_messages.stream = MagicMock(side_effect=stream_side_effect)

    return mock_client


def make_gated_mock_anthropic_client(
    gate: asyncio.Event,
    *iterations: list[MockStreamEvent],
):
    """Like make_mock_anthropic_client, but the first stream blocks until gate is set.

    Use this for testing concurrent/stop behavior — the agent will block during
    the first API call until you call gate.set().
    """
    streams = []
    for i, events in enumerate(iterations):
        g = gate if i == 0 else None
        streams.append(MockAnthropicStream(events, gate=g))
    call_count = 0

    mock_client = MagicMock()
    mock_messages = MagicMock()
    mock_client.messages = mock_messages

    def stream_side_effect(**kwargs):
        nonlocal call_count
        if call_count >= len(streams):
            return MockAnthropicStream([])
        s = streams[call_count]
        call_count += 1
        return s

    mock_messages.stream = MagicMock(side_effect=stream_side_effect)

    return mock_client


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def transcripts_dir(tmp_path: Path) -> Path:
    """Create a temp transcripts directory with a test project and file."""
    project_dir = tmp_path / "test-project"
    project_dir.mkdir()
    write_test_transcript(project_dir / "sample.jsonl")
    return tmp_path


@pytest.fixture
def file_key() -> str:
    return "test-project/sample.jsonl"


@pytest.fixture
def test_app(transcripts_dir: Path):
    """Create a FastAPI test app with a test-scoped SessionManager."""
    import sessions as sessions_module
    from sessions import SessionManager

    # Create a test SessionManager pointing to the temp dir
    test_sm = SessionManager(transcripts_dir=transcripts_dir)

    # The agent module's _to_agent_thread and sessions' asyncio.to_thread
    # use run_in_executor which causes CancelledError in TestClient's
    # anyio event loop. Replace with direct synchronous calls.
    async def _sync_to_agent_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _sync_to_thread(fn, /, *args, **kwargs):
        return fn(*args, **kwargs)

    # Neutralize is a no-op in tests (avoid real API calls to rewrite text)
    def _noop_neutralize(_client, text):
        return text

    # Patch the global session_manager and TRANSCRIPTS_DIR
    with (
        patch.object(sessions_module, "session_manager", test_sm),
        patch.object(sessions_module, "TRANSCRIPTS_DIR", transcripts_dir),
        patch("agent._to_agent_thread", side_effect=_sync_to_agent_thread),
        patch("sessions.asyncio.to_thread", side_effect=_sync_to_thread),
        patch("neutralize.neutralize_text", _noop_neutralize, create=True),
    ):
        # Also patch in the sse router which imports session_manager
        import routers.sse as sse_module

        with patch.object(sse_module, "session_manager", test_sm):
            # Import and build the app fresh — but reuse existing app since
            # routers are already registered. Just patch the references.
            import main as main_module

            with patch.object(main_module, "session_manager", test_sm):
                yield main_module.app, test_sm, transcripts_dir
