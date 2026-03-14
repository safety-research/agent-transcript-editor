"""
Files Router - Transcript file management.

Provides endpoints for:
- Listing available transcript files (project-scoped)
- Loading transcript content
- Saving transcripts (project-scoped)
- Uploading new transcripts (project-scoped)
- Project CRUD (subdirectories on disk)
- Sidecar metadata (.meta.json) for outcome, mechanism, scores
"""

import asyncio
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import APIRouter, HTTPException, Query, UploadFile
from pydantic import BaseModel

import sidecar

router = APIRouter()


def get_transcripts_dir() -> Path:
    """Get the transcripts directory from environment or default."""
    return Path(os.getenv("TRANSCRIPTS_DIR", "./transcripts")).resolve()


def sanitize_dirname(name: str) -> str:
    """Sanitize a project name into a safe directory name."""
    # Replace spaces with hyphens, strip unsafe chars
    safe = re.sub(r"[^\w\s-]", "", name.strip()).strip()
    safe = re.sub(r"[\s]+", "-", safe).lower()
    if not safe:
        raise ValueError("Name results in empty dirname")
    return safe


def _check_within_transcripts(path: Path, transcripts_dir: Path) -> None:
    """Security: ensure resolved path is within transcripts directory."""
    if not str(path).startswith(str(transcripts_dir)):
        raise HTTPException(status_code=403, detail="Access denied")


def _safe_jsonl_name(name: str) -> str:
    """Sanitize a filename: strip path separators and ensure .jsonl extension."""
    safe = Path(name).name
    if not safe.endswith(".jsonl"):
        safe += ".jsonl"
    return safe


# ── Models ──────────────────────────────────────────────────────────────


class TranscriptInfo(BaseModel):
    """Metadata about a transcript file."""

    name: str
    path: str
    size: int
    modified: str
    message_count: int | None = None
    scores: dict[str, float] | None = None
    has_metadata: bool = False
    agent_running: bool = False
    eval_status: str | None = None  # "running", "done", "error", or None


class TranscriptContent(BaseModel):
    """Full transcript with content."""

    name: str
    messages: list[dict[str, Any]]
    metadata: dict[str, Any] | None = None
    auto_fixed: dict[str, Any] | None = None  # Info about auto-fixes applied on load


class SaveRequest(BaseModel):
    """Request to save a transcript."""

    name: str
    messages: list[dict[str, Any]]
    project: str | None = None


class MetaRequest(BaseModel):
    """Request to write/merge sidecar metadata."""

    project: str
    file_name: str
    outcome: str | None = None
    scenario: str | None = None
    mechanism: dict[str, str] | None = None
    summary: dict[str, str] | None = None
    scores: dict[str, float] | None = None
    evals: dict[str, Any] | None = None  # Full eval results per metric
    transcript_hash: str | None = None
    consistency_suppressions: list[dict[str, str]] | None = None


class RenameFileRequest(BaseModel):
    """Request to rename a transcript file."""

    project: str
    old_name: str
    new_name: str


class DuplicateFileRequest(BaseModel):
    """Request to duplicate a transcript file (and sidecar, minus chat)."""

    project: str
    file_name: str


class MoveFileRequest(BaseModel):
    """Request to move a transcript between projects."""

    source_project: str
    target_project: str
    file_name: str


class ProjectInfo(BaseModel):
    """Metadata about a project directory."""

    name: str
    dir_name: str
    file_count: int
    modified: str | None = None


class CreateProjectRequest(BaseModel):
    """Request to create a project."""

    name: str


class RenameProjectRequest(BaseModel):
    """Request to rename a project."""

    old_name: str
    new_name: str


# ── Project Endpoints ───────────────────────────────────────────────────


@router.get("/projects", response_model=list[ProjectInfo])
async def list_projects():
    """List all project directories."""
    transcripts_dir = get_transcripts_dir()
    # Run all sync file I/O in a thread to avoid blocking the event loop
    return await asyncio.to_thread(_list_projects_sync, transcripts_dir)


