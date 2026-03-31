#!/usr/bin/env python3
"""
CLI for transcript editor — wraps executor.py for use from shell/Claude Code.

Usage:
    te open <file>
    te list [--start N] [--end N] [--search TEXT]
    te show <index> [--key KEY] [--offset N] [--limit N]
    te insert <index> <role> <content_json> [--cwd PATH]
    te update <index> <content_json>
    te delete <start> <end>
    te move <from_start> <from_end> <to>
    te replace <start> <end> <messages_json>
    te find-replace <find> <replace> [--regex] [--raw-json] [--key KEY] [--index N] [--start N] [--end N]
    te update-tool-content <index> [--content TEXT] [--source N]
    te count-tokens [--start N] [--end N]
    te metadata [get|set] [--summary JSON] [--outcome TEXT] [--scenario TEXT] [--mechanism JSON]
    te check-consistency
    te current

Install: symlink to PATH as 'te':
    ln -sf /Users/colin/Code/agent-transcript-editor/backend/cli.py /usr/local/bin/te
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add backend to path so imports work from anywhere
_BACKEND_DIR = str(Path(__file__).resolve().parent)
sys.path.insert(0, _BACKEND_DIR)
from executor import ExecuteResult, execute_tool

# ── State file for persistence across invocations ────────────────────────

STATE_FILE = Path(__file__).parent / ".te_state.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def _load_file(path: Path) -> list[dict]:
    """Load a JSONL transcript file."""
    messages = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


def _save_file(path: Path, messages: list[dict]) -> None:
    """Save messages back to JSONL file."""
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def _get_current_file() -> Path:
    """Get the currently open file from state, or die."""
    state = _load_state()
    fp = state.get("file")
    if not fp:
        print("Error: No transcript file open. Use: te open <file>", file=sys.stderr)
        sys.exit(1)
    path = Path(fp)
    if not path.exists():
        print(f"Error: File no longer exists: {fp}", file=sys.stderr)
        sys.exit(1)
    return path


def _exec(name: str, inp: dict, messages: list[dict]) -> ExecuteResult:
    """Execute a tool and return result."""
    return execute_tool(name, inp, messages)


# ── Commands ─────────────────────────────────────────────────────────────


def cmd_open(args):
    path = Path(args.file).resolve()
    if not path.exists():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    if path.suffix != ".jsonl":
        print(f"Error: Expected .jsonl file, got: {path.suffix}", file=sys.stderr)
        sys.exit(1)
    messages = _load_file(path)
    _save_state({"file": str(path)})
    print(f"Opened {path.name} — {len(messages)} messages")


def cmd_current(args):
    state = _load_state()
    fp = state.get("file")
    if not fp:
        print("No file open. Use: te open <file>")
        return
    path = Path(fp)
    if not path.exists():
        print(f"{fp} (file missing)")
        return
    messages = _load_file(path)
    print(f"{fp} — {len(messages)} messages")


def cmd_list(args):
    path = _get_current_file()
    messages = _load_file(path)
    inp = {}
    if args.start is not None:
        inp["start"] = args.start
    if args.end is not None:
        inp["end"] = args.end
    if args.search is not None:
        inp["search"] = args.search
    result = _exec("get_messages", inp, messages)
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    print(result.result)


def cmd_show(args):
    path = _get_current_file()
    messages = _load_file(path)
    inp = {"message_index": args.index}
    if args.key is not None:
        inp["input_key"] = args.key
    if args.offset is not None:
        inp["offset"] = args.offset
    if args.limit is not None:
        inp["limit"] = args.limit
    result = _exec("get_messages", inp, messages)
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    print(result.result)


def cmd_insert(args):
    path = _get_current_file()
    messages = _load_file(path)
    content = json.loads(args.content)
    cwd = args.cwd or "/home/user/project"
    inp = {"index": args.index, "role": args.role, "content": content, "cwd": cwd}
    result = _exec("insert_message", inp, messages)
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    if result.messages is not None:
        _save_file(path, result.messages)
    print(result.result)


def cmd_update(args):
    path = _get_current_file()
    messages = _load_file(path)
    content = json.loads(args.content)
    inp = {"index": args.index, "content": content}
    result = _exec("update_message", inp, messages)
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    if result.messages is not None:
        _save_file(path, result.messages)
    print(result.result)


def cmd_delete(args):
    path = _get_current_file()
    messages = _load_file(path)
    inp = {"start": args.start, "end": args.end}
    result = _exec("delete_messages", inp, messages)
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    if result.messages is not None:
        _save_file(path, result.messages)
    print(result.result)


def cmd_move(args):
    path = _get_current_file()
    messages = _load_file(path)
    inp = {"from_start": args.from_start, "from_end": args.from_end, "to": args.to}
    result = _exec("move_messages", inp, messages)
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    if result.messages is not None:
        _save_file(path, result.messages)
    print(result.result)


def cmd_replace(args):
    path = _get_current_file()
    messages = _load_file(path)
    new_messages = json.loads(args.messages)
    inp = {"start": args.start, "end": args.end, "messages": new_messages}
    result = _exec("replace_messages", inp, messages)
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    if result.messages is not None:
        _save_file(path, result.messages)
    print(result.result)


def cmd_find_replace(args):
    path = _get_current_file()
    messages = _load_file(path)
    inp = {"find": args.find, "replace": args.replace}
    if args.regex:
        inp["regex"] = True
    if args.raw_json:
        inp["raw_json"] = True
    if args.key is not None:
        inp["input_key"] = args.key
    if args.index is not None:
        inp["message_index"] = args.index
    if args.start is not None:
        inp["start"] = args.start
    if args.end is not None:
        inp["end"] = args.end
    result = _exec("find_replace", inp, messages)
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    if result.messages is not None:
        _save_file(path, result.messages)
    print(result.result)


def cmd_update_tool_content(args):
    path = _get_current_file()
    messages = _load_file(path)
    inp = {"message_index": args.index}
    if args.content is not None:
        inp["content"] = args.content
    if args.source is not None:
        inp["source_message"] = args.source
    result = _exec("update_tool_content", inp, messages)
    if not result.success:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    if result.messages is not None:
        _save_file(path, result.messages)
    print(result.result)


def cmd_count_tokens(args):
    path = _get_current_file()
    messages = _load_file(path)
    target = "transcript"
    msgs = messages

    if args.start is not None or args.end is not None:
        target = "selection"
        start = (args.start or 1) - 1
        end = args.end or len(messages)
        msgs = messages[start:end]

    # Try Anthropic API for accurate count, fall back to estimate
    try:
        import anthropic
        client = anthropic.Anthropic()
        text = "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs)
        result = client.messages.count_tokens(
            model="claude-sonnet-4-5-20250929",
            messages=[{"role": "user", "content": text}],
        )
        print(json.dumps({"token_count": result.input_tokens, "message_count": len(msgs), "target": target}, indent=2))
    except Exception:
        text = "\n".join(json.dumps(m, ensure_ascii=False) for m in msgs)
        rough = round(len(text) / 4)
        print(json.dumps({"token_count": rough, "message_count": len(msgs), "target": target, "note": "Estimated (API unavailable)"}, indent=2))


def cmd_metadata(args):
    path = _get_current_file()
    import sidecar

    if args.action == "get" or (args.action is None and args.outcome is None and args.scenario is None and args.summary is None and args.mechanism is None):
        meta = sidecar.load(path)
        if meta is None:
            print("No metadata file exists for this transcript.")
        else:
            print(json.dumps(meta, indent=2, ensure_ascii=False))
    else:
        data = {}
        if args.summary is not None:
            data["summary"] = json.loads(args.summary)
        if args.outcome is not None:
            data["outcome"] = args.outcome
        if args.scenario is not None:
            data["scenario"] = args.scenario
        if args.mechanism is not None:
            data["mechanism"] = json.loads(args.mechanism)
        if not data:
            print("Error: No metadata fields provided", file=sys.stderr)
            sys.exit(1)
        sidecar.save(path, data)
        print(f"Metadata updated: {', '.join(data.keys())}")


def cmd_check_consistency(args):
    path = _get_current_file()
    messages = _load_file(path)

    from dataclasses import asdict
    from vendor.check_consistency import check_consistency

    result = check_consistency(messages)
    if not result.warnings:
        print(f"No inconsistencies found. ({result.files_tracked} files tracked, {result.files_known} fully known)")
    else:
        print(json.dumps(
            {"warnings": [asdict(w) for w in result.warnings], "files_tracked": result.files_tracked, "files_known": result.files_known},
            indent=2, ensure_ascii=False,
        ))


# ── Parser ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="te",
        description="Transcript editor CLI — read, edit, and manage .jsonl transcripts",
    )
    sub = parser.add_subparsers(dest="command")

    # open
    p = sub.add_parser("open", help="Open a transcript file")
    p.add_argument("file", help="Path to .jsonl file")
    p.set_defaults(func=cmd_open)

    # current
    p = sub.add_parser("current", help="Show currently open file")
    p.set_defaults(func=cmd_current)

    # list
    p = sub.add_parser("list", aliases=["ls"], help="List messages (truncated previews)")
    p.add_argument("--start", "-s", type=int, help="Start index (1-indexed)")
    p.add_argument("--end", "-e", type=int, help="End index (1-indexed)")
    p.add_argument("--search", help="Filter to messages containing text")
    p.set_defaults(func=cmd_list)

    # show
    p = sub.add_parser("show", aliases=["get"], help="Show full content of a message")
    p.add_argument("index", type=int, help="Message index (1-indexed)")
    p.add_argument("--key", "-k", help="Extract specific input key from tool_use")
    p.add_argument("--offset", type=int, help="Character offset")
    p.add_argument("--limit", type=int, help="Max characters to return")
    p.set_defaults(func=cmd_show)

    # insert
    p = sub.add_parser("insert", help="Insert a message before index")
    p.add_argument("index", type=int, help="Position to insert before (1-indexed)")
    p.add_argument("role", choices=["user", "assistant"], help="Message role")
    p.add_argument("content", help="Content block as JSON string")
    p.add_argument("--cwd", help="Working directory (default: /home/user/project)")
    p.set_defaults(func=cmd_insert)

    # update
    p = sub.add_parser("update", help="Update a message's content")
    p.add_argument("index", type=int, help="Message index (1-indexed)")
    p.add_argument("content", help="New content block as JSON string")
    p.set_defaults(func=cmd_update)

    # delete
    p = sub.add_parser("delete", aliases=["rm"], help="Delete a range of messages")
    p.add_argument("start", type=int, help="Start index (1-indexed, inclusive)")
    p.add_argument("end", type=int, help="End index (1-indexed, inclusive)")
    p.set_defaults(func=cmd_delete)

    # move
    p = sub.add_parser("move", aliases=["mv"], help="Move messages to a new position")
    p.add_argument("from_start", type=int, help="Start of range (1-indexed)")
    p.add_argument("from_end", type=int, help="End of range (1-indexed)")
    p.add_argument("to", type=int, help="Destination index (1-indexed)")
    p.set_defaults(func=cmd_move)

    # replace
    p = sub.add_parser("replace", help="Replace a range with new messages")
    p.add_argument("start", type=int, help="Start index (1-indexed)")
    p.add_argument("end", type=int, help="End index (1-indexed)")
    p.add_argument("messages", help="New messages as JSON array")
    p.set_defaults(func=cmd_replace)

    # find-replace
    p = sub.add_parser("find-replace", aliases=["fr"], help="Find and replace text")
    p.add_argument("find", help="Text to find")
    p.add_argument("replace", help="Replacement text")
    p.add_argument("--regex", "-r", action="store_true", help="Treat find as regex")
    p.add_argument("--raw-json", action="store_true", help="Operate on JSON representation")
    p.add_argument("--key", "-k", help="Target specific input key in tool_use")
    p.add_argument("--index", "-i", type=int, help="Single message index (1-indexed)")
    p.add_argument("--start", "-s", type=int, help="Start of range")
    p.add_argument("--end", "-e", type=int, help="End of range")
    p.set_defaults(func=cmd_find_replace)

    # update-tool-content
    p = sub.add_parser("update-tool-content", aliases=["utc"], help="Update Write tool content")
    p.add_argument("index", type=int, help="Message index of tool_use (1-indexed)")
    p.add_argument("--content", "-c", help="New file content")
    p.add_argument("--source", type=int, help="Copy content from this message index")
    p.set_defaults(func=cmd_update_tool_content)

    # count-tokens
    p = sub.add_parser("count-tokens", aliases=["tokens"], help="Count tools-only tokens")
    p.add_argument("--start", "-s", type=int, help="Start index (1-indexed)")
    p.add_argument("--end", "-e", type=int, help="End index (1-indexed)")
    p.set_defaults(func=cmd_count_tokens)

    # metadata
    p = sub.add_parser("metadata", aliases=["meta"], help="Get or set transcript metadata")
    p.add_argument("action", nargs="?", choices=["get", "set"], help="Action (default: get)")
    p.add_argument("--summary", help="Summary dict as JSON")
    p.add_argument("--outcome", help="Outcome description")
    p.add_argument("--scenario", help="Scenario description")
    p.add_argument("--mechanism", help="Mechanism dict as JSON")
    p.set_defaults(func=cmd_metadata)

    # check-consistency
    p = sub.add_parser("check-consistency", aliases=["check"], help="Check for file-state inconsistencies")
    p.set_defaults(func=cmd_check_consistency)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
