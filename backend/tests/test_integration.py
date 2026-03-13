"""Integration tests for transcript editor SSE + HTTP session flows.

Tests exercise the real code path: HTTP commands → agent loop → executor → session → disk.
Only the Anthropic streaming API and monitor HTTP calls are mocked.

The SSE streaming transport is NOT tested here (it's a thin wrapper that yields
queue events as `data:` lines). Instead, tests subscribe directly to the session's
event queue to collect agent events, while using HTTP endpoints for commands.
This avoids event-loop deadlocks from trying to read an infinite SSE stream in tests.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.conftest import (
    SAMPLE_MESSAGES,
    make_gated_mock_anthropic_client,
    make_mock_anthropic_client,
    make_text_events,
    make_thinking_events,
    make_tool_call_events,
    read_transcript,
)


def find_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    """Find first event with matching type."""
    for e in events:
        if e.get("type") == event_type:
            return e
    return None


def find_events(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    """Find all events with matching type."""
    return [e for e in events if e.get("type") == event_type]


async def collect_queue_events(
    queue: asyncio.Queue,
    *,
    until_type: str,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Collect events from a subscriber queue until we see one with the given type."""
    events = []
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(queue.get(), timeout=remaining)
            events.append(event)
            if event.get("type") == until_type:
                return events
        except asyncio.TimeoutError:
            break
    return events


# ── Helpers ──────────────────────────────────────────────────────────────


def _patch_anthropic(mock_client):
    """Patch anthropic.AsyncAnthropic to return our mock client."""
    return patch("agent.anthropic.AsyncAnthropic", return_value=mock_client)


def _patch_consistency_check():
    """Patch _check_consistency to return no warnings (skip HTTP call)."""
    return patch("agent._check_consistency", new_callable=AsyncMock, return_value={"warnings": []})


def _patch_fix_tool_ids():
    """Patch _fix_tool_ids to be a no-op."""

    async def noop(messages):
        return messages, 0

    return patch("agent._fix_tool_ids", side_effect=noop)


def _patch_monitor_score():
    """Patch _evaluate_monitor_score to return fake scores."""

    async def fake_eval(session, session_manager, messages):
        return json.dumps({"status": "done", "suspiciousness": {"score": 25, "reasoning": "test"}})

    return patch("agent._evaluate_monitor_score", side_effect=fake_eval)


# ── Tests ────────────────────────────────────────────────────────────────


