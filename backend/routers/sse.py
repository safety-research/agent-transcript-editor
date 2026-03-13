"""
SSE + HTTP endpoints for agent sessions.

Replaces the WebSocket endpoint. Server->client streaming uses SSE (EventSource),
client->server commands use plain HTTP POST/PUT.

SSE stream (mounted at /api/session):
  GET /{file_key}/events -> text/event-stream

Commands:
  POST /{file_key}/submit   { "prompt": "..." }
  POST /{file_key}/stop
  POST /{file_key}/reset
  GET  /{file_key}/state
  PUT  /{file_key}/settings  { model?, api_key_id?, mode? }

Global settings (not per-session):
  GET  /global-settings
  PUT  /global-settings  { child_lock_enabled?, lock_first_message?, ... }

Event types (same as before):
  session_state, agent_start, thinking_start, thinking, thinking_end,
  text, tool_call_start, tool_call_input, tool_call_end,
  messages_updated, agent_done, agent_error, fork_created,
  monitor_update, settings_updated, global_settings_updated,
  file_renamed, user_message
"""

from __future__ import annotations

import asyncio
import json
import logging

from agent import run_agent
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sessions import _delete_chat, _save_metadata, _save_transcript, session_manager


def _save_transcript_async(*a, **kw):
    return asyncio.to_thread(_save_transcript, *a, **kw)


def _save_metadata_async(*a, **kw):
    return asyncio.to_thread(_save_metadata, *a, **kw)


logger = logging.getLogger(__name__)
router = APIRouter()

KEEPALIVE_INTERVAL = 25  # seconds


# ── Global settings (MUST be defined before {file_key:path} routes) ────


class GlobalSettingsBody(BaseModel):
    soul_document: str | None = None
    prompt_mode: str | None = None  # "soul", "executor", "both"
    child_lock_enabled: bool | None = None
    lock_first_message: bool | None = None
    n_evals: int | None = None
    n_evals_other: int | None = None
    prompt_variants: list[str] | None = None
    eval_egregiousness: bool | None = None
    eval_incriminating: bool | None = None
    eval_effectiveness: bool | None = None
    eval_confidence: bool | None = None
    eval_realism: bool | None = None
    monitor_api_key_id: str | None = None
    goal_score: int | None = None
    tpm_default: int | None = None
    tpm_alt: int | None = None
    llm_model: str | None = None
    llm_api_key_id: str | None = None
    auto_eval_enabled: bool | None = None
    auto_eval_on_load: bool | None = None


@router.get("/global-settings")
async def get_global_settings():
    """Get global settings."""
    return session_manager.get_global_settings()


@router.put("/global-settings")
async def update_global_settings(body: GlobalSettingsBody):
    """Update global settings and broadcast to all sessions."""
    from rate_limit import set_limit

    if body.soul_document is not None:
        session_manager.soul_document = body.soul_document
    if body.prompt_mode is not None:
        from sessions import VALID_PROMPT_MODES

        if body.prompt_mode not in VALID_PROMPT_MODES:
            raise ValueError(f"Invalid prompt_mode: {body.prompt_mode}, must be one of {VALID_PROMPT_MODES}")
        session_manager.prompt_mode = body.prompt_mode
    if body.child_lock_enabled is not None:
        session_manager.child_lock_enabled = body.child_lock_enabled
    if body.lock_first_message is not None:
        session_manager.lock_first_message = body.lock_first_message
    if body.n_evals is not None:
        session_manager.n_evals = body.n_evals
    if body.n_evals_other is not None:
        session_manager.n_evals_other = body.n_evals_other
    if body.prompt_variants is not None:
        session_manager.prompt_variants = body.prompt_variants
    if body.eval_egregiousness is not None:
        session_manager.eval_egregiousness = body.eval_egregiousness
    if body.eval_incriminating is not None:
        session_manager.eval_incriminating = body.eval_incriminating
    if body.eval_effectiveness is not None:
        session_manager.eval_effectiveness = body.eval_effectiveness
    if body.eval_confidence is not None:
        session_manager.eval_confidence = body.eval_confidence
    if body.eval_realism is not None:
        session_manager.eval_realism = body.eval_realism
    if body.monitor_api_key_id is not None:
        session_manager.monitor_api_key_id = body.monitor_api_key_id
    if body.goal_score is not None:
        session_manager.goal_score = body.goal_score
    if body.tpm_default is not None:
        session_manager.tpm_default = body.tpm_default
        set_limit("default", body.tpm_default)
    if body.tpm_alt is not None:
        session_manager.tpm_alt = body.tpm_alt
        set_limit("alt", body.tpm_alt)
    if body.llm_model is not None:
        session_manager.llm_model = body.llm_model
    if body.llm_api_key_id is not None:
        session_manager.llm_api_key_id = body.llm_api_key_id
    if body.auto_eval_enabled is not None:
        session_manager.auto_eval_enabled = body.auto_eval_enabled
    if body.auto_eval_on_load is not None:
        session_manager.auto_eval_on_load = body.auto_eval_on_load

    # Persist to disk and broadcast to ALL sessions
    await asyncio.to_thread(session_manager._save_persisted_settings)
    event = {"type": "global_settings_updated", **session_manager.get_global_settings()}
    for s in session_manager._sessions.values():
        await session_manager.broadcast(s, event)

    return session_manager.get_global_settings()


