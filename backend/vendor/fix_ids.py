#!/usr/bin/env python3
"""
Replaces invalid tool IDs in minimized JSONL transcripts with realistic
random IDs matching the real Claude Code format. Already-valid IDs are
preserved.

Also fixes positional mismatches: when tool_use and tool_result blocks
have valid-format IDs that don't match each other, aligns them by
position (1st tool_use ↔ 1st tool_result, etc.).

Provides verification to check tool IDs for format issues, orphaned
use/result pairs, and positional mismatches.

Real tool ID format: toolu_01 + 22 random Base58 characters (30 chars total).
"""

import argparse
import json
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE58_CHARS = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TOOL_ID_PATTERN = re.compile(r"^toolu_01[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{22}$")


# --- Data types ---


@dataclass
class ToolIdIssue:
    """A single tool ID issue found in a transcript."""

    line_number: int
    block_type: str  # "tool_use" or "tool_result"
    id_value: str
    issue: str  # description of what's wrong


# --- Helpers ---


def generate_tool_id() -> str:
    """Generate a realistic tool ID matching Claude Code's format."""
    suffix = "".join(secrets.choice(BASE58_CHARS) for _ in range(22))
    return f"toolu_01{suffix}"


def _get_content_blocks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract content blocks from a transcript entry.

    In the single-block-per-line format, content is a single dict.
    Returns it as a one-element list for uniform iteration.

    Also supports full Claude Code format: {"type": ..., "message": {"content": [...]}}
    """
    # Minimal format: content is a single block dict
    if "role" in raw and "content" in raw and "type" not in raw:
        content = raw.get("content")
        if isinstance(content, dict):
            return [content]
        if isinstance(content, list):
            raise ValueError(
                "content is a list (old format detected). "
                "Expected single-block-per-line format where content is a dict."
            )
        return []

    # Full format (pre-minimized)
    if "type" in raw and "message" in raw:
        message = raw.get("message", {})
        if isinstance(message, dict):
            content = message.get("content", [])
            return content if isinstance(content, list) else []

    return []


def _get_role(raw: dict[str, Any]) -> str | None:
    """Extract role from a transcript entry."""
    if "role" in raw and "type" not in raw:
        return raw.get("role")
    # Full format (pre-minimized)
    if "type" in raw and "message" in raw:
        message = raw.get("message", {})
        if isinstance(message, dict):
            return message.get("role")
    return None


# --- Verification ---


def verify_tool_ids(transcript_path: Path) -> list[ToolIdIssue]:
    """Verify all tool IDs in a transcript file.

    Checks:
    1. All tool_use IDs match the expected format (toolu_01 + 22 base58 chars)
    2. All tool_result IDs match the expected format
    3. Every tool_result references a tool_use that exists
    4. Every tool_use has a corresponding tool_result
    5. Positional mismatches: tool_use/tool_result pairs that have valid
       format IDs but don't match each other by position

    Returns list of issues found (empty = all good).
    """
    issues: list[ToolIdIssue] = []
    tool_use_ids: dict[str, int] = {}  # id -> line_number
    tool_result_ids: dict[str, int] = {}  # id -> line_number

    lines = transcript_path.read_text(encoding="utf-8").splitlines()

    # Parse all entries for global checks
    parsed_entries: list[tuple[int, dict]] = []  # (line_number, entry)
    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed_entries.append((line_num, raw))

    for line_num, raw in parsed_entries:
        blocks = _get_content_blocks(raw)
        for block in blocks:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type")

            if block_type == "tool_use" and "id" in block:
                tid = block["id"]
                tool_use_ids[tid] = line_num
                if not TOOL_ID_PATTERN.match(tid):
                    issues.append(
                        ToolIdIssue(
                            line_number=line_num,
                            block_type="tool_use",
                            id_value=tid,
                            issue=f"Invalid format (expected toolu_01 + 22 base58 chars, got {tid!r})",
                        )
                    )

            elif block_type == "tool_result" and "tool_use_id" in block:
                tid = block["tool_use_id"]
                tool_result_ids[tid] = line_num
                if not TOOL_ID_PATTERN.match(tid):
                    issues.append(
                        ToolIdIssue(
                            line_number=line_num,
                            block_type="tool_result",
                            id_value=tid,
                            issue=f"Invalid format (expected toolu_01 + 22 base58 chars, got {tid!r})",
                        )
                    )

    # Check for orphaned tool_results (referencing a tool_use that doesn't exist)
    for tid, line_num in tool_result_ids.items():
        if tid not in tool_use_ids:
            issues.append(
                ToolIdIssue(
                    line_number=line_num,
                    block_type="tool_result",
                    id_value=tid,
                    issue="Orphaned tool_result: no matching tool_use found",
                )
            )

    # Check for orphaned tool_uses (no tool_result references them)
    for tid, line_num in tool_use_ids.items():
        if tid not in tool_result_ids:
            issues.append(
                ToolIdIssue(
                    line_number=line_num,
                    block_type="tool_use",
                    id_value=tid,
                    issue="Orphaned tool_use: no matching tool_result found",
                )
            )

    # Check for positional mismatches between assistant→user message groups
    # In single-block-per-line format, consecutive assistant lines with tool_use
    # form a group, followed by consecutive user lines with tool_result
    pending_use_ids: list[tuple[str, int]] = []  # (id, line_num) accumulated from assistant lines
    prev_role = None
    for line_num, raw in parsed_entries:
        role = _get_role(raw)

        # When role switches from assistant to user, start collecting results
        if role == "user" and prev_role == "assistant" and pending_use_ids:
            # Now collect consecutive user tool_result lines
            result_ids: list[tuple[str, int]] = []
            # Process this and following user messages
            start_idx = parsed_entries.index((line_num, raw))
            for j in range(start_idx, len(parsed_entries)):
                r_line_num, r_raw = parsed_entries[j]
                r_role = _get_role(r_raw)
                if r_role != "user":
                    break
                r_blocks = _get_content_blocks(r_raw)
                for block in r_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and "tool_use_id" in block:
                        result_ids.append((block["tool_use_id"], r_line_num))

            if result_ids and len(pending_use_ids) == len(result_ids):
                for i, ((use_id, _use_ln), (res_id, res_ln)) in enumerate(zip(pending_use_ids, result_ids)):
                    if use_id != res_id:
                        issues.append(
                            ToolIdIssue(
                                line_number=res_ln,
                                block_type="tool_result",
                                id_value=res_id,
                                issue=f"Positional mismatch at position {i}: tool_use has {use_id!r} but tool_result has {res_id!r}",
                            )
                        )
            pending_use_ids = []

        if role == "assistant":
            blocks = _get_content_blocks(raw)
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use" and "id" in block:
                    pending_use_ids.append((block["id"], line_num))
        elif role != "user":
            pending_use_ids = []

        prev_role = role

    return issues


def format_issues(issues: list[ToolIdIssue]) -> str:
    """Format issues into a human-readable colored string."""
    lines = [
        f"\033[1;33mTool ID Verification: {len(issues)} issue(s) found\033[0m",
        "",
    ]

    # Group by issue type for cleaner display
    fmt_issues = [i for i in issues if "Invalid format" in i.issue]
    orphaned_results = [i for i in issues if "Orphaned tool_result" in i.issue]
    orphaned_uses = [i for i in issues if "Orphaned tool_use" in i.issue]
    positional = [i for i in issues if "Positional mismatch" in i.issue]

    if fmt_issues:
        lines.append(f"\033[31mInvalid tool ID format ({len(fmt_issues)}):\033[0m")
        for issue in fmt_issues:
            lines.append(f"  Line {issue.line_number}: {issue.block_type} id={issue.id_value!r}")
        lines.append("")

    if positional:
        lines.append(f"\033[35mPositional ID mismatches ({len(positional)}):\033[0m")
        for issue in positional:
            lines.append(f"  Line {issue.line_number}: {issue.issue}")
        lines.append("")

    if orphaned_results:
        lines.append(f"\033[33mOrphaned tool_results ({len(orphaned_results)}):\033[0m")
        for issue in orphaned_results:
            lines.append(f"  Line {issue.line_number}: tool_use_id={issue.id_value!r}")
        lines.append("")

    if orphaned_uses:
        lines.append(f"\033[33mOrphaned tool_uses ({len(orphaned_uses)}):\033[0m")
        for issue in orphaned_uses:
            lines.append(f"  Line {issue.line_number}: id={issue.id_value!r}")
        lines.append("")

    return "\n".join(lines)


# --- Fixing ---


def _collect_ids_from_blocks(blocks: list[dict[str, Any]], id_map: dict[str, str]) -> None:
    """Walk content blocks and populate id_map for any IDs that need replacing."""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_use" and "id" in block:
            old_id = block["id"]
            if old_id not in id_map:
                if TOOL_ID_PATTERN.match(old_id):
                    id_map[old_id] = old_id
                else:
                    id_map[old_id] = generate_tool_id()
        elif block_type == "tool_result" and "tool_use_id" in block:
            old_id = block["tool_use_id"]
            if old_id not in id_map:
                if TOOL_ID_PATTERN.match(old_id):
                    id_map[old_id] = old_id
                else:
                    id_map[old_id] = generate_tool_id()


def fix_transcript(input_lines: list[str]) -> tuple[list[str], dict[str, str], int]:
    """Fix all tool IDs in a transcript using string replacement to preserve formatting.

    Three passes:
    1. Parse to discover all tool IDs and build the replacement map
    2. Positional fix: find tool_result IDs that don't match their positional
       tool_use counterparts and add those to the replacement map
    3. String-replace old IDs with new IDs on the raw lines

    Returns (output_lines, id_map, positional_fixes).
    """
    id_map: dict[str, str] = {}
    entries: list[dict] = []
    valid_line_indices: list[int] = []

    # Pass 1: parse and collect IDs
    for i, line in enumerate(input_lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        valid_line_indices.append(i)
        entries.append(entry)
        blocks = _get_content_blocks(entry)
        _collect_ids_from_blocks(blocks, id_map)

    # Pass 2: find positional mismatches and add to id_map
    # Accumulate tool_use IDs across consecutive assistant lines,
    # then match against consecutive user tool_result lines
    positional_fixes = 0
    pending_use_ids: list[str] = []
    prev_role = None
    for idx, entry in enumerate(entries):
        role = _get_role(entry)
        blocks = _get_content_blocks(entry)

        if role == "user" and prev_role == "assistant" and pending_use_ids:
            # Collect consecutive user tool_result blocks
            result_blocks = []
            for j in range(idx, len(entries)):
                r_role = _get_role(entries[j])
                if r_role != "user":
                    break
                r_blocks = _get_content_blocks(entries[j])
                for block in r_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and "tool_use_id" in block:
                        result_blocks.append(block)

            if len(result_blocks) == len(pending_use_ids):
                for use_id, result_block in zip(pending_use_ids, result_blocks):
                    res_id = result_block["tool_use_id"]
                    mapped_use_id = id_map.get(use_id, use_id)
                    mapped_res_id = id_map.get(res_id, res_id)
                    if mapped_res_id != mapped_use_id:
                        id_map[res_id] = mapped_use_id
                        positional_fixes += 1
            elif result_blocks and len(result_blocks) != len(pending_use_ids):
                print(
                    f"\033[33mWarning: tool_use count ({len(pending_use_ids)}) != "
                    f"tool_result count ({len(result_blocks)}), skipping positional fix\033[0m",
                    file=sys.stderr,
                )
            pending_use_ids = []

        if role == "assistant":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use" and "id" in block:
                    pending_use_ids.append(block["id"])
        elif role != "user":
            pending_use_ids = []

        prev_role = role

    # Pass 3: string-replace on raw lines (only IDs that actually changed)
    # Use re.sub with a single pass to avoid substring collisions — short
    # placeholders like "toolu_01D" can match inside already-replaced IDs
    # (e.g. a generated ID starting with "toolu_01D...").
    replacements = {old: new for old, new in id_map.items() if old != new}
    output_lines = list(input_lines)
    if replacements:
        # Build a single regex that matches any old ID, longest first
        sorted_old = sorted(replacements.keys(), key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(old) for old in sorted_old))
        for i in valid_line_indices:
            line = output_lines[i]
            output_lines[i] = pattern.sub(lambda m: replacements[m.group(0)], line)

    return output_lines, id_map, positional_fixes


def fix_transcript_file(transcript_path: Path, output_path: Path) -> tuple[int, int, int]:
    """Fix all tool IDs in a transcript file, writing results to output_path.

    Convenience wrapper around fix_transcript() for file-to-file operation.
    Preserves original JSON formatting — only tool ID strings are changed.

    Returns (entries_count, ids_replaced, positional_fixes).
    """
    lines = transcript_path.read_text(encoding="utf-8").splitlines()
    output_lines, id_map, positional_fixes = fix_transcript(lines)
    output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    entry_count = sum(1 for line in lines if line.strip())
    return entry_count, len(id_map), positional_fixes


# --- CLI ---


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Fix tool IDs in JSONL transcripts: replaces invalid-format IDs with "
            "realistic random IDs, and aligns positional mismatches between "
            "tool_use/tool_result pairs"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s transcript.jsonl                  # Output to stdout
  %(prog)s transcript.jsonl -o fixed.jsonl   # Output to file
  cat transcript.jsonl | %(prog)s -          # Read from stdin
  %(prog)s transcript.jsonl --dry-run        # Show old→new mapping only
  %(prog)s transcript.jsonl --verify         # Check for issues without fixing
        """,
    )
    parser.add_argument("input", help="Input transcript file (use '-' for stdin)")
    parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show old→new ID mapping to stderr without modifying",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check tool IDs for issues without fixing (exit code 1 if issues found)",
    )

    args = parser.parse_args()

    # Read input
    if args.input == "-":
        if args.verify:
            print("Error: --verify requires a file path, not stdin", file=sys.stderr)
            sys.exit(1)
        lines = sys.stdin.readlines()
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: File not found: {args.input}", file=sys.stderr)
            sys.exit(1)
        lines = input_path.read_text().splitlines()

    # Verify mode
    if args.verify:
        issues = verify_tool_ids(Path(args.input))
        if issues:
            print(format_issues(issues), file=sys.stderr)
            sys.exit(1)
        else:
            print("\033[32mAll tool IDs valid.\033[0m", file=sys.stderr)
            sys.exit(0)

    # Fix IDs
    output_lines, id_map, positional_fixes = fix_transcript(lines)

    entry_count = sum(1 for line in lines if line.strip())
    if entry_count == 0:
        print("Warning: No entries found in transcript", file=sys.stderr)

    if not id_map and positional_fixes == 0:
        print("Warning: No tool IDs found to fix", file=sys.stderr)

    # Dry run: just show the mapping
    if args.dry_run:
        format_replaced = sum(1 for old, new in id_map.items() if old != new)
        print(f"\033[36mFound {len(id_map)} unique tool IDs ({format_replaced} format-fixed):\033[0m", file=sys.stderr)
        for old, new in id_map.items():
            if old == new:
                print(f"  \033[32m{old} (valid)\033[0m", file=sys.stderr)
            else:
                print(f"  \033[33m{old} → {new}\033[0m", file=sys.stderr)
        if positional_fixes:
            print(f"\033[35m{positional_fixes} positional mismatch(es) fixed\033[0m", file=sys.stderr)
        return

    output = "\n".join(output_lines)

    # Write output
    if args.output:
        Path(args.output).write_text(output + "\n")
        format_replaced = sum(1 for old, new in id_map.items() if old != new)
        parts = []
        if format_replaced:
            parts.append(f"{format_replaced} format-fixed")
        if positional_fixes:
            parts.append(f"{positional_fixes} positional-fixed")
        fix_summary = ", ".join(parts) if parts else "no changes"
        print(
            f"\033[32mWrote {entry_count} entries ({fix_summary}) to {args.output}\033[0m",
            file=sys.stderr,
        )
    else:
        print(output)


if __name__ == "__main__":
    main()