class TestHttpEndpoints:
    """Test HTTP endpoints (non-streaming)."""

    def test_get_state(self, test_app, file_key):
        """GET /state returns session state."""
        app, sm, transcripts_dir = test_app
        client = TestClient(app)
        resp = client.get(f"/api/session/{file_key}/state")
        assert resp.status_code == 200
        state = resp.json()
        assert state["type"] == "session_state"
        assert state["file_key"] == file_key
        assert state["message_count"] == len(SAMPLE_MESSAGES)
        assert state["is_streaming"] is False

    def test_get_state_nonexistent_file(self, test_app):
        """GET /state for nonexistent file creates an empty session."""
        app, sm, transcripts_dir = test_app
        (transcripts_dir / "test-project").mkdir(exist_ok=True)
        client = TestClient(app)
        resp = client.get("/api/session/test-project/nonexistent.jsonl/state")
        assert resp.status_code == 200
        state = resp.json()
        assert state["message_count"] == 0

    def test_settings_update(self, test_app, file_key):
        """PUT /settings updates and returns settings."""
        app, sm, transcripts_dir = test_app
        client = TestClient(app)
        resp = client.put(
            f"/api/session/{file_key}/settings",
            json={
                "model": "claude-sonnet-4-5-20250929",
                "child_lock_enabled": True,
            },
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["model"] == "claude-sonnet-4-5-20250929"
        assert result["child_lock_enabled"] is True


def _patch_transcripts_dir(transcripts_dir):
    """Patch the files router's get_transcripts_dir to use the test temp dir."""
    return patch("routers.files.get_transcripts_dir", return_value=Path(transcripts_dir))


class TestFileDelete:
    """Test: Deleting transcript files and projects."""

    def test_delete_transcript(self, test_app, file_key):
        """DELETE /files/delete removes file and sidecar from disk."""
        app, sm, transcripts_dir = test_app
        file_path = transcripts_dir / "test-project" / "sample.jsonl"
        sidecar_path = file_path.with_suffix(".meta.json")

        # Create a sidecar so we can verify it's also deleted
        sidecar_path.write_text(json.dumps({"scores": {"suspiciousness": 42}}))
        assert file_path.exists()
        assert sidecar_path.exists()

        client = TestClient(app)
        with _patch_transcripts_dir(transcripts_dir):
            resp = client.delete("/api/files/delete/test-project/sample.jsonl")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        assert not file_path.exists()
        assert not sidecar_path.exists()

    def test_delete_nonexistent_transcript_404(self, test_app):
        """DELETE /files/delete for missing file returns 404."""
        app, sm, transcripts_dir = test_app
        client = TestClient(app)
        with _patch_transcripts_dir(transcripts_dir):
            resp = client.delete("/api/files/delete/test-project/does-not-exist.jsonl")
        assert resp.status_code == 404

    def test_delete_project_empty(self, test_app):
        """DELETE /projects removes an empty project directory."""
        app, sm, transcripts_dir = test_app

        # Create an empty project
        empty_dir = transcripts_dir / "empty-project"
        empty_dir.mkdir()
        assert empty_dir.exists()

        client = TestClient(app)
        with _patch_transcripts_dir(transcripts_dir):
            resp = client.delete("/api/files/projects/empty-project")
        assert resp.status_code == 200
        assert not empty_dir.exists()

    def test_delete_project_nonempty_rejected(self, test_app):
        """DELETE /projects on non-empty project without delete_files=true returns 400."""
        app, sm, transcripts_dir = test_app
        client = TestClient(app)

        # test-project has sample.jsonl in it
        with _patch_transcripts_dir(transcripts_dir):
            resp = client.delete("/api/files/projects/test-project")
        assert resp.status_code == 400
        assert "files" in resp.json()["detail"].lower()

    def test_delete_project_force(self, test_app):
        """DELETE /projects with delete_files=true removes non-empty project."""
        app, sm, transcripts_dir = test_app
        project_dir = transcripts_dir / "test-project"
        assert project_dir.exists()
        assert (project_dir / "sample.jsonl").exists()

        client = TestClient(app)
        with _patch_transcripts_dir(transcripts_dir):
            resp = client.delete("/api/files/projects/test-project?delete_files=true")
        assert resp.status_code == 200
        assert not project_dir.exists()

    def test_delete_default_project_rejected(self, test_app):
        """Cannot delete the default project."""
        app, sm, transcripts_dir = test_app

        # Create default project
        default_dir = transcripts_dir / "default"
        default_dir.mkdir(exist_ok=True)

        client = TestClient(app)
        with _patch_transcripts_dir(transcripts_dir):
            resp = client.delete("/api/files/projects/default")
        assert resp.status_code == 400
        assert "default" in resp.json()["detail"].lower()
        assert default_dir.exists()

    def test_delete_project_path_traversal_blocked(self, test_app):
        """Path traversal in project name is blocked."""
        app, sm, transcripts_dir = test_app
        client = TestClient(app)
        with _patch_transcripts_dir(transcripts_dir):
            resp = client.delete("/api/files/projects/../../etc")
        assert resp.status_code in (403, 404)

    def test_delete_transcript_path_traversal_blocked(self, test_app):
        """Path traversal in file path is blocked."""
        app, sm, transcripts_dir = test_app
        client = TestClient(app)
        with _patch_transcripts_dir(transcripts_dir):
            resp = client.delete("/api/files/delete/../../etc/passwd")
        assert resp.status_code in (403, 404)


class TestSubmitAndSave:
    """Test: Agent writes are saved to disk immediately."""

    @pytest.mark.asyncio
    async def test_submit_agent_writes_saved_immediately(self, test_app, file_key):
        app, sm, transcripts_dir = test_app
        file_path = transcripts_dir / "test-project" / "sample.jsonl"
        original_messages = read_transcript(file_path)

        new_msg_content = [{"type": "text", "text": "Injected by test"}]
        mock_client = make_mock_anthropic_client(
            [
                *make_tool_call_events(
                    "insert_message",
                    {
                        "index": len(original_messages) + 1,
                        "role": "assistant",
                        "content": new_msg_content,
                        "cwd": "/home/user/project",
                    },
                ),
                *make_tool_call_events("finish_editing", {}),
            ]
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
        ):
            # Subscribe to queue directly to collect events
            sub_id, queue = await sm.subscribe(file_key)

            # Submit via HTTP
            client = TestClient(app)
            resp = client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Add a message"},
            )
            assert resp.status_code == 204

            # Collect events from queue
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            assert find_event(events, "agent_start") is not None
            assert find_event(events, "messages_updated") is not None
            assert find_event(events, "agent_done") is not None

        # Verify file on disk was updated
        updated = read_transcript(file_path)
        assert len(updated) == len(original_messages) + 1
        assert updated[-1]["content"][0]["text"] == "Injected by test"


