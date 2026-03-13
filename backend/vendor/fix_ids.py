#!/usr/bin/env python3
"""
Replaces invalid tool IDs in minimized JSONL transcripts with realistic
random IDs matching the real Claude Code format. Already-valid IDs are
preserved.

Also provides verification to check tool IDs for format issues and
orphaned use/result pairs.

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
    """Extract content blocks from a transcript entry (either format).

    Supports both:
      Simple/API format: {"role": ..., "content": [...]}
      Full Claude Code format: {"type": ..., "message": {"content": [...]}}
    """
    # Simple format
    if "role" in raw and "content" in raw and "type" not in raw:
        content = raw.get("content", [])
        return content if isinstance(content, list) else []

    # Full format
    if "type" in raw and "message" in raw:
        message = raw.get("message", {})
        if isinstance(message, dict):
            content = message.get("content", [])
            return content if isinstance(content, list) else []

    return []


# --- Verification ---


def verify_tool_ids(transcript_path: Path) -> list[ToolIdIssue]:
    """Verify all tool IDs in a transcript file.

    Checks:
    1. All tool_use IDs match the expected format (toolu_01 + 22 base58 chars)
    2. All tool_result IDs match the expected format
    3. Every tool_result references a tool_use that exists
    4. Every tool_use has a corresponding tool_result

    Returns list of issues found (empty = all good).
    """
    issues: list[ToolIdIssue] = []
    tool_use_ids: dict[str, int] = {}  # id -> line_number
    tool_result_ids: dict[str, int] = {}  # id -> line_number

    lines = transcript_path.read_text(encoding="utf-8").splitlines()

    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue

        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue

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

    if fmt_issues:
        lines.append(f"\033[31mInvalid tool ID format ({len(fmt_issues)}):\033[0m")
        for issue in fmt_issues:
            lines.append(f"  Line {issue.line_number}: {issue.block_type} id={issue.id_value!r}")
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


def fix_content_blocks(content: Any, id_map: dict[str, str]) -> Any:
    """Walk content blocks and replace invalid tool IDs, preserving valid ones."""
    if not isinstance(content, list):
        return content

    for block in content:
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
            block["id"] = id_map[old_id]

        elif block_type == "tool_result" and "tool_use_id" in block:
            old_id = block["tool_use_id"]
            if old_id not in id_map:
                if TOOL_ID_PATTERN.match(old_id):
                    id_map[old_id] = old_id
                else:
                    id_map[old_id] = generate_tool_id()
            block["tool_use_id"] = id_map[old_id]

    return content


def fix_entry(entry: dict, id_map: dict[str, str]) -> dict:
    """Fix tool IDs in a single transcript entry.

    Handles both minimal format (role/content at top level) and
    full format (type/message wrapper).
    """
    # Minimal format: role + content at top level
    if "role" in entry and "content" in entry and "type" not in entry:
        entry["content"] = fix_content_blocks(entry["content"], id_map)
        return entry

    # Full format: type + message wrapper
    if "type" in entry and "message" in entry:
        message = entry["message"]
        if isinstance(message, dict) and "content" in message:
            message["content"] = fix_content_blocks(message["content"], id_map)
        return entry

    # Unknown format — pass through unchanged
    return entry


def fix_transcript(input_lines: list[str]) -> tuple[list[dict], dict[str, str]]:
    """Fix all tool IDs in a transcript. Returns (entries, id_map)."""
    id_map: dict[str, str] = {}
    entries = []

    for line in input_lines:
        line = line.strip()
        if not line:
            continue

        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entries.append(fix_entry(entry, id_map))

    return entries, id_map


def fix_transcript_file(transcript_path: Path, output_path: Path) -> tuple[int, int]:
    """Fix all tool IDs in a transcript file, writing results to output_path.

    Convenience wrapper around fix_transcript() for file-to-file operation.

    Returns (entries_count, ids_replaced).
    """
    lines = transcript_path.read_text(encoding="utf-8").splitlines()
    entries, id_map = fix_transcript(lines)
    output_lines = [json.dumps(e, separators=(",", ":"), ensure_ascii=False) for e in entries]
    output_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    return len(entries), len(id_map)


# --- CLI ---


def main():
    parser = argparse.ArgumentParser(
        description="Replace tool IDs in JSONL transcripts with realistic random IDs",
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
    entries, id_map = fix_transcript(lines)

    if not entries:
        print("Warning: No entries found in transcript", file=sys.stderr)

    if not id_map:
        print("Warning: No tool IDs found to replace", file=sys.stderr)

    # Dry run: just show the mapping
    if args.dry_run:
        print(f"Found {len(id_map)} unique tool IDs:", file=sys.stderr)
        for old, new in id_map.items():
            print(f"  {old} → {new}", file=sys.stderr)
        return

    # Format output (compact JSON, matching minimizer style)
    output_lines = [json.dumps(e, separators=(",", ":")) for e in entries]
    output = "\n".join(output_lines)

    # Write output
    if args.output:
        Path(args.output).write_text(output + "\n")
        print(
            f"Wrote {len(entries)} entries ({len(id_map)} IDs replaced) to {args.output}",
            file=sys.stderr,
        )
    else:
        print(output)


if __name__ == "__main__":
    main()