# ── SSE stream ────────────────────────────────────────────────────────


@router.get("/{file_key:path}/events")
async def sse_events(file_key: str, request: Request):
    """SSE stream for a session. Sends all agent events as JSON."""
    subscriber_id, queue = await session_manager.subscribe(file_key)
    session = await session_manager.get_or_create(file_key)

    async def event_generator():
        try:
            # Send initial session state
            state = session_manager.get_session_state(session)
            yield f"data: {json.dumps(state, ensure_ascii=False)}\n\n"

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent proxy timeouts
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            await session_manager.unsubscribe(file_key, subscriber_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Mutation endpoints (frontend → backend as sole source of truth) ────


class UpdateMessagesBody(BaseModel):
    messages: list[dict]
    message_op: dict | None = None  # optional remap operation for mechanism/summary


@router.post("/{file_key:path}/messages", status_code=200)
async def update_messages(file_key: str, body: UpdateMessagesBody):
    """Replace all messages for a session (user edits from frontend)."""
    from agent import _fix_tool_ids

    session = await session_manager.get_or_create(file_key)
    session.messages = body.messages
    session.dirty = True

    # Fix tool IDs server-side (replaces frontend's debounced fixToolIds)
    fixed, fix_count = await _fix_tool_ids(session.messages)
    if fix_count > 0:
        session.messages = fixed

    # If a message_op was provided, remap mechanism/summary keys atomically
    metadata_changed = False
    if body.message_op and (session.mechanism or session.summary):
        from executor import remap_mechanism_keys

        if session.mechanism:
            session.mechanism = remap_mechanism_keys(session.mechanism, body.message_op)
        if session.summary:
            session.summary = remap_mechanism_keys(session.summary, body.message_op)
        metadata_changed = True

    session.transcript_hash = session.compute_hash()
    await _save_transcript_async(file_key, session.messages, session_manager.transcripts_dir)

    # If metadata was remapped, save it to disk and broadcast the update
    if metadata_changed:
        meta = session.get_metadata()
        await _save_metadata_async(file_key, meta, session_manager.transcripts_dir)
        await session_manager.broadcast(
            session,
            {
                "type": "metadata_updated",
                "outcome": session.outcome,
                "scenario": session.scenario,
                "mechanism": session.mechanism,
                "summary": session.summary,
                "transcript_hash": session.transcript_hash,
            },
        )

    await session_manager.broadcast(
        session,
        {
            "type": "messages_updated",
            "messages": session.messages,
            "message_count": len(session.messages),
            "transcript_hash": session.transcript_hash,
        },
    )
    return {"transcript_hash": session.transcript_hash, "ids_fixed": fix_count}


class UpdateMetadataBody(BaseModel):
    outcome: str | None = None
    scenario: str | None = None
    mechanism: dict | None = None
    summary: dict | None = None


@router.post("/{file_key:path}/metadata", status_code=200)
async def update_metadata(file_key: str, body: UpdateMetadataBody):
    """Update metadata fields for a session (user edits from frontend)."""
    session = await session_manager.get_or_create(file_key)
    if body.outcome is not None:
        session.outcome = body.outcome if body.outcome else None
    if body.scenario is not None:
        session.scenario = body.scenario if body.scenario else None
    if body.mechanism is not None:
        session.mechanism = body.mechanism if body.mechanism else None
    if body.summary is not None:
        session.summary = body.summary if body.summary else None
    session.transcript_hash = session.compute_hash()

    # Build metadata dict for disk save
    meta = session.get_metadata()
    # For empty outcome/scenario, explicitly write empty string to clear
    if body.outcome is not None and not body.outcome:
        meta["outcome"] = ""
    if body.scenario is not None and not body.scenario:
        meta["scenario"] = ""
    await _save_metadata_async(file_key, meta, session_manager.transcripts_dir)

    await session_manager.broadcast(
        session,
        {
            "type": "metadata_updated",
            "outcome": session.outcome,
            "scenario": session.scenario,
            "mechanism": session.mechanism,
            "summary": session.summary,
            "transcript_hash": session.transcript_hash,
        },
    )
    return {"transcript_hash": session.transcript_hash}


# ── HTTP commands ─────────────────────────────────────────────────────


class SubmitBody(BaseModel):
    prompt: str


@router.post("/{file_key:path}/submit", status_code=204)
async def submit_prompt(file_key: str, body: SubmitBody):
    """Submit a prompt to the agent."""
    session = await session_manager.get_or_create(file_key)

    prompt = body.prompt.strip()
    if not prompt:
        await session_manager.broadcast(
            session,
            {
                "type": "agent_error",
                "error": "Empty prompt",
            },
        )
        return

    # Reject if another agent is already running on this file.
    if session.agent_lock.locked():
        await session_manager.broadcast(
            session,
            {
                "type": "agent_error",
                "error": "An agent is already running on this file. Wait for it to finish or stop it first.",
            },
        )
        return

    # Clear last error on new submission
    session.last_error = None
    # Reset child lock — user sending a new message means they want the agent to continue editing
    session.child_locked = False

    # Run agent in background so the HTTP response returns immediately
    async def _run():
        async with session.agent_lock:
            await run_agent(session, session_manager, prompt)

    asyncio.create_task(_run())


@router.post("/{file_key:path}/stop", status_code=204)
async def stop_agent(file_key: str):
    """Stop a running agent."""
    session = session_manager.get_session(file_key)
    if not session:
        return

    if session.is_streaming:
        session.cancel_event.set()
    else:
        await session_manager.broadcast(
            session,
            {
                "type": "agent_error",
                "error": "No agent is currently running",
            },
        )


@router.post("/{file_key:path}/reset", status_code=204)
async def reset_session(file_key: str):
    """Clear chat history, content blocks, and stop any running agent."""
    session = await session_manager.get_or_create(file_key)

    # Stop any running agent first
    if session.is_streaming:
        session.cancel_event.set()
        # Give the agent a moment to notice the cancellation
        for _ in range(10):
            if not session.is_streaming:
                break
            await asyncio.sleep(0.2)
        # Force-clear if the agent didn't stop gracefully
        if session.is_streaming:
            logger.warning(f"Force-clearing is_streaming on {file_key} during reset")
            session.is_streaming = False
            session.cancel_event.clear()

    session.chat_history.clear()
    session.content_blocks.clear()
    session.hit_limit = False
    session.child_locked = False
    session.consistency_suppressions.clear()
    session.last_error = None
    _delete_chat(session.file_key)

    await session_manager.broadcast(session, session_manager.get_session_state(session))


@router.get("/{file_key:path}/state")
async def get_state(file_key: str):
    """Get current session state (for non-streaming clients)."""
    session = await session_manager.get_or_create(file_key)
    return session_manager.get_session_state(session)


class SettingsBody(BaseModel):
    """Per-session settings only."""

    model: str | None = None
    api_key_id: str | None = None
    mode: str | None = None
    soul_document: str | None = None
    prompt_mode: str | None = None  # "soul", "executor", "both"
    # Legacy: accept global settings here for backwards compatibility
    # (batch_orchestrator sends child_lock_enabled via per-session settings)
    child_lock_enabled: bool | None = None


@router.put("/{file_key:path}/settings")
async def update_settings(file_key: str, body: SettingsBody):
    """Update per-session settings."""
    session = await session_manager.get_or_create(file_key)

    if body.model is not None:
        session.model = body.model
    if body.api_key_id is not None:
        session.api_key_id = body.api_key_id
    if body.mode is not None:
        session.mode = body.mode
    if body.soul_document is not None:
        session.soul_document = body.soul_document
    if body.prompt_mode is not None:
        from sessions import VALID_PROMPT_MODES

        if body.prompt_mode not in VALID_PROMPT_MODES:
            raise ValueError(f"Invalid prompt_mode: {body.prompt_mode}, must be one of {VALID_PROMPT_MODES}")
        session.prompt_mode = body.prompt_mode
    # Legacy: allow child_lock_enabled via per-session endpoint for batch_orchestrator compat
    if body.child_lock_enabled is not None:
        session_manager.child_lock_enabled = body.child_lock_enabled

    event = {
        "type": "settings_updated",
        "model": session.model,
        "api_key_id": session.api_key_id,
        "mode": session.mode,
        "child_lock_enabled": session_manager.child_lock_enabled,
    }
    await session_manager.broadcast(session, event)
    return event