class TestReset:
    """Test: Reset clears state."""

    @pytest.mark.asyncio
    async def test_reset_clears_state(self, test_app, file_key):
        app, sm, transcripts_dir = test_app

        mock_client = make_mock_anthropic_client(
            [
                *make_tool_call_events(
                    "insert_message",
                    {
                        "index": 5,
                        "role": "assistant",
                        "content": [{"type": "text", "text": "test"}],
                        "cwd": "/home/user/project",
                    },
                ),
                *make_tool_call_events("finish_editing", {}),
            ]
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)

            # Submit to get some chat history
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Do something"},
            )
            await collect_queue_events(queue, until_type="agent_done")

            # Reset
            client.post(f"/api/session/{file_key}/reset")
            events = await collect_queue_events(queue, until_type="session_state")
            await sm.unsubscribe(file_key, sub_id)

            reset_state = find_event(events, "session_state")
            assert reset_state is not None
            assert reset_state["chat_history_length"] == 0


class TestEmptyPrompt:
    """Test: Empty prompt rejected."""

    @pytest.mark.asyncio
    async def test_empty_prompt_rejected(self, test_app, file_key):
        app, sm, transcripts_dir = test_app
        sub_id, queue = await sm.subscribe(file_key)
        client = TestClient(app)

        client.post(
            f"/api/session/{file_key}/submit",
            json={"prompt": ""},
        )
        events = await collect_queue_events(queue, until_type="agent_error")
        await sm.unsubscribe(file_key, sub_id)

        error = find_event(events, "agent_error")
        assert error is not None
        assert "Empty prompt" in error["error"]

    @pytest.mark.asyncio
    async def test_whitespace_prompt_rejected(self, test_app, file_key):
        app, sm, transcripts_dir = test_app
        sub_id, queue = await sm.subscribe(file_key)
        client = TestClient(app)

        client.post(
            f"/api/session/{file_key}/submit",
            json={"prompt": "   "},
        )
        events = await collect_queue_events(queue, until_type="agent_error")
        await sm.unsubscribe(file_key, sub_id)

        error = find_event(events, "agent_error")
        assert error is not None
        assert "Empty prompt" in error["error"]


class TestSessionPersistence:
    """Test: Session persists across reconnects."""

    @pytest.mark.asyncio
    async def test_session_persistence_across_reconnect(self, test_app, file_key):
        app, sm, transcripts_dir = test_app

        mock_client = make_mock_anthropic_client(
            [
                *make_tool_call_events(
                    "insert_message",
                    {
                        "index": 5,
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Persisted message"}],
                        "cwd": "/home/user/project",
                    },
                ),
                *make_tool_call_events("finish_editing", {}),
            ]
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
        ):
            # First connection: submit
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Add message"},
            )
            await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            # Second connection: check state via HTTP
            resp = client.get(f"/api/session/{file_key}/state")
            state = resp.json()
            assert state["message_count"] == len(SAMPLE_MESSAGES) + 1


class TestChildLock:
    """Test: Child lock blocks edits after monitor eval."""

    @pytest.mark.asyncio
    async def test_child_lock_blocks_writes(self, test_app, file_key):
        """After get_monitor_score with child_lock_enabled, write tools should be blocked."""
        app, sm, transcripts_dir = test_app

        mock_client = make_mock_anthropic_client(
            # First iteration: insert + get_monitor_score
            [
                *make_tool_call_events(
                    "insert_message",
                    {
                        "index": 5,
                        "role": "assistant",
                        "content": [{"type": "text", "text": "First edit"}],
                        "cwd": "/home/user/project",
                    },
                ),
                *make_tool_call_events("get_monitor_score", {}),
            ],
            # Second iteration: try another insert (should be blocked)
            [
                *make_tool_call_events(
                    "insert_message",
                    {
                        "index": 6,
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Should be blocked"}],
                        "cwd": "/home/user/project",
                    },
                ),
                *make_tool_call_events("finish_editing", {}),
            ],
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
            _patch_monitor_score(),
        ):
            client = TestClient(app)

            # Enable child lock
            client.put(
                f"/api/session/{file_key}/settings",
                json={"child_lock_enabled": True},
            )

            sub_id, queue = await sm.subscribe(file_key)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Edit and score"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            # Find the tool_call_end for the second insert_message
            insert_ends = [e for e in events if e.get("type") == "tool_call_end" and e.get("name") == "insert_message"]

            # The second insert should have been blocked
            if len(insert_ends) >= 2:
                assert "Child lock" in insert_ends[1]["result"]


