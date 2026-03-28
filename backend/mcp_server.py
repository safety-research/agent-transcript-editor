"""
MCP server exposing transcript editor tools for use in Claude Code.

Wraps executor.py (pure functions) so Claude Code can directly read/edit
transcript JSONL files using the same tools the editor agents have.

Usage:
    python mcp_server.py                          # stdio mode (for Claude Code)
    python mcp_server.py --file path/to/file.jsonl  # pre-load a file

Configure in Claude Code settings:
    "mcpServers": {
        "transcript-editor": {
            "command": "python",
            "args": ["/path/to/mcp_server.py"]
        }
    }
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# Add backend to path so we can import executor
sys.path.insert(0, str(Path(__file__).parent))
from executor import ExecuteResult, execute_tool, is_write_tool

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("transcript-editor")

# ── State ─────────────────────────────────────────────────────────────────

_current_file: Path | None = None
_messages: list[dict[str, Any]] = []


def _load_file(path: Path) -> None:
    """Load a JSONL transcript file into memory."""
    global _current_file, _messages
    _current_file = path.resolve()
    _messages = []
    with open(_current_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                _messages.append(json.loads(line))
    logger.info(f"Loaded {len(_messages)} messages from {_current_file}")


def _save_file() -> None:
    """Save current messages back to the JSONL file."""
    if _current_file is None:
        raise RuntimeError("No file loaded")
    with open(_current_file, "w", encoding="utf-8") as f:
        for msg in _messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    logger.info(f"Saved {len(_messages)} messages to {_current_file}")


def _exec(name: str, inp: dict[str, Any]) -> str:
    """Execute a tool and handle state updates."""
    global _messages
    if _current_file is None:
        return "Error: No transcript file loaded. Use open_transcript first."

    result: ExecuteResult = execute_tool(name, inp, _messages)

    if not result.success:
        return f"Error: {result.error}"

    # Write tools return updated messages
    if is_write_tool(name) and result.messages is not None:
        _messages = result.messages
        _save_file()

    return result.result or "(no output)"


# ── File management tools ─────────────────────────────────────────────────


@mcp.tool()
def open_transcript(file_path: str) -> str:
    """Open a transcript JSONL file for editing. Must be called before using other tools.

    Args:
        file_path: Absolute path to the .jsonl transcript file
    """
    path = Path(file_path)
    if not path.exists():
        return f"Error: File not found: {file_path}"
    if not path.suffix == ".jsonl":
        return f"Error: Expected .jsonl file, got: {path.suffix}"
    _load_file(path)
    return f"Opened {path.name} — {len(_messages)} messages loaded"


@mcp.tool()
def current_file() -> str:
    """Show which transcript file is currently open."""
    if _current_file is None:
        return "No file open. Use open_transcript first."
    return f"{_current_file} — {len(_messages)} messages"


# ── Transcript read/write tools ───────────────────────────────────────────


@mcp.tool()
def get_messages(
    start: int | None = None,
    end: int | None = None,
    search: str | None = None,
    message_index: int | None = None,
    input_key: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """Get messages from the transcript. All indices are 1-indexed.

    Each message has a single content block (text, thinking, tool_use, or tool_result).

    LIST MODE (default): Returns truncated previews of messages in a range.
    DETAIL MODE (single): When message_index is provided, returns full content with character-level pagination.
    FIELD MODE: When message_index AND input_key are provided on a tool_use message, returns the plain string value.
    SEARCH: When search is provided, filters to messages containing the text.

    Args:
        start: Start index, 1-indexed (inclusive, default 1)
        end: End index, 1-indexed (inclusive, default last)
        search: Search text to filter messages
        message_index: Index of specific message to read (1-indexed)
        input_key: For tool_use messages: show plain string value of this input key
        offset: Character offset to start from (default 0)
        limit: Max characters to return
    """
    inp: dict[str, Any] = {}
    if start is not None:
        inp["start"] = start
    if end is not None:
        inp["end"] = end
    if search is not None:
        inp["search"] = search
    if message_index is not None:
        inp["message_index"] = message_index
    if input_key is not None:
        inp["input_key"] = input_key
    if offset is not None:
        inp["offset"] = offset
    if limit is not None:
        inp["limit"] = limit
    return _exec("get_messages", inp)


@mcp.tool()
def insert_message(index: int, role: str, content: dict[str, Any], cwd: str) -> str:
    """Insert a new message BEFORE the message at the given index. Index is 1-indexed.
    Use index = (total messages + 1) to append at the end.

    Args:
        index: Position to insert before (1-indexed)
        role: Message role ("user" or "assistant")
        content: A single content block, e.g. {"type": "text", "text": "..."}
        cwd: Working directory path for this message
    """
    return _exec("insert_message", {"index": index, "role": role, "content": content, "cwd": cwd})


@mcp.tool()
def update_message(index: int, content: dict[str, Any]) -> str:
    """Update the content of an existing message. Index is 1-indexed.

    Args:
        index: Message index to update (1-indexed)
        content: A single content block
    """
    return _exec("update_message", {"index": index, "content": content})


@mcp.tool()
def delete_messages(start: int, end: int) -> str:
    """Delete a contiguous range of messages. Both start and end are 1-indexed and inclusive.

    Args:
        start: Start index of range to delete (1-indexed, inclusive)
        end: End index of range to delete (1-indexed, inclusive)
    """
    return _exec("delete_messages", {"start": start, "end": end})


@mcp.tool()
def move_messages(from_start: int, from_end: int, to: int) -> str:
    """Move a contiguous range of messages to a new position. All indices are 1-indexed.

    Args:
        from_start: Start of range to move (1-indexed, inclusive)
        from_end: End of range to move (1-indexed, inclusive)
        to: Destination index (1-indexed). Messages inserted BEFORE this position.
    """
    return _exec("move_messages", {"from_start": from_start, "from_end": from_end, "to": to})


@mcp.tool()
def replace_messages(start: int, end: int, messages: list[dict[str, Any]]) -> str:
    """Replace a contiguous range of messages with a new set. Indices are 1-indexed and inclusive.

    Args:
        start: Start index of range to replace (1-indexed, inclusive)
        end: End index of range to replace (1-indexed, inclusive)
        messages: Array of new messages. Each has role, content (single block dict), and cwd.
    """
    return _exec("replace_messages", {"start": start, "end": end, "messages": messages})


@mcp.tool()
def find_replace(
    find: str,
    replace: str,
    regex: bool = False,
    raw_json: bool = False,
    input_key: str | None = None,
    message_index: int | None = None,
    start: int | None = None,
    end: int | None = None,
) -> str:
    """Find and replace text within message content blocks. Replaces all occurrences.

    For tool_use blocks, use input_key to target a specific string field — operates on
    the plain string value with no JSON escaping.

    Args:
        find: The exact string (or regex pattern if regex=true) to find
        replace: The string to replace it with
        regex: If true, treat find as a regex pattern
        raw_json: If true, operate on JSON representation of tool_use inputs
        input_key: For tool_use blocks: target only this string field
        message_index: Single message to search (1-indexed)
        start: Start of range (1-indexed, inclusive)
        end: End of range (1-indexed, inclusive)
    """
    inp: dict[str, Any] = {"find": find, "replace": replace}
    if regex:
        inp["regex"] = True
    if raw_json:
        inp["raw_json"] = True
    if input_key is not None:
        inp["input_key"] = input_key
    if message_index is not None:
        inp["message_index"] = message_index
    if start is not None:
        inp["start"] = start
    if end is not None:
        inp["end"] = end
    return _exec("find_replace", inp)


@mcp.tool()
def update_tool_content(
    message_index: int,
    content: str | None = None,
    source_message: int | None = None,
) -> str:
    """Update file content inside a Write tool_use message.

    Args:
        message_index: Message index containing the tool_use (1-indexed)
        content: New file content (omit if using source reference)
        source_message: Copy content from this message index (1-indexed)
    """
    inp: dict[str, Any] = {"message_index": message_index}
    if content is not None:
        inp["content"] = content
    if source_message is not None:
        inp["source_message"] = source_message
    return _exec("update_tool_content", inp)


@mcp.tool()
def count_tokens(
    target: str = "transcript",
    start: int | None = None,
    end: int | None = None,
) -> str:
    """Count tools-only tokens in the transcript or a selection of messages.

    Args:
        target: What to count — "transcript" or "selection"
        start: Start message index for selection (1-indexed, inclusive)
        end: End message index for selection (1-indexed, inclusive)
    """
    inp: dict[str, Any] = {"target": target}
    if start is not None:
        inp["start"] = start
    if end is not None:
        inp["end"] = end
    return _exec("count_tokens", inp)


# ── Metadata tools ────────────────────────────────────────────────────────


@mcp.tool()
def get_metadata() -> str:
    """Get the sidecar metadata (.meta.json) for the current transcript."""
    if _current_file is None:
        return "Error: No file open"
    import sidecar

    meta = sidecar.load(_current_file)
    if meta is None:
        return "No metadata file exists for this transcript."
    return json.dumps(meta, indent=2, ensure_ascii=False)


@mcp.tool()
def set_metadata(
    summary: dict[str, str] | None = None,
    outcome: str | None = None,
    scenario: str | None = None,
    mechanism: dict[str, str] | None = None,
) -> str:
    """Set transcript metadata (summary, outcome, scenario, mechanism).

    Args:
        summary: Dict of 1-indexed message numbers to step descriptions
        outcome: Description of what happened (passive voice, no actor)
        scenario: Deployment scenario description
        mechanism: Dict of 1-indexed message numbers to mechanism step descriptions
    """
    if _current_file is None:
        return "Error: No file open"
    import sidecar

    data: dict[str, Any] = {}
    if summary is not None:
        data["summary"] = summary
    if outcome is not None:
        data["outcome"] = outcome
    if scenario is not None:
        data["scenario"] = scenario
    if mechanism is not None:
        data["mechanism"] = mechanism

    sidecar.save(_current_file, data)
    return f"Metadata updated: {', '.join(data.keys())}"


@mcp.tool()
def check_consistency() -> str:
    """Check the current transcript for file-state inconsistencies."""
    if _current_file is None:
        return "Error: No file open"

    from vendor.check_consistency import check_consistency as _check

    warnings = _check(_messages)
    if not warnings:
        return "No inconsistencies found."
    return json.dumps(warnings, indent=2, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Transcript editor MCP server")
    parser.add_argument("--file", "-f", help="Pre-load a transcript file")
    # Parse known args only — MCP/FastMCP may add its own
    args, _ = parser.parse_known_args()

    if args.file:
        _load_file(Path(args.file))

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
