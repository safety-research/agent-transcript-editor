"""
Python port of src/llm/executor.ts — transcript tool execution.

All read/write tools that operate on transcript message arrays.
User-facing indices are 1-indexed, converted to 0-indexed internally.
Write tools return (result_string, new_messages, message_op) — immutable, never mutate input.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# ── Types ─────────────────────────────────────────────────────────────────

Message = dict[str, Any]  # {role, content, cwd}

MessageOp = (
    dict[str, Any]
    # { type: 'insert', index }
    # { type: 'delete', start, end }
    # { type: 'move', fromStart, fromEnd, to }
    # { type: 'replace', start, end, newCount }
)


@dataclass
class ExecuteResult:
    success: bool
    result: str | None = None
    error: str | None = None
    messages: list[Message] | None = None
    message_op: MessageOp | None = None


# ── Constants ─────────────────────────────────────────────────────────────

MAX_DETAIL_LIMIT = 10000
DEFAULT_DETAIL_LIMIT = 10000
MAX_RANGE_LIMIT = 30000
LIST_PREVIEW_LENGTH = 200

READ_TOOLS = {"get_messages"}
WRITE_TOOLS = {
    "insert_message",
    "update_message",
    "delete_messages",
    "move_messages",
    "replace_messages",
    "update_tool_content",
    "find_replace",
}
CONTROL_TOOLS = {"finish_editing"}
META_TOOLS = {
    "fork_to_new_tab",
    "duplicate_transcript",
    "get_monitor_score",
    "set_transcript_metadata",
    "evaluate_egregiousness",
    "suggest_changes",
    "count_tokens",
    "vent",
    "check_consistency",
    "suppress_consistency_warning",
}


def is_read_tool(name: str) -> bool:
    return name in READ_TOOLS


def is_write_tool(name: str) -> bool:
    return name in WRITE_TOOLS


def is_finish_tool(name: str) -> bool:
    return name in CONTROL_TOOLS


def is_meta_tool(name: str) -> bool:
    return name in META_TOOLS


# ── Helpers ───────────────────────────────────────────────────────────────


def safe_stringify(value: Any, indent: int | None = None) -> str:
    """Safe stringify that always returns a string."""
    if value is None:
        return "[]"
    result = json.dumps(value, indent=indent, ensure_ascii=False)
    return result if result is not None else "[]"


def _format_block_as_text(block: dict[str, Any]) -> str:
    """Format a single content block as readable text.

    Unlike safe_stringify (json.dumps), this preserves string content verbatim —
    real newlines stay as newlines, quotes stay as quotes. This avoids double-encoding
    when the result gets embedded in a JSON response.
    """
    if "type" not in block:
        raise ValueError(f"Content block missing required 'type' field: {safe_stringify(block, 2)}")
    block_type = block["type"]
    if block_type == "text":
        return f"[text]\n{block['text']}"
    elif block_type == "thinking":
        return f"[thinking]\n{block['thinking']}"
    elif block_type == "tool_result":
        tool_use_id = block.get("tool_use_id", "")
        content = block.get("content", "")
        if isinstance(content, str):
            return f"[tool_result for {tool_use_id}]\n{content}"
        else:
            return f"[tool_result for {tool_use_id}]\n" + json.dumps(content, indent=2, ensure_ascii=False)
    elif block_type == "tool_use":
        name = block.get("name", "")
        return f'[tool_use "{name}"]\n' + json.dumps(block.get("input", {}), indent=2, ensure_ascii=False)
    else:
        raise ValueError(f"Unknown content block type {block_type!r}: {safe_stringify(block, 2)}")


def _json_escape_string(s: str) -> str:
    """Convert a string to its JSON-encoded form (without surrounding quotes).

    Use when matching or splicing text into JSON-serialized content (e.g.,
    find_replace in raw_json mode on tool_use inputs).
    Ensures control characters like \\t, \\n match the JSON encoding.
    Example: real tab char → \\t (2 chars), real newline → \\n (2 chars).
    """
    return json.dumps(s)[1:-1]


def to_array_index(ui_index: int) -> int:
    """Convert 1-indexed (UI) to 0-indexed (array)."""
    return ui_index - 1


def _deep_copy_messages(messages: list[Message]) -> list[Message]:
    """Deep copy messages list for immutable write operations."""
    return [{**m, "content": dict(m["content"]) if isinstance(m["content"], dict) else m["content"]} for m in messages]


# ── Mechanism Key Remapping ───────────────────────────────────────────────


def _parse_int_keys(d: dict) -> tuple[dict[int, str], dict[str, str]]:
    """Split a mechanism/summary dict into integer-keyed and non-integer-keyed entries."""
    int_keys: dict[int, str] = {}
    other_keys: dict[str, str] = {}
    for k, v in d.items():
        try:
            int_keys[int(k)] = v
        except (ValueError, TypeError):
            other_keys[str(k)] = v
    return int_keys, other_keys


def remap_mechanism_keys(
    d: dict[int, str] | None,
    op: MessageOp,
) -> dict[int, str] | None:
    """Remap mechanism/summary dict keys after a message operation."""
    if not d:
        return d

    int_entries, other_entries = _parse_int_keys(d)
    result: dict[int, str] = {}
    op_type = op["type"]

    if op_type == "insert":
        index = op["index"]
        for key, v in int_entries.items():
            if key >= index:
                result[key + 1] = v
            else:
                result[key] = v

    elif op_type == "delete":
        start, end = op["start"], op["end"]
        count = end - start + 1
        for key, v in int_entries.items():
            if start <= key <= end:
                pass  # removed
            elif key > end:
                result[key - count] = v
            else:
                result[key] = v

    elif op_type == "move":
        from_start = op["fromStart"]
        from_end = op["fromEnd"]
        to = op["to"]
        count = from_end - from_start + 1

        extracted: list[tuple[int, str]] = []
        remaining: list[tuple[int, str]] = []

        for key, v in int_entries.items():
            if from_start <= key <= from_end:
                extracted.append((key - from_start, v))
            else:
                remaining.append((key, v))

        # Close gap
        after_removal = []
        for key, v in remaining:
            if key > from_end:
                after_removal.append((key - count, v))
            else:
                after_removal.append((key, v))

        adjusted_to = to
        if to > from_end:
            adjusted_to -= count

        for key, v in after_removal:
            if key >= adjusted_to:
                result[key + count] = v
            else:
                result[key] = v

        for offset, v in extracted:
            result[adjusted_to + offset] = v

    elif op_type == "replace":
        start, end = op["start"], op["end"]
        removed_count = end - start + 1
        delta = op["newCount"] - removed_count
        for key, v in int_entries.items():
            if start <= key <= end:
                pass  # removed
            elif key > end:
                result[key + delta] = v
            else:
                result[key] = v

    # Preserve non-integer keys (e.g. "attack", "main_task") unchanged
    result.update(other_entries)
    return result


# ── Read Tools ────────────────────────────────────────────────────────────


def execute_read_tool(
    name: str,
    inp: dict[str, Any],
    messages: list[Message],
) -> ExecuteResult:
    if name != "get_messages":
        return ExecuteResult(success=False, error=f"Unknown read tool: {name}")

    message_index = inp.get("message_index")

    # Detail mode: read a single message with pagination
    if message_index is not None:
        array_index = to_array_index(message_index)
        if array_index < 0 or array_index >= len(messages):
            return ExecuteResult(
                success=False,
                error=f"Invalid message index: {message_index}. Valid range: 1-{len(messages)}",
            )

        message = messages[array_index]
        block = message["content"]
        input_key = inp.get("input_key")
        block_type = block.get("type", "unknown")

        # Field mode: show specific field of content block with character offsets
        if input_key is not None:
            if block_type != "tool_use":
                return ExecuteResult(
                    success=False,
                    error=f"input_key only works on tool_use blocks, got {block_type}",
                )
            if input_key not in block["input"]:
                available = ", ".join(block["input"].keys())
                return ExecuteResult(
                    success=False,
                    error=f'Key "{input_key}" not found in tool_use input. Available keys: {available}',
                )
            val = block["input"][input_key]
            if not isinstance(val, str):
                return ExecuteResult(
                    success=False,
                    error=f'input["{input_key}"] is {type(val).__name__}, not a string. input_key only works with string fields.',
                )
            text_content = val
            content_desc = f'tool_use "{block["name"]}" input.{input_key} (plain string)'

            total_length = len(text_content)
            offset = max(0, inp.get("offset", 0) or 0)
            limit = min(MAX_DETAIL_LIMIT, inp.get("limit", DEFAULT_DETAIL_LIMIT) or DEFAULT_DETAIL_LIMIT)
            chunk = text_content[offset : offset + limit]
            has_more = offset + limit < total_length

            lines = chunk.split("\n")
            char_pos = offset
            numbered_lines = []
            for line in lines:
                numbered_lines.append(f"[{char_pos}] {line}")
                char_pos += len(line) + 1

            return ExecuteResult(
                success=True,
                result=json.dumps(
                    {
                        "message_index": message_index,
                        "block_type": block_type,
                        "content_type": content_desc,
                        "total_length": total_length,
                        "offset": offset,
                        "limit": limit,
                        "has_more": has_more,
                        "content": "\n".join(numbered_lines),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )

        search = inp.get("search")

        # Search within message
        if search:
            block_str = safe_stringify(block, 2)
            search_lower = search.lower()
            matches_search = search_lower in block_str.lower()

            result_data: dict[str, Any] = {
                "message_index": message_index,
                "role": message["role"],
                "cwd": message.get("cwd"),
                "search": search,
                "block_type": block_type,
                "matches": matches_search,
            }

            if matches_search:
                match_pos = block_str.lower().index(search_lower)
                context_start = max(0, match_pos - 100)
                context_end = min(len(block_str), match_pos + len(search) + 100)
                preview = (
                    ("..." if context_start > 0 else "")
                    + block_str[context_start:context_end]
                    + ("..." if context_end < len(block_str) else "")
                )
                result_data["preview"] = preview

            return ExecuteResult(
                success=True,
                result=json.dumps(result_data, indent=2, ensure_ascii=False),
            )

        full_content = _format_block_as_text(block)
        total_length = len(full_content)
        offset = max(0, inp.get("offset", 0) or 0)
        limit = min(MAX_DETAIL_LIMIT, inp.get("limit", DEFAULT_DETAIL_LIMIT) or DEFAULT_DETAIL_LIMIT)
        chunk = full_content[offset : offset + limit]
        has_more = offset + limit < total_length

        return ExecuteResult(
            success=True,
            result=json.dumps(
                {
                    "message_index": message_index,
                    "role": message["role"],
                    "cwd": message.get("cwd"),
                    "total_length": total_length,
                    "offset": offset,
                    "limit": limit,
                    "has_more": has_more,
                    "content": chunk,
                },
                indent=2,
                ensure_ascii=False,
            ),
        )

    # start/end are 1-indexed
    start = inp.get("start", 1) or 1
    end = inp.get("end", len(messages)) or len(messages)
    search = inp.get("search")
    limit = inp.get("limit")

    start_array = to_array_index(start)
    end_array = to_array_index(end) + 1  # end is inclusive, +1 for slice

    # Range detail mode: when limit is provided, return full content
    if limit is not None:
        char_budget = min(MAX_RANGE_LIMIT, max(1, limit))

        result_items = [
            {"message": m, "ui_index": max(1, start) + i}
            for i, m in enumerate(messages[max(0, start_array) : min(len(messages), end_array)])
        ]

        if search:
            result_items = [
                item for item in result_items if search.lower() in safe_stringify(item["message"]["content"]).lower()
            ]

        parts: list[str] = []
        total_chars = 0
        included_count = 0

        for item in result_items:
            header = (
                f"=== Message {item['ui_index']} ({item['message']['role']}, cwd: {item['message'].get('cwd')}) ===\n"
            )
            body = safe_stringify(item["message"]["content"], 2)
            entry_length = len(header) + len(body) + 1

            if included_count > 0 and total_chars + entry_length > char_budget:
                break

            parts.append(header + body)
            total_chars += entry_length
            included_count += 1

        remaining_count = len(result_items) - included_count
        last_included_ui_index = result_items[included_count - 1]["ui_index"] if included_count > 0 else start
        has_more = remaining_count > 0

        summary = f"Showing {included_count} of {len(result_items)} messages ({total_chars} chars)"
        if has_more:
            summary += f" | has_more: true, next_start: {last_included_ui_index + 1}"
        summary += f" | total transcript: {len(messages)} messages"

        return ExecuteResult(
            success=True,
            result=summary + "\n\n" + "\n\n".join(parts),
        )

    # List mode (no limit): return truncated previews
    result_items = [
        {"message": m, "ui_index": max(1, start) + i}
        for i, m in enumerate(messages[max(0, start_array) : min(len(messages), end_array)])
    ]

    if search:
        result_items = [
            item for item in result_items if search.lower() in safe_stringify(item["message"]["content"]).lower()
        ]

    formatted = []
    for item in result_items:
        full_content = safe_stringify(item["message"]["content"])
        preview = full_content[:LIST_PREVIEW_LENGTH]
        truncated = len(full_content) > LIST_PREVIEW_LENGTH
        formatted.append(
            f"[{item['ui_index']}] {item['message']['role']} "
            f"(cwd: {item['message'].get('cwd')}) "
            f"({len(full_content)} chars): {preview}{'...' if truncated else ''}"
        )

    return ExecuteResult(
        success=True,
        result=f"Found {len(result_items)} messages (total: {len(messages)}):\n" + "\n".join(formatted),
    )


# ── Write Tools ───────────────────────────────────────────────────────────


def execute_write_tool(
    name: str,
    inp: dict[str, Any],
    messages: list[Message],
) -> ExecuteResult:
    new_messages = _deep_copy_messages(messages)

    if name == "insert_message":
        ui_index = inp["index"]
        role = inp["role"]
        content = inp["content"]
        cwd = inp["cwd"]

        if not isinstance(content, dict):
            return ExecuteResult(
                success=False,
                error=f"content must be a single content block dict, got {type(content).__name__}",
            )

        array_index = ui_index - 1
        if array_index < 0 or array_index > len(new_messages):
            return ExecuteResult(
                success=False,
                error=f"Invalid index: {ui_index}. Valid range: 1-{len(new_messages) + 1}",
            )

        new_messages.insert(array_index, {"role": role, "content": content, "cwd": cwd})
        return ExecuteResult(
            success=True,
            result=f"Inserted {role} message at position {ui_index}",
            messages=new_messages,
            message_op={"type": "insert", "index": ui_index},
        )

    elif name == "update_message":
        ui_index = inp["index"]
        content = inp["content"]

        if not isinstance(content, dict):
            return ExecuteResult(
                success=False,
                error=f"content must be a single content block dict, got {type(content).__name__}",
            )

        array_index = to_array_index(ui_index)
        if array_index < 0 or array_index >= len(new_messages):
            return ExecuteResult(
                success=False,
                error=f"Invalid index: {ui_index}. Valid range: 1-{len(new_messages)}",
            )

        new_messages[array_index] = {**new_messages[array_index], "content": content}
        return ExecuteResult(
            success=True,
            result=f"Updated message {ui_index}",
            messages=new_messages,
        )

    elif name == "delete_messages":
        start_ui = inp["start"]
        end_ui = inp["end"]

        if start_ui > end_ui:
            return ExecuteResult(
                success=False,
                error=f"start ({start_ui}) must be <= end ({end_ui})",
            )

        start_array = to_array_index(start_ui)
        end_array = to_array_index(end_ui)

        if start_array < 0 or end_array >= len(new_messages):
            return ExecuteResult(
                success=False,
                error=f"Invalid range: {start_ui}-{end_ui}. Valid range: 1-{len(new_messages)}",
            )

        count = end_array - start_array + 1
        del new_messages[start_array : end_array + 1]
        return ExecuteResult(
            success=True,
            result=f"Deleted {count} messages ({start_ui}-{end_ui})",
            messages=new_messages,
            message_op={"type": "delete", "start": start_ui, "end": end_ui},
        )

    elif name == "move_messages":
        from_start_ui = inp["from_start"]
        from_end_ui = inp["from_end"]
        to_ui = inp["to"]

        if from_start_ui > from_end_ui:
            return ExecuteResult(
                success=False,
                error=f"from_start ({from_start_ui}) must be <= from_end ({from_end_ui})",
            )
        if from_start_ui < 1 or from_end_ui > len(new_messages):
            return ExecuteResult(
                success=False,
                error=f"Invalid source range: {from_start_ui}-{from_end_ui}. Valid range: 1-{len(new_messages)}",
            )
        if to_ui < 1 or to_ui > len(new_messages) + 1:
            return ExecuteResult(
                success=False,
                error=f"Invalid destination: {to_ui}. Valid range: 1-{len(new_messages) + 1}",
            )
        if from_start_ui <= to_ui <= from_end_ui + 1:
            return ExecuteResult(
                success=False,
                error=f"Destination {to_ui} is within or adjacent to source range {from_start_ui}-{from_end_ui} — this would be a no-op",
            )

        from_start_arr = to_array_index(from_start_ui)
        count = from_end_ui - from_start_ui + 1
        extracted = new_messages[from_start_arr : from_start_arr + count]
        del new_messages[from_start_arr : from_start_arr + count]

        adjusted_to = to_ui
        if to_ui > from_end_ui:
            adjusted_to -= count
        insert_at = to_array_index(adjusted_to)

        for i, msg in enumerate(extracted):
            new_messages.insert(insert_at + i, msg)

        return ExecuteResult(
            success=True,
            result=f"Moved messages {from_start_ui}-{from_end_ui} to position {to_ui}",
            messages=new_messages,
            message_op={"type": "move", "fromStart": from_start_ui, "fromEnd": from_end_ui, "to": to_ui},
        )

    elif name == "replace_messages":
        start_ui = inp["start"]
        end_ui = inp["end"]
        replacements = inp["messages"]

        if start_ui > end_ui:
            return ExecuteResult(
                success=False,
                error=f"start ({start_ui}) must be <= end ({end_ui})",
            )

        start_array = to_array_index(start_ui)
        end_array = to_array_index(end_ui)

        if start_array < 0 or end_array >= len(new_messages):
            return ExecuteResult(
                success=False,
                error=f"Invalid range: {start_ui}-{end_ui}. Valid range: 1-{len(new_messages)}",
            )

        for i, msg in enumerate(replacements):
            if not isinstance(msg.get("content"), dict):
                return ExecuteResult(
                    success=False,
                    error=f"Replacement message {i}: content must be a single content block dict, got {type(msg.get('content')).__name__}",
                )

        removed_count = end_array - start_array + 1
        new_messages[start_array : end_array + 1] = replacements
        return ExecuteResult(
            success=True,
            result=f"Replaced {removed_count} messages ({start_ui}-{end_ui}) with {len(replacements)} new messages",
            messages=new_messages,
            message_op={"type": "replace", "start": start_ui, "end": end_ui, "newCount": len(replacements)},
        )

    elif name == "find_replace":
        find_str = inp.get("find", "")
        replace_str = inp.get("replace", "")
        use_regex = inp.get("regex", False)
        raw_json = inp.get("raw_json", False)
        input_key = inp.get("input_key")
        message_index = inp.get("message_index")
        start_ui = inp.get("start")
        end_ui = inp.get("end")

        if not find_str:
            return ExecuteResult(success=False, error="find string cannot be empty")

        # Build regex or use literal
        find_regex: re.Pattern | None = None
        if use_regex:
            try:
                find_regex = re.compile(find_str)
            except re.error as e:
                return ExecuteResult(success=False, error=f"Invalid regex pattern: {e}")

        # Determine range
        if message_index is not None:
            range_start = to_array_index(message_index)
            range_end = range_start
        else:
            range_start = to_array_index(start_ui) if start_ui is not None else 0
            range_end = to_array_index(end_ui) if end_ui is not None else len(new_messages) - 1

        if range_start < 0 or range_end >= len(new_messages) or range_start > range_end:
            return ExecuteResult(
                success=False,
                error=f"Invalid range. Valid: 1-{len(new_messages)}",
            )

        def replace_all(s: str) -> str:
            if find_regex:
                return find_regex.sub(replace_str, s)
            return s.replace(find_str, replace_str)

        def count_occurrences(s: str) -> int:
            if find_regex:
                return len(find_regex.findall(s))
            return s.count(find_str)

        def replace_in_object(obj: Any) -> Any:
            if isinstance(obj, str):
                return replace_all(obj)
            if isinstance(obj, list):
                return [replace_in_object(item) for item in obj]
            if isinstance(obj, dict):
                return {k: replace_in_object(v) for k, v in obj.items()}
            return obj

        total_replacements = 0
        messages_modified = 0

        for i in range(range_start, range_end + 1):
            msg = new_messages[i]
            block = msg["content"]
            new_block = dict(block)
            block_type = block.get("type")
            msg_modified = False

            if block_type == "text":
                replaced = replace_all(block["text"])
                if replaced != block["text"]:
                    total_replacements += count_occurrences(block["text"])
                    msg_modified = True
                    new_block = {**block, "text": replaced}

            elif block_type == "thinking":
                replaced = replace_all(block["thinking"])
                if replaced != block["thinking"]:
                    total_replacements += count_occurrences(block["thinking"])
                    msg_modified = True
                    new_block = {**block, "thinking": replaced}

            elif block_type == "tool_result":
                content = block["content"]
                if isinstance(content, str):
                    replaced = replace_all(content)
                    if replaced != content:
                        total_replacements += count_occurrences(content)
                        msg_modified = True
                        new_block = {**block, "content": replaced}
                else:
                    # Non-string content (e.g., list): search recursively
                    replaced_content = replace_in_object(content)
                    orig_str = safe_stringify(content)
                    new_str = safe_stringify(replaced_content)
                    if orig_str != new_str:
                        total_replacements += count_occurrences(orig_str)
                        msg_modified = True
                        new_block = {**block, "content": replaced_content}

            elif block_type == "tool_use":
                if input_key:
                    # input_key mode: operate directly on a specific string field
                    if input_key in block["input"] and isinstance(block["input"][input_key], str):
                        val = block["input"][input_key]
                        replaced = replace_all(val)
                        if replaced != val:
                            total_replacements += count_occurrences(val)
                            msg_modified = True
                            new_block = {**block, "input": {**block["input"], input_key: replaced}}
                elif raw_json:
                    # In raw_json mode, search operates on JSON-serialized text.
                    # Auto-escape find/replace strings so control characters
                    # (\t, \n, etc.) match their JSON representations.
                    # For regex mode, the user is responsible for matching JSON text.
                    orig_str = json.dumps(block["input"], ensure_ascii=False)
                    if find_regex:
                        new_str = find_regex.sub(replace_str, orig_str)
                        raw_count = len(find_regex.findall(orig_str))
                    else:
                        json_find = _json_escape_string(find_str)
                        json_replace = _json_escape_string(replace_str)
                        raw_count = orig_str.count(json_find)
                        new_str = orig_str.replace(json_find, json_replace)
                    if new_str != orig_str:
                        try:
                            parsed = json.loads(new_str)
                        except json.JSONDecodeError:
                            return ExecuteResult(
                                success=False,
                                error=f"raw_json replacement produced invalid JSON. Original: {orig_str[:100]}... Result: {new_str[:100]}...",
                            )
                        total_replacements += raw_count
                        msg_modified = True
                        new_block = {**block, "input": parsed}
                else:
                    replaced_input = replace_in_object(block["input"])
                    orig_str = json.dumps(block["input"], ensure_ascii=False)
                    new_str = json.dumps(replaced_input, ensure_ascii=False)
                    if orig_str != new_str:
                        total_replacements += count_occurrences(orig_str)
                        msg_modified = True
                        new_block = {**block, "input": replaced_input}

            if msg_modified:
                new_messages[i] = {**msg, "content": new_block}
                messages_modified += 1

        if total_replacements == 0:
            return ExecuteResult(
                success=False,
                error=f'No occurrences of "{find_str}" found in the specified range',
            )

        if message_index is not None:
            range_desc = f"message {message_index}"
        elif start_ui is not None:
            range_desc = f"messages {start_ui}-{end_ui or len(new_messages)}"
        else:
            range_desc = "entire transcript"

        s_repl = "" if total_replacements == 1 else "s"
        s_msgs = "" if messages_modified == 1 else "s"
        regex_tag = " [regex]" if use_regex else ""
        return ExecuteResult(
            success=True,
            result=f"Replaced {total_replacements} occurrence{s_repl} in {messages_modified} message{s_msgs} ({range_desc}){regex_tag}",
            messages=new_messages,
        )

    elif name == "update_tool_content":
        ui_message_index = inp["message_index"]
        direct_content = inp.get("content")
        source_message = inp.get("source_message")

        array_index = to_array_index(ui_message_index)
        if array_index < 0 or array_index >= len(new_messages):
            return ExecuteResult(
                success=False,
                error=f"Invalid message index: {ui_message_index}. Valid range: 1-{len(new_messages)}",
            )

        message = new_messages[array_index]
        block = message["content"]
        if block.get("type") != "tool_use":
            return ExecuteResult(success=False, error=f"Message {ui_message_index} is not a tool_use (got {block.get('type')})")

        if source_message is not None:
            src_array_index = to_array_index(source_message)
            if src_array_index < 0 or src_array_index >= len(new_messages):
                return ExecuteResult(
                    success=False,
                    error=f"Invalid source message index: {source_message}. Valid range: 1-{len(new_messages)}",
                )
            src_block = new_messages[src_array_index]["content"]

            if src_block.get("type") == "tool_use":
                src_input = src_block.get("input", {})
                content = src_input.get("content") if isinstance(src_input, dict) else None
                if content is None:
                    content = json.dumps(src_input, indent=2, ensure_ascii=False)
            elif src_block.get("type") == "tool_result":
                content = src_block["content"]
            else:
                return ExecuteResult(
                    success=False,
                    error=f'Source message is type "{src_block.get("type")}" — expected tool_use or tool_result',
                )

            # Strip line numbers from Read tool results
            lines = content.split("\n")
            line_number_pattern = re.compile(r"^\s*\d+\t")
            if len(lines) > 1 and all(line_number_pattern.match(line) for line in lines if line.strip()):
                content = "\n".join(line_number_pattern.sub("", line) for line in lines)

        elif direct_content is not None:
            content = direct_content
        else:
            return ExecuteResult(
                success=False,
                error="Either content or source_message must be provided",
            )

        new_block = {
            **block,
            "input": {**block["input"], "content": content},
        }
        new_messages[array_index] = {**message, "content": new_block}

        source_ref = (
            f" (copied from message {source_message})" if source_message is not None else ""
        )
        return ExecuteResult(
            success=True,
            result=f"Updated tool content at message {ui_message_index}{source_ref}",
            messages=new_messages,
        )

    else:
        return ExecuteResult(success=False, error=f"Unknown write tool: {name}")


# ── Unified Entry Point ──────────────────────────────────────────────────


def execute_tool(
    name: str,
    inp: dict[str, Any],
    messages: list[Message],
) -> ExecuteResult:
    """Execute a transcript tool. Dispatches to read or write handler."""
    if is_read_tool(name):
        return execute_read_tool(name, inp, messages)
    if is_write_tool(name):
        return execute_write_tool(name, inp, messages)
    raise ValueError(f"Unknown tool type: {name}. Expected read or write tool.")