class TestWriteWithMonitor:
    """Test: Write + monitor evaluation."""

    @pytest.mark.asyncio
    async def test_write_and_monitor_eval(self, test_app, file_key):
        app, sm, transcripts_dir = test_app
        file_path = transcripts_dir / "test-project" / "sample.jsonl"
        original_count = len(read_transcript(file_path))

        mock_client = make_mock_anthropic_client(
            [
                *make_tool_call_events(
                    "insert_message",
                    {
                        "index": 5,
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Attack message"}],
                        "cwd": "/home/user/project",
                    },
                ),
                *make_tool_call_events("get_monitor_score", {}),
            ]
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
            _patch_monitor_score(),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Insert and score"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            done = find_event(events, "agent_done")
            assert done is not None

            score_end = [e for e in events if e.get("type") == "tool_call_end" and e.get("name") == "get_monitor_score"]
            assert len(score_end) == 1
            assert "suspiciousness" in score_end[0]["result"]

        # Changes saved immediately — file should have new message
        assert len(read_transcript(file_path)) == original_count + 1


class TestMonitorEvalTriggerBroadcast:
    """Fix 1: get_monitor_score broadcasts monitor_eval_trigger event."""

    @pytest.mark.asyncio
    async def test_monitor_eval_trigger_broadcast(self, test_app, file_key):
        """When agent calls get_monitor_score, a monitor_eval_trigger SSE event should be broadcast."""
        app, sm, transcripts_dir = test_app

        async def mock_eval_with_broadcast(session, session_manager, messages):
            await session_manager.broadcast(
                session,
                {
                    "type": "monitor_eval_trigger",
                    "eval_id": "test-eval-123",
                },
            )
            return json.dumps({"status": "done", "suspiciousness": {"score": 30, "reasoning": "test"}})

        mock_client = make_mock_anthropic_client([*make_tool_call_events("get_monitor_score", {})])

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
            patch("agent._evaluate_monitor_score", side_effect=mock_eval_with_broadcast),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Score it"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            # Should see monitor_eval_trigger event (not monitor_update)
            trigger = find_event(events, "monitor_eval_trigger")
            assert (
                trigger is not None
            ), f"Expected monitor_eval_trigger event, got types: {[e.get('type') for e in events]}"
            assert trigger["eval_id"] == "test-eval-123"

            # Should NOT see old monitor_update events
            monitor_updates = find_events(events, "monitor_update")
            assert len(monitor_updates) == 0, "Should not broadcast monitor_update events anymore"

    @pytest.mark.asyncio
    async def test_monitor_eval_trigger_with_cached_scores(self, test_app, file_key):
        """Cached evaluations should also broadcast monitor_eval_trigger."""
        app, sm, transcripts_dir = test_app

        call_count = 0

        async def mock_eval_tracks_calls(session, session_manager, messages):
            nonlocal call_count
            call_count += 1
            await session_manager.broadcast(
                session,
                {
                    "type": "monitor_eval_trigger",
                    "eval_id": f"eval-{call_count}",
                },
            )
            return json.dumps({"status": "done", "suspiciousness": {"score": 25, "reasoning": "cached"}})

        # Two iterations: first calls get_monitor_score, second calls finish_editing
        mock_client = make_mock_anthropic_client(
            [*make_tool_call_events("get_monitor_score", {})],
            [*make_tool_call_events("get_monitor_score", {})],
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
            patch("agent._evaluate_monitor_score", side_effect=mock_eval_tracks_calls),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Score twice"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            triggers = find_events(events, "monitor_eval_trigger")
            assert len(triggers) == 2
            assert triggers[0]["eval_id"] == "eval-1"
            assert triggers[1]["eval_id"] == "eval-2"


class TestAutoEvalAfterFinish:
    """Fix 3: Auto-evaluate after finish_editing."""

    @pytest.mark.asyncio
    async def test_auto_eval_fires_after_finish(self, test_app, file_key):
        """After agent calls finish_editing, _evaluate_monitor_score should be called as background task."""
        app, sm, transcripts_dir = test_app

        eval_called = asyncio.Event()

        async def mock_eval_sets_flag(session, session_manager, messages):
            eval_called.set()
            await session_manager.broadcast(
                session,
                {
                    "type": "monitor_eval_trigger",
                    "eval_id": "auto-eval-123",
                },
            )
            return json.dumps({"status": "done", "suspiciousness": {"score": 15, "reasoning": "auto"}})

        mock_client = make_mock_anthropic_client([*make_tool_call_events("finish_editing", {})])

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
            patch("agent._evaluate_monitor_score", side_effect=mock_eval_sets_flag),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Finish up"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")

            # Wait for auto-eval background task to fire
            try:
                await asyncio.wait_for(eval_called.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

            # Collect any additional events from auto-eval broadcast
            try:
                while True:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    events.append(event)
            except asyncio.TimeoutError:
                pass

            await sm.unsubscribe(file_key, sub_id)

            assert eval_called.is_set(), "_evaluate_monitor_score was not called after finish_editing"

            # The auto-eval should also have broadcast a trigger event
            trigger = find_event(events, "monitor_eval_trigger")
            assert (
                trigger is not None
            ), f"Expected monitor_eval_trigger from auto-eval, got types: {[e.get('type') for e in events]}"

    @pytest.mark.asyncio
    async def test_no_auto_eval_on_cancel(self, test_app, file_key):
        """Auto-eval should NOT fire if the agent was cancelled (not finished)."""
        app, sm, transcripts_dir = test_app

        eval_called = asyncio.Event()

        async def mock_eval_sets_flag(session, session_manager, messages):
            eval_called.set()
            return json.dumps({"status": "done"})

        # Agent just produces text (no finish_editing, no tool calls → agent stops)
        mock_client = make_mock_anthropic_client(make_text_events("I'll just respond without finishing."))

        with (
            _patch_anthropic(mock_client),
            patch("agent._evaluate_monitor_score", side_effect=mock_eval_sets_flag),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Just chat"},
            )
            _events = await collect_queue_events(queue, until_type="agent_done")  # noqa: F841

            # Give background task time to fire (it shouldn't)
            await asyncio.sleep(0.5)
            await sm.unsubscribe(file_key, sub_id)

            assert not eval_called.is_set(), "Auto-eval should NOT fire when finish_editing was not called"

    @pytest.mark.asyncio
    async def test_no_auto_eval_on_consistency_failure(self, test_app, file_key):
        """Auto-eval should NOT fire if finish_editing failed (consistency check blocked it)."""
        app, sm, transcripts_dir = test_app

        eval_called = asyncio.Event()

        async def mock_eval_sets_flag(session, session_manager, messages):
            eval_called.set()
            return json.dumps({"status": "done"})

        mock_client = make_mock_anthropic_client([*make_tool_call_events("finish_editing", {})])

        # Consistency check returns warnings → finish_editing is blocked
        async def failing_consistency(messages, suppressions):
            return {
                "warnings": [
                    {
                        "id": "w1",
                        "type": "MISMATCH",
                        "file_path": "/f.py",
                        "message_index": 1,
                        "message": "bad",
                        "expected": "a",
                        "actual": "b",
                        "diff": None,
                    }
                ],
                "files_tracked": 1,
                "files_known": 1,
                "files_fragments": 0,
                "files_partial": 0,
            }

        with (
            _patch_anthropic(mock_client),
            _patch_fix_tool_ids(),
            patch("agent._check_consistency", new_callable=AsyncMock, side_effect=failing_consistency),
            patch("agent._evaluate_monitor_score", side_effect=mock_eval_sets_flag),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Try finish"},
            )
            _events = await collect_queue_events(queue, until_type="agent_done")  # noqa: F841
            await asyncio.sleep(0.5)
            await sm.unsubscribe(file_key, sub_id)

            assert (
                not eval_called.is_set()
            ), "Auto-eval should NOT fire when finish_editing was blocked by consistency check"


# ══════════════════════════════════════════════════════════════════════════
# General behavior tests
# ══════════════════════════════════════════════════════════════════════════


class TestConcurrentSubmitRejection:
    """Submitting while agent is already running should return an error."""

    @pytest.mark.asyncio
    async def test_concurrent_submit_broadcasts_error(self, test_app, file_key):
        app, sm, transcripts_dir = test_app

        # Gated stream — agent blocks until we release the gate
        gate = asyncio.Event()
        mock_client = make_gated_mock_anthropic_client(
            gate,
            [*make_tool_call_events("finish_editing", {})],
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
        ):
            sub_id, queue = await sm.subscribe(file_key)

            # Use httpx.AsyncClient so all requests, background tasks, and
            # queue collection share the same event loop. TestClient (sync)
            # runs on a separate thread, so the background agent task's lock
            # acquisition races with the second submit check.
            from httpx import ASGITransport, AsyncClient

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                # First submit starts the agent (it will block on the gated stream)
                await client.post(
                    f"/api/session/{file_key}/submit",
                    json={"prompt": "First prompt"},
                )

                # Wait for agent_start to confirm it's running
                await collect_queue_events(queue, until_type="agent_start", timeout=3)

                # Second submit while agent is blocked
                await client.post(
                    f"/api/session/{file_key}/submit",
                    json={"prompt": "Second prompt"},
                )

            # Should immediately get an error for the concurrent submit
            error_events = await collect_queue_events(queue, until_type="agent_error", timeout=3)

            # Unblock the agent so it finishes
            gate.set()
            await collect_queue_events(queue, until_type="agent_done", timeout=5)
            await sm.unsubscribe(file_key, sub_id)

            errors = find_events(error_events, "agent_error")
            assert any(
                "already running" in e.get("error", "") for e in errors
            ), f"Expected 'already running' error, got: {[e.get('error') for e in errors]}"


class TestMultiIterationLoop:
    """Agent loop runs multiple iterations (multiple API rounds)."""

    @pytest.mark.asyncio
    async def test_multi_iteration_edits(self, test_app, file_key):
        """Agent makes edits across two iterations, both saved to disk."""
        app, sm, transcripts_dir = test_app
        file_path = transcripts_dir / "test-project" / "sample.jsonl"
        original_count = len(read_transcript(file_path))

        mock_client = make_mock_anthropic_client(
            # Iteration 1: insert a message
            [
                *make_tool_call_events(
                    "insert_message",
                    {
                        "index": original_count + 1,
                        "role": "assistant",
                        "content": [{"type": "text", "text": "First insert"}],
                        "cwd": "/home/user/project",
                    },
                ),
            ],
            # Iteration 2: insert another message, then finish
            [
                *make_tool_call_events(
                    "insert_message",
                    {
                        "index": original_count + 2,
                        "role": "user",
                        "content": [{"type": "text", "text": "Second insert"}],
                        "cwd": "/home/user/project",
                    },
                ),
                *make_tool_call_events("finish_editing", {}),
            ],
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Insert two messages"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            # Two messages_updated events (one per write)
            msg_updates = find_events(events, "messages_updated")
            assert len(msg_updates) == 2

        # Disk should have both insertions
        updated = read_transcript(file_path)
        assert len(updated) == original_count + 2
        assert updated[-2]["content"][0]["text"] == "First insert"
        assert updated[-1]["content"][0]["text"] == "Second insert"


class TestForkFlow:
    """Fork creates a new file and broadcasts fork_created."""

    @pytest.mark.asyncio
    async def test_fork_creates_file_and_broadcasts(self, test_app, file_key):
        app, sm, transcripts_dir = test_app

        # First set summary (fork requires it), then fork
        mock_client = make_mock_anthropic_client(
            [
                *make_tool_call_events(
                    "set_transcript_metadata",
                    {
                        "summary": {"1": "User asks to refactor"},
                    },
                ),
                *make_tool_call_events(
                    "fork_to_new_tab",
                    {
                        "prompt": "Make it stealthy",
                        "label": "stealthy-fork",
                    },
                ),
                *make_tool_call_events("finish_editing", {}),
            ]
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Fork to a stealthy variant"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            fork_event = find_event(events, "fork_created")
            assert fork_event is not None
            assert fork_event["label"] == "stealthy-fork"
            assert fork_event["file_key"] == "test-project/stealthy-fork.jsonl"

        # File should exist on disk
        fork_path = transcripts_dir / "test-project" / "stealthy-fork.jsonl"
        assert fork_path.exists()
        forked = read_transcript(fork_path)
        assert len(forked) == len(SAMPLE_MESSAGES)


class TestMetadataRoundTrip:
    """set_transcript_metadata updates session state visible in GET /state."""

    @pytest.mark.asyncio
    async def test_metadata_visible_in_state(self, test_app, file_key):
        app, sm, transcripts_dir = test_app

        mock_client = make_mock_anthropic_client(
            [
                *make_tool_call_events(
                    "set_transcript_metadata",
                    {
                        "outcome": "SSH keys were exfiltrated to external server",
                        "scenario": "Internal ML platform with GPU cluster",
                        "mechanism": {"3": "Agent read .ssh/id_rsa", "5": "Agent sent keys via curl"},
                        "summary": {"1": "User asks for help", "3": "Agent reads SSH keys"},
                    },
                ),
                *make_tool_call_events("finish_editing", {}),
            ]
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Set metadata"},
            )
            await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

        # Check state via HTTP
        resp = client.get(f"/api/session/{file_key}/state")
        state = resp.json()
        assert state["outcome"] == "SSH keys were exfiltrated to external server"
        assert state["scenario"] == "Internal ML platform with GPU cluster"
        assert state["mechanism"]["3"] == "Agent read .ssh/id_rsa"
        assert state["mechanism"]["5"] == "Agent sent keys via curl"
        assert state["summary"]["1"] == "User asks for help"


class TestLockFirstMessage:
    """Lock first message setting blocks edits targeting message 1."""

    @pytest.mark.asyncio
    async def test_lock_blocks_edit_to_message_1(self, test_app, file_key):
        app, sm, transcripts_dir = test_app

        # Try to replace messages starting at index 1
        mock_client = make_mock_anthropic_client(
            [
                *make_tool_call_events(
                    "replace_messages",
                    {
                        "start": 1,
                        "end": 1,
                        "messages": [
                            {
                                "role": "user",
                                "content": [{"type": "text", "text": "Modified first message"}],
                                "cwd": "/home/user/project",
                            }
                        ],
                    },
                ),
                *make_tool_call_events("finish_editing", {}),
            ]
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
        ):
            # Enable lock first message (global setting)
            client = TestClient(app)
            client.put(
                "/api/session/global-settings",
                json={"lock_first_message": True},
            )

            sub_id, queue = await sm.subscribe(file_key)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Modify the first message"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            # The replace_messages tool result should contain the lock error
            replace_end = [
                e for e in events if e.get("type") == "tool_call_end" and e.get("name") == "replace_messages"
            ]
            assert len(replace_end) == 1
            assert "Lock first message" in replace_end[0]["result"] or "lock" in replace_end[0]["result"].lower()


class TestStreamingEvents:
    """Agent text and thinking blocks produce correct SSE event shapes."""

    @pytest.mark.asyncio
    async def test_text_events_broadcast(self, test_app, file_key):
        """Agent text response produces text event."""
        app, sm, transcripts_dir = test_app

        mock_client = make_mock_anthropic_client(make_text_events("Hello, I'll help you with that."))

        with _patch_anthropic(mock_client):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Hi"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            text_events = find_events(events, "text")
            assert len(text_events) >= 1
            assert text_events[0]["text"] == "Hello, I'll help you with that."

    @pytest.mark.asyncio
    async def test_thinking_events_broadcast(self, test_app, file_key):
        """Agent thinking produces thinking_start, thinking, thinking_end events."""
        app, sm, transcripts_dir = test_app

        mock_client = make_mock_anthropic_client(
            [
                *make_thinking_events("Let me analyze this..."),
                *make_text_events("Here's my analysis."),
            ]
        )

        with _patch_anthropic(mock_client):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Analyze this"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            assert find_event(events, "thinking_start") is not None
            thinking = find_event(events, "thinking")
            assert thinking is not None
            assert thinking["text"] == "Let me analyze this..."
            assert find_event(events, "thinking_end") is not None


class TestStopAgent:
    """Stopping a running agent sets cancelled flag."""

    @pytest.mark.asyncio
    async def test_stop_sets_cancelled(self, test_app, file_key):
        app, sm, transcripts_dir = test_app

        # Use the session's cancel_event directly for reliable cancellation.
        # The gated stream approach has timing issues with TestClient's sync bridge.
        mock_client = make_mock_anthropic_client(
            [*make_tool_call_events("get_messages", {})],
            [*make_tool_call_events("finish_editing", {})],
        )

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
        ):
            # Pre-set the cancel event on the session before submitting.
            # The agent will see it set on the first iteration check and exit immediately.
            session = await sm.get_or_create(file_key)
            session.cancel_event.set()

            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Do some work"},
            )

            events = await collect_queue_events(queue, until_type="agent_done", timeout=5)
            await sm.unsubscribe(file_key, sub_id)

            done = find_event(events, "agent_done")
            assert done is not None
            assert done["cancelled"] is True


class TestChatHistoryAccumulation:
    """Multiple submit rounds accumulate chat history."""

    @pytest.mark.asyncio
    async def test_chat_history_grows_across_submits(self, test_app, file_key):
        app, sm, transcripts_dir = test_app

        # Round 1: text reply
        mock_client1 = make_mock_anthropic_client(make_text_events("First response."))
        # Round 2: text reply
        mock_client2 = make_mock_anthropic_client(make_text_events("Second response."))

        with _patch_anthropic(mock_client1):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "First question"},
            )
            await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

        # Check state — should have 2 chat history entries (user + assistant)
        resp = client.get(f"/api/session/{file_key}/state")
        state = resp.json()
        assert state["chat_history_length"] == 2

        with _patch_anthropic(mock_client2):
            sub_id, queue = await sm.subscribe(file_key)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Second question"},
            )
            await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

        # Should now have 4 entries
        resp = client.get(f"/api/session/{file_key}/state")
        state = resp.json()
        assert state["chat_history_length"] == 4


