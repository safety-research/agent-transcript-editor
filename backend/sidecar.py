"""
Sidecar metadata (.meta.json) I/O — single source of truth.

Every read/write of .meta.json files should go through this module.
"""

import json
from pathlib import Path
from typing import Any


def get_path(jsonl_path: Path) -> Path:
    """Get the .meta.json sidecar path for a .jsonl file."""
    return jsonl_path.with_suffix(".meta.json")


def load(jsonl_path: Path) -> dict[str, Any] | None:
    """Read sidecar metadata if it exists. Returns None if no sidecar."""
    path = get_path(jsonl_path)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(jsonl_path: Path, data: dict[str, Any]) -> None:
    """Merge data into existing sidecar (or create new).

    - Shallow-merges dict values so we don't wipe existing sub-keys
    - Invalidates scores/evals if transcript_hash changed (stale scores are worse than none)
    """
    existing = load(jsonl_path) or {}

    # If transcript changed, wipe stale evaluation data
    if (
        "transcript_hash" in data
        and "transcript_hash" in existing
        and data["transcript_hash"] != existing["transcript_hash"]
    ):
        existing.pop("scores", None)
        existing.pop("evals", None)

    merged = {**existing, **data}
    # Shallow-merge any dict values so we don't wipe existing keys
    for key in data:
        if isinstance(data[key], dict) and isinstance(existing.get(key), dict):
            merged[key] = {**existing[key], **data[key]}

    path = get_path(jsonl_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)


def read_variant_data(jsonl_path: Path, transcript_hash: str | None) -> dict[str, dict]:
    """Read existing suspiciousness variant data from meta.json for merging during partial reruns.

    Only returns data if the stored transcript_hash matches the current one,
    to avoid merging stale evals from a different version of the transcript.
    """
    meta = load(jsonl_path) or {}

    stored_hash = meta.get("transcript_hash")
    if transcript_hash is not None and stored_hash != transcript_hash:
        return {}  # Transcript changed — don't merge stale variant data

    return meta.get("evals", {}).get("suspiciousness", {}).get("variants", {})


def write_eval_scores(jsonl_path: Path, scores: dict, evals: dict, transcript_hash: str | None) -> None:
    """Write evaluation scores to the .meta.json sidecar file."""
    data: dict[str, Any] = {"scores": scores, "evals": evals}
    if transcript_hash is not None:
        data["transcript_hash"] = transcript_hash
    save(jsonl_path, data)


def read_eval_scores(jsonl_path: Path, transcript_hash: str) -> dict[str, Any] | None:
    """Read cached eval scores if transcript_hash matches.

    Returns {"scores": ..., "evals": ...} if hash matches, else None.
    """
    meta = load(jsonl_path)
    if not meta:
        return None

    if meta.get("transcript_hash") != transcript_hash:
        return None

    scores = meta.get("scores", {})
    evals = meta.get("evals", {})
    if not scores:
        return None

    return {"scores": scores, "evals": evals}