def _list_projects_sync(transcripts_dir: Path) -> list[ProjectInfo]:
    """Synchronous project listing — runs in a thread pool."""
    projects: list[ProjectInfo] = []

    if not transcripts_dir.exists():
        return projects

    for entry in sorted(transcripts_dir.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
            jsonl_files = list(entry.glob("*.jsonl"))
            file_count = len(jsonl_files)
            # Most recent modification time across all files in project
            latest_mtime: float | None = None
            for f in jsonl_files:
                try:
                    mtime = f.stat().st_mtime
                    if latest_mtime is None or mtime > latest_mtime:
                        latest_mtime = mtime
                except OSError:
                    continue
            # Fall back to directory mtime if no files
            if latest_mtime is None:
                latest_mtime = entry.stat().st_mtime
            projects.append(
                ProjectInfo(
                    name=entry.name,
                    dir_name=entry.name,
                    file_count=file_count,
                    modified=datetime.fromtimestamp(latest_mtime).isoformat(),
                )
            )

    return projects


@router.post("/projects")
async def create_project(request: CreateProjectRequest):
    """Create a new project directory."""
    transcripts_dir = get_transcripts_dir()
    dir_name = sanitize_dirname(request.name)
    project_dir = transcripts_dir / dir_name

    if project_dir.exists():
        raise HTTPException(status_code=409, detail=f"Project '{dir_name}' already exists")

    project_dir.mkdir(parents=True)
    return {"success": True, "dir_name": dir_name}


@router.post("/projects/rename")
async def rename_project(request: RenameProjectRequest):
    """Rename a project directory."""
    transcripts_dir = get_transcripts_dir()
    old_dir = transcripts_dir / request.old_name
    new_dir_name = sanitize_dirname(request.new_name)
    new_dir = transcripts_dir / new_dir_name

    if not old_dir.exists():
        raise HTTPException(status_code=404, detail=f"Project '{request.old_name}' not found")
    if new_dir.exists() and new_dir != old_dir:
        raise HTTPException(status_code=409, detail=f"Project '{new_dir_name}' already exists")

    if old_dir != new_dir:
        old_dir.rename(new_dir)

    return {"success": True, "dir_name": new_dir_name}


@router.delete("/projects/{name}")
async def delete_project(name: str, delete_files: bool = Query(default=False)):
    """Delete a project directory."""
    transcripts_dir = get_transcripts_dir()
    project_dir = (transcripts_dir / name).resolve()

    _check_within_transcripts(project_dir, transcripts_dir)

    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project not found")

    if name == "default":
        raise HTTPException(status_code=400, detail="Cannot delete default project")

    if delete_files:
        shutil.rmtree(project_dir)
    else:
        # Only delete if empty
        remaining = list(project_dir.iterdir())
        if remaining:
            raise HTTPException(
                status_code=400,
                detail=f"Project has {len(remaining)} files. Use delete_files=true to force.",
            )
        project_dir.rmdir()

    return {"success": True}


# ── File Endpoints ──────────────────────────────────────────────────────


@router.get("/list", response_model=list[TranscriptInfo])
async def list_transcripts(project: str | None = Query(default=None)):
    """
    List transcript files, optionally scoped to a project subdirectory.

    When project param is given, lists files in transcripts_dir/<project>/.
    When absent, lists root-level files only.
    """
    transcripts_dir = get_transcripts_dir()

    if project:
        target_dir = (transcripts_dir / project).resolve()
        _check_within_transcripts(target_dir, transcripts_dir)
        if not target_dir.exists():
            return []
    else:
        target_dir = transcripts_dir
        if not target_dir.exists():
            return []

    # Run all sync file I/O in a thread to avoid blocking the event loop
    transcripts = await asyncio.to_thread(_list_transcripts_sync, target_dir, transcripts_dir)

    # Enrich with live session status (agent running, eval status)
    from sessions import session_manager

    for t in transcripts:
        session = session_manager.get_session(t.path)
        if session:
            t.agent_running = session.is_streaming
            if session.monitor_eval_status != "idle":
                t.eval_status = session.monitor_eval_status

    return transcripts


def _list_transcripts_sync(target_dir: Path, transcripts_dir: Path) -> list[TranscriptInfo]:
    """Synchronous file listing — runs in a thread pool."""
    transcripts: list[TranscriptInfo] = []

    import logging

    logger = logging.getLogger(__name__)
    for file_path in target_dir.glob("*.jsonl"):
        try:
            stat = file_path.stat()
            relative_path = file_path.relative_to(transcripts_dir)

            # Count messages (binary newline count — fast even for large files)
            message_count = None
            try:
                message_count = file_path.read_bytes().count(b"\n")
            except Exception as e:
                logger.warning(f"Failed to count messages in {file_path.name}: {e}")

            # Read sidecar metadata for scores
            sidecar_data = sidecar.load(file_path)
            scores = sidecar_data.get("scores") if sidecar_data else None
            has_metadata = sidecar_data is not None

            transcripts.append(
                TranscriptInfo(
                    name=file_path.name,
                    path=str(relative_path),
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    message_count=message_count,
                    scores=scores,
                    has_metadata=has_metadata,
                )
            )
        except Exception as e:
            logger.warning(f"Failed to list transcript {file_path.name}: {e}")
            continue

    # Sort by modified time, newest first
    transcripts.sort(key=lambda t: t.modified, reverse=True)
    return transcripts


@router.get("/load/{path:path}", response_model=TranscriptContent)
async def load_transcript(path: str):
    """
    Load a transcript file by path.

    Goes through the session manager so there's a single source of truth
    for messages (the session cache), avoiding stale-data bugs.
    """
    from sessions import session_manager

    transcripts_dir = get_transcripts_dir()
    file_path = (transcripts_dir / path).resolve()

    _check_within_transcripts(file_path, transcripts_dir)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Transcript not found")

    if not file_path.suffix == ".jsonl":
        raise HTTPException(status_code=400, detail="Not a JSONL file")

    try:
        session = await session_manager.get_or_create(path)
        auto_fixed = session.auto_fixed if session.auto_fixed else None
        # Clear after first read so toast only shows once
        session.auto_fixed = {}
        return TranscriptContent(
            name=file_path.name,
            messages=session.messages,
            metadata=session.sidecar_metadata if session.sidecar_metadata else None,
            auto_fixed=auto_fixed,
        )
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reload/{path:path}")
async def reload_transcript(path: str):
    """
    Reload a transcript from disk, discarding the in-memory session cache.

    Use this after editing the .jsonl or .meta.json on disk while the file
    is open in the editor. Refuses if an agent is currently streaming.
    """
    from sessions import session_manager

    session = session_manager.get_session(path)
    if not session:
        return {"success": True, "reloaded": False, "reason": "no active session"}

    try:
        new_session = await session_manager.reload_from_disk(path)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {
        "success": True,
        "reloaded": True,
        "message_count": len(new_session.messages) if new_session else 0,
    }


class CreateFileRequest(BaseModel):
    project: str
    name: str


@router.post("/create")
async def create_file(request: CreateFileRequest):
    """Create an empty transcript file on disk."""
    transcripts_dir = get_transcripts_dir()
    name = _safe_jsonl_name(request.name)
    target_dir = (transcripts_dir / request.project).resolve()
    _check_within_transcripts(target_dir, transcripts_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / name

    if file_path.exists():
        raise HTTPException(status_code=409, detail=f"File already exists: {name}")

    file_path.touch()
    return {"name": name, "path": str(file_path.relative_to(transcripts_dir))}


@router.post("/save")
async def save_transcript(request: SaveRequest):
    """
    Save a transcript to the transcripts directory.

    If project is set, saves to transcripts_dir/<project>/.
    If the file exists, it will be overwritten.
    """
    transcripts_dir = get_transcripts_dir()

    name = _safe_jsonl_name(request.name)

    if request.project:
        target_dir = (transcripts_dir / request.project).resolve()
        _check_within_transcripts(target_dir, transcripts_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / name
    else:
        file_path = transcripts_dir / name

    # Validate each message is a well-formed transcript entry
    for i, msg in enumerate(request.messages):
        if "role" not in msg:
            raise HTTPException(status_code=422, detail=f"Message {i}: missing 'role'")
        if msg["role"] not in ("user", "assistant"):
            raise HTTPException(status_code=422, detail=f"Message {i}: invalid role '{msg['role']}'")
        if "content" not in msg:
            raise HTTPException(status_code=422, detail=f"Message {i}: missing 'content'")
        # Verify each message round-trips through JSON
        try:
            json.loads(json.dumps(msg))
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=422, detail=f"Message {i}: not JSON-serializable: {e}")

    try:
        content = "\n".join(json.dumps(msg) for msg in request.messages)
        async with aiofiles.open(file_path, mode="w", encoding="utf-8") as f:
            await f.write(content)

        return {"success": True, "path": str(file_path.relative_to(transcripts_dir))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload")
async def upload_transcript(file: UploadFile, project: str | None = Query(default=None)):
    """
    Upload a transcript file.

    Validates the file is valid JSONL before saving.
    When project param is given, saves to transcripts_dir/<project>/.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    if not file.filename.endswith(".jsonl"):
        raise HTTPException(status_code=400, detail="File must be .jsonl")

    transcripts_dir = get_transcripts_dir()
    safe_name = Path(file.filename).name

    if project:
        target_dir = (transcripts_dir / project).resolve()
        _check_within_transcripts(target_dir, transcripts_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / safe_name
    else:
        file_path = transcripts_dir / safe_name

    try:
        content = await file.read()
        text = content.decode("utf-8")

        # Validate JSONL format
        messages: list[dict[str, Any]] = []
        for i, line in enumerate(text.strip().split("\n")):
            if line.strip():
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid JSON on line {i + 1}: {e}")

        # Auto-fix invalid tool IDs before saving (skip minimization — upload is already parsed)
        from sessions import auto_fix_messages

        messages, auto_fixed = auto_fix_messages(messages, minimize=False)

        # Save the file (with any fixes applied)
        async with aiofiles.open(file_path, mode="w", encoding="utf-8") as f:
            for msg in messages:
                await f.write(json.dumps(msg) + "\n")

        return {
            "success": True,
            "name": safe_name,
            "path": str(file_path.relative_to(transcripts_dir)),
            "message_count": len(messages),
            "auto_fixed": auto_fixed if auto_fixed else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/delete/{path:path}")
async def delete_transcript(path: str):
    """
    Delete a transcript file.

    The path is relative to the transcripts directory.
    Destroys any active backend session (cancels running agents/monitors)
    so they don't re-create the file after deletion.
    """
    from sessions import _delete_chat, session_manager

    transcripts_dir = get_transcripts_dir()
    file_path = (transcripts_dir / path).resolve()

    _check_within_transcripts(file_path, transcripts_dir)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Transcript not found")

    try:
        # Destroy backend session first (cancels agents, prevents re-save)
        await session_manager.destroy_session(path)

        file_path.unlink()
        # Also delete sidecar and chat files
        meta_path = sidecar.get_path(file_path)
        if meta_path.exists():
            meta_path.unlink()
        _delete_chat(path, transcripts_dir)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Sidecar Metadata Endpoints ────────────────────────────────────────


@router.post("/meta")
async def write_meta(request: MetaRequest):
    """Write/merge sidecar metadata for a transcript."""
    transcripts_dir = get_transcripts_dir()
    target_dir = (transcripts_dir / request.project).resolve()

    _check_within_transcripts(target_dir, transcripts_dir)

    file_name = _safe_jsonl_name(request.file_name)
    file_path = target_dir / file_name
    # Allow writing metadata even if jsonl doesn't exist yet (pre-save)

    data: dict[str, Any] = {}
    if request.outcome is not None:
        data["outcome"] = request.outcome
    if request.scenario is not None:
        data["scenario"] = request.scenario
    if request.mechanism is not None:
        data["mechanism"] = request.mechanism
    if request.summary is not None:
        data["summary"] = request.summary
    if request.scores is not None:
        data["scores"] = request.scores
    if request.evals is not None:
        data["evals"] = request.evals
    if request.transcript_hash is not None:
        data["transcript_hash"] = request.transcript_hash
    if request.consistency_suppressions is not None:
        data["consistency_suppressions"] = request.consistency_suppressions

    if not data:
        return {"success": True}

    try:
        await asyncio.to_thread(sidecar.save, file_path, data)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rename")
async def rename_file(request: RenameFileRequest):
    """Rename a transcript file and its sidecar atomically."""
    transcripts_dir = get_transcripts_dir()
    project_dir = (transcripts_dir / request.project).resolve()

    _check_within_transcripts(project_dir, transcripts_dir)

    old_name = Path(request.old_name).name
    new_name = _safe_jsonl_name(request.new_name)

    old_path = project_dir / old_name
    new_path = project_dir / new_name

    if not old_path.exists():
        raise HTTPException(status_code=404, detail=f"File '{old_name}' not found")
    if new_path.exists() and new_path != old_path:
        raise HTTPException(status_code=409, detail=f"File '{new_name}' already exists")

    try:
        old_path.rename(new_path)
        # Rename sidecar too
        old_sidecar = sidecar.get_path(old_path)
        if old_sidecar.exists():
            new_sidecar = sidecar.get_path(new_path)
            old_sidecar.rename(new_sidecar)

        # Rekey session if one exists
        from sessions import session_manager

        old_file_key = f"{request.project}/{old_name}"
        new_file_key = f"{request.project}/{new_name}"
        session = session_manager.get_session(old_file_key)
        if session:
            session.file_key = new_file_key
            await session_manager.rekey_session(old_file_key, new_file_key)

        return {"success": True, "new_name": new_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/move")
async def move_file(request: MoveFileRequest):
    """Move a transcript file (and sidecar) between projects."""
    transcripts_dir = get_transcripts_dir()
    source_dir = (transcripts_dir / request.source_project).resolve()
    target_dir = (transcripts_dir / request.target_project).resolve()

    _check_within_transcripts(source_dir, transcripts_dir)
    _check_within_transcripts(target_dir, transcripts_dir)

    file_name = Path(request.file_name).name
    source_path = source_dir / file_name
    target_path = target_dir / file_name

    if not source_path.exists():
        raise HTTPException(status_code=404, detail=f"File '{file_name}' not found in source project")
    if target_path.exists():
        raise HTTPException(status_code=409, detail=f"File '{file_name}' already exists in target project")

    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        source_path.rename(target_path)
        # Move sidecar too
        source_sidecar = sidecar.get_path(source_path)
        if source_sidecar.exists():
            target_sidecar = sidecar.get_path(target_path)
            source_sidecar.rename(target_sidecar)

        # Rekey session if one exists
        from sessions import session_manager

        old_file_key = f"{request.source_project}/{file_name}"
        new_file_key = f"{request.target_project}/{file_name}"
        session = session_manager.get_session(old_file_key)
        if session:
            session.file_key = new_file_key
            await session_manager.rekey_session(old_file_key, new_file_key)

        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/duplicate")
async def duplicate_file(request: DuplicateFileRequest):
    """Duplicate a transcript file and its sidecar metadata (excluding chat)."""
    transcripts_dir = get_transcripts_dir()
    project_dir = (transcripts_dir / request.project).resolve()

    _check_within_transcripts(project_dir, transcripts_dir)

    file_name = Path(request.file_name).name
    source_path = project_dir / file_name

    if not source_path.exists():
        raise HTTPException(status_code=404, detail=f"File '{file_name}' not found")

    # Generate copy name
    base = file_name.removesuffix(".jsonl")
    copy_name = f"{base}-copy.jsonl"
    # If -copy already exists, append a number
    counter = 2
    while (project_dir / copy_name).exists():
        copy_name = f"{base}-copy-{counter}.jsonl"
        counter += 1

    dest_path = project_dir / copy_name

    try:
        # Copy the jsonl file
        shutil.copy2(source_path, dest_path)

        # Copy sidecar, stripping chat
        sidecar_data = sidecar.load(source_path)
        if sidecar_data:
            cleaned = {k: v for k, v in sidecar_data.items() if k != "chat"}
            sidecar_path = sidecar.get_path(dest_path)
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, indent=2)

        return {"success": True, "new_name": copy_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Prompt Files ──────────────────────────────────────────────────────