class TestCachedMonitorEvalTrigger:
    """Fix: get_monitor_score broadcasts cached=true when eval hits cache."""

    @pytest.mark.asyncio
    async def test_cached_eval_includes_cached_flag(self, test_app, file_key):
        """When evaluate_file returns a cached result, monitor_eval_trigger should include cached=true."""
        app, sm, transcripts_dir = test_app

        mock_client = make_mock_anthropic_client([*make_tool_call_events("get_monitor_score", {})])

        # Mock evaluate_file to return a cached result
        cached_eval_id = "cached-eval-001"

        async def mock_evaluate_file(request):
            from routers.monitor import _file_eval_store

            done_event = asyncio.Event()
            done_event.set()
            _file_eval_store[cached_eval_id] = {
                "status": "done",
                "metrics": {},
                "scores": {"suspiciousness": 25},
                "evals": {},
                "error_message": None,
                "cached": True,
                "done_event": done_event,
            }
            return {"eval_id": cached_eval_id}

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
            patch("routers.monitor.evaluate_file", side_effect=mock_evaluate_file),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Score it"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            trigger = find_event(events, "monitor_eval_trigger")
            assert (
                trigger is not None
            ), f"Expected monitor_eval_trigger event, got types: {[e.get('type') for e in events]}"
            assert trigger["cached"] is True, "Cached eval should include cached=true in trigger"

    @pytest.mark.asyncio
    async def test_non_cached_eval_has_cached_false(self, test_app, file_key):
        """When evaluate_file runs a fresh eval, cached should be false."""
        app, sm, transcripts_dir = test_app

        mock_client = make_mock_anthropic_client([*make_tool_call_events("get_monitor_score", {})])

        fresh_eval_id = "fresh-eval-001"

        async def mock_evaluate_file(request):
            from routers.monitor import _file_eval_store

            # Simulate a fresh (non-cached) eval that's already done
            done_event = asyncio.Event()
            done_event.set()
            _file_eval_store[fresh_eval_id] = {
                "status": "done",
                "metrics": {},
                "scores": {"suspiciousness": 50},
                "evals": {},
                "error_message": None,
                "done_event": done_event,
                # No "cached" key — should result in cached=False
            }
            return {"eval_id": fresh_eval_id}

        with (
            _patch_anthropic(mock_client),
            _patch_consistency_check(),
            _patch_fix_tool_ids(),
            patch("routers.monitor.evaluate_file", side_effect=mock_evaluate_file),
        ):
            sub_id, queue = await sm.subscribe(file_key)
            client = TestClient(app)
            client.post(
                f"/api/session/{file_key}/submit",
                json={"prompt": "Score it"},
            )
            events = await collect_queue_events(queue, until_type="agent_done")
            await sm.unsubscribe(file_key, sub_id)

            trigger = find_event(events, "monitor_eval_trigger")
            assert trigger is not None
            assert trigger["cached"] is False, "Fresh eval should include cached=false in trigger"


