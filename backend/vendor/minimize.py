#!/usr/bin/env python3
"""
Converts full Claude Code transcripts to minimal JSONL format.

The minimal format strips all metadata and keeps only the essential
message structure needed for conversation replay.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Pattern to match <system-reminder>...</system-reminder> blocks injected by Claude Code.
# These are environment artifacts (e.g. malware analysis warnings) not part of the conversation.
SYSTEM_REMINDER_PATTERN = re.compile(r"<system-reminder>.*?</system-reminder>\n?", re.DOTALL)

# Known content block types and their expected attributes.
# If a new block type or attribute appears, we want to error so we can decide how to handle it.
KNOWN_BLOCK_TYPES = {
    "text",
    "thinking",
    "tool_use",
    "tool_result",
}

# Attributes we preserve per block type (these are the meaningful content fields).
PRESERVED_ATTRIBUTES = {
    "text": {"type", "text"},
    "thinking": {"type", "thinking"},
    "tool_use": {"type", "id", "name", "input"},
    "tool_result": {"type", "tool_use_id", "content", "is_error"},
}

# Attributes we've seen and intentionally drop (metadata, not content).
DROPPED_ATTRIBUTES = {
    "thinking": {"signature"},  # cryptographic integrity check, not content
}

# Union of preserved + dropped = all known attributes.
KNOWN_BLOCK_ATTRIBUTES = {
    block_type: PRESERVED_ATTRIBUTES.get(block_type, set()) | DROPPED_ATTRIBUTES.get(block_type, set())
    for block_type in KNOWN_BLOCK_TYPES
}

# Known tool names in Claude Code transcripts.
# Update this set when Claude Code adds new tools.
KNOWN_TOOL_NAMES = {
    # Core file tools
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    # Search tools
    "Glob",
    "Grep",
    # Execution
    "Bash",
    # Web tools
    "WebFetch",
    "WebSearch",
    # Agent/task tools
    "Task",
    "TodoRead",
    "TodoWrite",
    # Interaction tools
    "AskFollowupQuestion",
    "AskUserQuestion",
    # Mode tools
    "EnterPlanMode",
    "ExitPlanMode",
    # MCP / other
    "Skill",
    "ToolSearch",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
    # Team tools
    "TeamCreate",
    "TeamDelete",
    "SendMessage",
    "TaskCreate",
    "TaskGet",
    "TaskUpdate",
    "TaskList",
    "TaskOutput",
    "TaskStop",
}


def strip_system_reminders(text: str) -> str:
    """Remove <system-reminder>...</system-reminder> blocks from text."""
    return SYSTEM_REMINDER_PATTERN.sub("", text)


class UnknownBlockTypeError(ValueError):
    """Raised when an unrecognized content block type is encountered."""

    pass


class UnknownToolNameError(ValueError):
    """Raised when an unrecognized tool name is encountered."""

    pass


class UnknownAttributeError(ValueError):
    """Raised when a content block has an unrecognized attribute."""

    pass


def extract_minimal_blocks(content: Any, strict: bool = True) -> list[dict]:
    """Extract minimal content blocks from message content.

    Returns a list of individual block dicts (one per block in the source).

    Args:
        content: Raw message content (string, list, or other).
        strict: If True, error on unknown block types or tool names.
    """
    # Normalize string content to single block
    if isinstance(content, str):
        return [{"type": "text", "text": content}]

    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    minimal_blocks = []
    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")

        if strict and block_type not in KNOWN_BLOCK_TYPES:
            raise UnknownBlockTypeError(
                f"Unknown content block type: {block_type!r}. "
                f"Known types: {sorted(KNOWN_BLOCK_TYPES)}. "
                f"Add it to KNOWN_BLOCK_TYPES and handle it in extract_minimal_blocks(), "
                f"or pass --no-strict to skip validation."
            )

        if strict and block_type in KNOWN_BLOCK_ATTRIBUTES:
            extra_attrs = set(block.keys()) - KNOWN_BLOCK_ATTRIBUTES[block_type]
            if extra_attrs:
                raise UnknownAttributeError(
                    f"Unknown attribute(s) {extra_attrs} on {block_type!r} block. "
                    f"Known attributes for {block_type!r}: {sorted(KNOWN_BLOCK_ATTRIBUTES[block_type])}. "
                    f"Add to KNOWN_BLOCK_ATTRIBUTES and preserve in extract_minimal_blocks(), "
                    f"or pass --no-strict to skip validation."
                )

        if block_type == "text":
            minimal_blocks.append({"type": "text", "text": block.get("text", "")})
        elif block_type == "thinking":
            minimal_blocks.append({"type": "thinking", "thinking": block.get("thinking", "")})
        elif block_type == "tool_use":
            tool_name = block.get("name", "")
            # Allow MCP tools (mcp__*) through without validation
            if strict and tool_name and not tool_name.startswith("mcp__") and tool_name not in KNOWN_TOOL_NAMES:
                raise UnknownToolNameError(
                    f"Unknown tool name: {tool_name!r}. "
                    f"Add it to KNOWN_TOOL_NAMES in minimize.py, "
                    f"or pass --no-strict to skip validation."
                )
            minimal_blocks.append(
                {
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": tool_name,
                    "input": block.get("input", {}),
                }
            )
        elif block_type == "tool_result":
            raw_content = block.get("content", "")
            # Strip system-reminder injections from tool result content
            if isinstance(raw_content, str):
                raw_content = strip_system_reminders(raw_content)
            elif isinstance(raw_content, list):
                for item in raw_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        item["text"] = strip_system_reminders(item["text"])
            result = {
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "content": raw_content,
            }
            if block.get("is_error"):
                result["is_error"] = True
            minimal_blocks.append(result)

    return minimal_blocks


def convert_entry(entry: dict, preserve_cwd: bool = True, strict: bool = True) -> list[dict]:
    """Convert a transcript entry to minimal format entries (one per block).

    Handles both full format (with type/message wrapper) and
    already-minimal format (just role/content).

    Returns a list of entries, one per content block. Each entry has
    content as a single dict (not a list).
    """
    role = None
    content = None
    cwd = None

    # Check if already in minimal format (has role directly)
    if "role" in entry and "content" in entry and "type" not in entry:
        role = entry.get("role")
        content = entry.get("content")
        cwd = entry.get("cwd")
    elif "type" in entry:
        # Full format with type/message wrapper
        entry_type = entry.get("type")
        if entry_type not in ("user", "assistant"):
            return []
        role = entry_type
        message = entry.get("message", {})
        content = message.get("content")
        cwd = entry.get("cwd")

    if role not in ("user", "assistant") or content is None:
        return []

    # If content is already a single dict (new format), just validate and return
    if isinstance(content, dict):
        result = {"role": role, "content": content}
        if preserve_cwd and cwd:
            result["cwd"] = cwd
        return [result]

    # Extract blocks from old list/string format
    blocks = extract_minimal_blocks(content, strict=strict)

    # Return one entry per block
    entries = []
    for block in blocks:
        result = {"role": role, "content": block}
        if preserve_cwd and cwd:
            result["cwd"] = cwd
        entries.append(result)
    return entries


def minimize_transcript(
    input_lines: list[str],
    keep_thinking: bool = True,
    preserve_cwd: bool = True,
    strict: bool = True,
) -> list[dict]:
    """Convert full transcript lines to minimal messages.

    Args:
        input_lines: Raw JSONL lines from transcript.
        keep_thinking: If False, strip thinking blocks.
        preserve_cwd: If True, keep cwd fields.
        strict: If True, error on unknown block types or tool names.
    """
    messages = []

    for i, line in enumerate(input_lines):
        line = line.strip()
        if not line:
            continue

        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        try:
            entries = convert_entry(entry, preserve_cwd=preserve_cwd, strict=strict)
        except (UnknownBlockTypeError, UnknownToolNameError, UnknownAttributeError) as e:
            raise type(e)(f"Line {i + 1}: {e}") from None

        for minimal in entries:
            # Optionally filter out thinking blocks
            if not keep_thinking and minimal["content"].get("type") == "thinking":
                continue
            messages.append(minimal)

    return messages


def main():
    parser = argparse.ArgumentParser(
        description="Convert Claude Code transcripts to minimal JSONL format",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s transcript.jsonl                    # Output to stdout
  %(prog)s transcript.jsonl -o minimal.jsonl   # Output to file
  %(prog)s transcript.jsonl --no-thinking      # Strip thinking blocks
  cat transcript.jsonl | %(prog)s -            # Read from stdin
        """,
    )
    parser.add_argument("input", help="Input transcript file (use '-' for stdin)")
    parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    parser.add_argument("--no-thinking", action="store_true", help="Remove thinking blocks from output")
    parser.add_argument("--no-cwd", action="store_true", help="Remove cwd (working directory) from output")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON (one message per line, but formatted)")
    parser.add_argument(
        "--no-strict", action="store_true", help="Disable strict validation of block types and tool names"
    )

    args = parser.parse_args()

    # Read input
    if args.input == "-":
        lines = sys.stdin.readlines()
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: File not found: {args.input}", file=sys.stderr)
            sys.exit(1)
        lines = input_path.read_text().splitlines()

    # Convert
    messages = minimize_transcript(
        lines,
        keep_thinking=not args.no_thinking,
        preserve_cwd=not args.no_cwd,
        strict=not args.no_strict,
    )

    if not messages:
        print("Warning: No messages extracted from transcript", file=sys.stderr)

    # Format output
    if args.pretty:
        output_lines = [json.dumps(m, indent=2) for m in messages]
        output = "\n\n".join(output_lines)
    else:
        output_lines = [json.dumps(m, separators=(",", ":")) for m in messages]
        output = "\n".join(output_lines)

    # Write output
    if args.output:
        Path(args.output).write_text(output + "\n")
        print(f"Wrote {len(messages)} messages to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
