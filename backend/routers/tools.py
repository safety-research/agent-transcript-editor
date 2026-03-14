"""
Tools Router - Tool-ID fixing, verification, transcript minimization, and consistency checking.

Uses sibling tools:
- tool-id-fixer: Fix unrealistic tool IDs in transcripts
- transcript-minimizer: Convert full Claude Code transcripts to minimal format
- file-consistency-checker: Check transcripts for file-state inconsistencies
"""

import json
from pathlib import Path
from typing import Any

# Add sibling tools to path for direct import
import tool_imports  # noqa: F401
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class FixIdsRequest(BaseModel):
    """Request to fix tool IDs in a transcript."""

    messages: list[dict[str, Any]]
    verify_only: bool = False


class FixIdsResponse(BaseModel):
    messages: list[dict[str, Any]]
    ids_fixed: int
    id_map: dict[str, str]


class VerifyResponse(BaseModel):
    valid: bool
    issues: list[dict[str, str]]


class MinimizeRequest(BaseModel):
    """Request to minimize a transcript (full Claude Code → minimal format)."""

    messages: list[dict[str, Any]]


class MinimizeResponse(BaseModel):
    messages: list[dict[str, Any]]
    original_count: int
    minimized_count: int
    was_full_format: bool


@router.post("/fix-ids")
async def fix_tool_ids(request: FixIdsRequest):
    """
    Fix unrealistic tool IDs in a transcript.

    Converts descriptive English IDs to realistic toolu_01 + 22 base58 format.
    Maintains consistency between tool_use and tool_result pairs.

    If verify_only=True, just returns validation results without fixing.
    """
    try:
        from fix_ids import fix_transcript
        from fix_ids import verify_tool_ids as _verify
    except ImportError as e:
        return {"error": f"tool-id-fixer not available: {e}"}

    if request.verify_only:
        # Write messages to temp lines for verification
        import tempfile

        lines = [json.dumps(m) for m in request.messages]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            f.flush()
            issues = _verify(Path(f.name))

        issue_dicts = [
            {
                "line_number": str(issue.line_number),
                "block_type": issue.block_type,
                "id_value": issue.id_value,
                "issue": issue.issue,
            }
            for issue in issues
        ]

        return VerifyResponse(valid=len(issues) == 0, issues=issue_dicts)

    # Fix mode: convert messages to JSONL lines, run fixer, return fixed messages
    input_lines = [json.dumps(m) for m in request.messages]
    fixed_lines, id_map, _positional_fixes = fix_transcript(input_lines)
    fixed_messages = [json.loads(line) for line in fixed_lines if line.strip()]

    return FixIdsResponse(
        messages=fixed_messages,
        ids_fixed=len(id_map),
        id_map=id_map,
    )


@router.post("/minimize")
async def minimize_transcript(request: MinimizeRequest):
    """
    Minimize a transcript from full Claude Code format to minimal format.

    Auto-detects whether the input is already minimal or needs conversion.
    Strips metadata, normalizes content blocks, preserves cwd at message level.
    """
    try:
        from minimize import minimize_transcript as _minimize
    except ImportError as e:
        return {"error": f"transcript-minimizer not available: {e}"}

    original_count = len(request.messages)

    # Detect if this is full format by checking for wrapper/metadata fields
    is_full = any(
        ("type" in m and "message" in m)
        or "uuid" in m
        or "parentUuid" in m
        or "sessionId" in m
        or m.get("type") in ("summary", "file-history-snapshot", "queue-operation")
        for m in request.messages[:10]
    )

    # Convert messages to JSONL lines and run through minimizer
    input_lines = [json.dumps(m) for m in request.messages]
    minimized = _minimize(input_lines, keep_thinking=True, preserve_cwd=True)

    return MinimizeResponse(
        messages=minimized,
        original_count=original_count,
        minimized_count=len(minimized),
        was_full_format=is_full,
    )


class CheckConsistencyRequest(BaseModel):
    """Request to check file-state consistency in a transcript."""

    messages: list[dict[str, Any]]
    suppressions: list[str] = []  # warning IDs to suppress
    expect_system_reminders: bool = True  # strip <system-reminder> blocks (True for CC transcripts)


class CheckConsistencyResponse(BaseModel):
    warnings: list[dict[str, Any]]  # unsuppressed warnings
    files_tracked: int
    files_known: int
    files_fragments: int
    files_partial: int


@router.post("/check-consistency")
async def check_consistency_endpoint(request: CheckConsistencyRequest):
    """
    Check a transcript for file-state inconsistencies.

    Tracks virtual filesystem state as messages are processed sequentially,
    detecting mismatches between Edit/Write and subsequent Read results.
    """
    try:
        from check_consistency import check_consistency
    except ImportError as e:
        return {"error": f"file-consistency-checker not available: {e}"}

    result = check_consistency(
        request.messages,
        suppressions=request.suppressions,
        expect_system_reminders=request.expect_system_reminders,
    )

    return CheckConsistencyResponse(
        warnings=[
            {
                "id": w.id,
                "type": w.type,
                "file_path": w.file_path,
                "message_index": w.message_index,
                "message": w.message,
                "expected": w.expected,
                "actual": w.actual,
                "diff": w.diff,
            }
            for w in result.warnings
        ],
        files_tracked=result.files_tracked,
        files_known=result.files_known,
        files_fragments=result.files_fragments,
        files_partial=result.files_partial,
    )