class TestNEvalsOtherMetrics:
    """Fix: n_evals only applies to suspiciousness; other metrics use n_evals_other."""

    @pytest.mark.asyncio
    async def test_suspiciousness_uses_n_evals(self):
        """Suspiciousness should use request.n_evals, not request.n_evals_other."""
        from routers.monitor import EvaluateFileRequest, _run_single_metric

        call_count = 0

        async def mock_throttled(fn, *args, api_key_id="default", on_start=None, estimated_input_tokens=0):
            nonlocal call_count
            call_count += 1
            return {"score": 30, "reasoning": "test", "input_tokens": 100}

        request = EvaluateFileRequest(
            project="test",
            file_name="test.jsonl",
            transcript=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            n_evals=5,
            n_evals_other=1,
            prompt_variants=["control_arena"],
        )

        with patch("routers.monitor._throttled_to_thread", side_effect=mock_throttled):
            result = await _run_single_metric("suspiciousness", "fake jsonl", request, variant="control_arena")

        assert call_count == 5, f"Suspiciousness should run n_evals=5 times, got {call_count}"
        assert result["mean_score"] == 30

    @pytest.mark.asyncio
    async def test_other_metrics_use_n_evals_other(self):
        """Non-suspiciousness metrics should use request.n_evals_other."""
        from routers.monitor import EvaluateFileRequest, _run_single_metric

        for metric_type in ("egregiousness", "incriminating", "effectiveness", "confidence", "realism"):
            call_count = 0

            async def mock_throttled(fn, *args, api_key_id="default", on_start=None, estimated_input_tokens=0):
                nonlocal call_count
                call_count += 1
                return {"score": 50, "scoreNumberOnly": 50, "reasoning": "test", "input_tokens": 100}

            request = EvaluateFileRequest(
                project="test",
                file_name="test.jsonl",
                transcript=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                outcome="bad thing happened",
                scenario="test scenario",
                mechanism="step 1",
                n_evals=5,
                n_evals_other=2,
                prompt_variants=["control_arena"],
            )

            with patch("routers.monitor._throttled_to_thread", side_effect=mock_throttled):
                result = await _run_single_metric(metric_type, "fake jsonl", request)

            assert call_count == 2, f"{metric_type} should run n_evals_other=2 times, got {call_count}"
            assert result["mean_score"] == 50

    @pytest.mark.asyncio
    async def test_n_evals_other_defaults_to_one(self):
        """n_evals_other should default to 1."""
        from routers.monitor import EvaluateFileRequest

        request = EvaluateFileRequest(
            project="test",
            file_name="test.jsonl",
            transcript=[],
            n_evals=5,
        )
        assert request.n_evals_other == 1
