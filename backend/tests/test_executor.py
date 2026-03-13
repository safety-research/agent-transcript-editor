"""Unit tests for executor.py — pure functions operating on transcript messages."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import (
    execute_read_tool,
    execute_write_tool,
    remap_mechanism_keys,
)

from tests.conftest import SAMPLE_MESSAGES


@pytest.fixture
def messages():
    """Fresh deep copy of sample messages for each test."""
    return copy.deepcopy(SAMPLE_MESSAGES)


# ── insert_message ───────────────────────────────────────────────────────


class TestInsertMessage:
    def test_insert_at_beginning(self, messages):
        result = execute_write_tool(
            "insert_message",
            {
                "index": 1,
                "role": "user",
                "content": [{"type": "text", "text": "New first message"}],
                "cwd": "/home/user/project",
            },
            messages,
        )
        assert result.success
        assert result.messages is not None
        assert len(result.messages) == len(messages) + 1
        assert result.messages[0]["content"][0]["text"] == "New first message"
        assert result.message_op == {"type": "insert", "index": 1}

    def test_insert_at_end(self, messages):
        result = execute_write_tool(
            "insert_message",
            {
                "index": len(messages) + 1,
                "role": "assistant",
                "content": [{"type": "text", "text": "Appended message"}],
                "cwd": "/home/user/project",
            },
            messages,
        )
        assert result.success
        assert result.messages is not None
        assert len(result.messages) == len(messages) + 1
        assert result.messages[-1]["content"][0]["text"] == "Appended message"

    def test_insert_in_middle(self, messages):
        result = execute_write_tool(
            "insert_message",
            {
                "index": 3,
                "role": "user",
                "content": [{"type": "text", "text": "Middle message"}],
                "cwd": "/home/user/project",
            },
            messages,
        )
        assert result.success
        assert result.messages[2]["content"][0]["text"] == "Middle message"
        # Original message 3 should now be at index 3 (0-indexed)
        assert result.messages[3]["role"] == messages[2]["role"]

    def test_insert_invalid_index(self, messages):
        result = execute_write_tool(
            "insert_message",
            {
                "index": 0,
                "role": "user",
                "content": [{"type": "text", "text": "Bad"}],
                "cwd": "/home/user/project",
            },
            messages,
        )
        assert not result.success
        assert "Invalid index" in result.error

    def test_insert_does_not_mutate_original(self, messages):
        original_len = len(messages)
        execute_write_tool(
            "insert_message",
            {
                "index": 1,
                "role": "user",
                "content": [{"type": "text", "text": "New"}],
                "cwd": "/home/user/project",
            },
            messages,
        )
        assert len(messages) == original_len


# ── delete_messages ──────────────────────────────────────────────────────


class TestDeleteMessages:
    def test_delete_single(self, messages):
        result = execute_write_tool("delete_messages", {"start": 2, "end": 2}, messages)
        assert result.success
        assert len(result.messages) == len(messages) - 1
        assert result.message_op == {"type": "delete", "start": 2, "end": 2}

    def test_delete_range(self, messages):
        result = execute_write_tool("delete_messages", {"start": 1, "end": 3}, messages)
        assert result.success
        assert len(result.messages) == len(messages) - 3

    def test_delete_all(self, messages):
        result = execute_write_tool("delete_messages", {"start": 1, "end": len(messages)}, messages)
        assert result.success
        assert len(result.messages) == 0

    def test_delete_invalid_range(self, messages):
        result = execute_write_tool("delete_messages", {"start": 3, "end": 2}, messages)
        assert not result.success

    def test_delete_out_of_bounds(self, messages):
        result = execute_write_tool("delete_messages", {"start": 1, "end": len(messages) + 1}, messages)
        assert not result.success


# ── move_messages ────────────────────────────────────────────────────────


class TestMoveMessages:
    def test_move_forward(self, messages):
        # Move message 1 to after message 3 (position 4)
        result = execute_write_tool("move_messages", {"from_start": 1, "from_end": 1, "to": 4}, messages)
        assert result.success
        assert result.messages[0]["role"] == messages[1]["role"]
        assert result.messages[2]["role"] == messages[0]["role"]

    def test_move_backward(self, messages):
        # Move message 4 to position 1
        result = execute_write_tool("move_messages", {"from_start": 4, "from_end": 4, "to": 1}, messages)
        assert result.success
        assert result.messages[0]["role"] == messages[3]["role"]

    def test_move_range(self, messages):
        # Move messages 2-3 to position 1
        result = execute_write_tool("move_messages", {"from_start": 2, "from_end": 3, "to": 1}, messages)
        assert result.success
        assert len(result.messages) == len(messages)

    def test_move_noop_rejected(self, messages):
        # Moving to adjacent position is a no-op
        result = execute_write_tool("move_messages", {"from_start": 2, "from_end": 2, "to": 2}, messages)
        assert not result.success


# ── find_replace ─────────────────────────────────────────────────────────


class TestFindReplace:
    def test_literal_replace(self, messages):
        result = execute_write_tool(
            "find_replace",
            {"find": "auth module", "replace": "authentication system"},
            messages,
        )
        assert result.success
        # Check the replacement happened in the user message
        first_msg = result.messages[0]
        assert "authentication system" in first_msg["content"][0]["text"]

    def test_regex_replace(self, messages):
        result = execute_write_tool(
            "find_replace",
            {"find": r"def \w+\(", "replace": "def authenticate(", "regex": True},
            messages,
        )
        assert result.success

    def test_no_match_is_error(self, messages):
        result = execute_write_tool(
            "find_replace",
            {"find": "nonexistent string xyz123", "replace": "something"},
            messages,
        )
        assert not result.success
        assert "No occurrences" in result.error

    def test_empty_find_rejected(self, messages):
        result = execute_write_tool("find_replace", {"find": "", "replace": "something"}, messages)
        assert not result.success

    def test_scoped_to_single_message(self, messages):
        result = execute_write_tool(
            "find_replace",
            {"find": "auth", "replace": "AUTH", "message_index": 1},
            messages,
        )
        assert result.success
        # Only first message should be modified
        assert "AUTH" in result.messages[0]["content"][0]["text"]
        # Message 2 should be unchanged (has "auth" in thinking)
        assert "auth" in result.messages[1]["content"][0]["thinking"]

    def test_replaces_in_tool_use_input(self, messages):
        result = execute_write_tool(
            "find_replace",
            {"find": "auth.py", "replace": "authentication.py"},
            messages,
        )
        assert result.success
        # Check tool_use input was updated
        tool_use_block = result.messages[1]["content"][1]
        assert tool_use_block["input"]["file_path"] == "/home/user/project/authentication.py"

    def test_raw_json_escapes_find_string(self):
        """raw_json mode should auto-escape control chars in find/replace strings."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01X",
                        "name": "Write",
                        "input": {"file_path": "/test.py", "content": "line1\tindented\nline2\tmore"},
                    }
                ],
                "cwd": "/test",
            }
        ]
        # The JSON-serialized input has \t and \n as escape sequences.
        # The find string "line1\tindented" has a real tab after JSON parsing.
        # raw_json should auto-escape it to match the JSON representation.
        result = execute_write_tool(
            "find_replace",
            {
                "find": "line1\tindented",
                "replace": "line1\tREPLACED",
                "raw_json": True,
                "message_index": 1,
            },
            messages,
        )
        assert result.success
        assert result.messages[0]["content"][0]["input"]["content"] == "line1\tREPLACED\nline2\tmore"

    def test_raw_json_plain_text_still_works(self):
        """raw_json should still match plain text without special characters."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01X",
                        "name": "Bash",
                        "input": {"command": "echo 192.0"},
                    }
                ],
                "cwd": "/test",
            }
        ]
        result = execute_write_tool(
            "find_replace",
            {"find": "192.0", "replace": "193.0", "raw_json": True, "message_index": 1},
            messages,
        )
        assert result.success
        assert result.messages[0]["content"][0]["input"]["command"] == "echo 193.0"

    def test_find_replace_non_string_tool_result(self):
        """find_replace should handle non-string tool_result content without crashing."""
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01X",
                        "content": [{"type": "text", "text": "Score: 192.0"}],
                    }
                ],
                "cwd": "/test",
            }
        ]
        result = execute_write_tool(
            "find_replace",
            {"find": "192.0", "replace": "193.0", "message_index": 1},
            messages,
        )
        assert result.success
        assert result.messages[0]["content"][0]["content"][0]["text"] == "Score: 193.0"


# ── remap_mechanism_keys ─────────────────────────────────────────────────


class TestRemapMechanismKeys:
    def test_insert_shifts_keys(self):
        d = {1: "step1", 3: "step3", 5: "step5"}
        result = remap_mechanism_keys(d, {"type": "insert", "index": 3})
        assert result == {1: "step1", 4: "step3", 6: "step5"}

    def test_delete_shifts_keys(self):
        d = {1: "step1", 3: "step3", 5: "step5"}
        result = remap_mechanism_keys(d, {"type": "delete", "start": 2, "end": 3})
        assert result == {1: "step1", 3: "step5"}

    def test_delete_removes_keys_in_range(self):
        d = {1: "step1", 2: "step2", 3: "step3"}
        result = remap_mechanism_keys(d, {"type": "delete", "start": 2, "end": 2})
        assert result == {1: "step1", 2: "step3"}

    def test_replace_adjusts_keys(self):
        d = {1: "step1", 4: "step4", 6: "step6"}
        # Replace messages 2-3 with 5 new messages
        result = remap_mechanism_keys(d, {"type": "replace", "start": 2, "end": 3, "newCount": 5})
        # Keys in range (2,3) removed, keys after shifted by +3 (5-2)
        assert result == {1: "step1", 7: "step4", 9: "step6"}

    def test_none_input(self):
        assert remap_mechanism_keys(None, {"type": "insert", "index": 1}) is None

    def test_empty_dict(self):
        assert remap_mechanism_keys({}, {"type": "insert", "index": 1}) == {}


# ── get_messages ─────────────────────────────────────────────────────────


class TestGetMessages:
    def test_list_mode_default(self, messages):
        result = execute_read_tool("get_messages", {}, messages)
        assert result.success
        assert f"Found {len(messages)} messages" in result.result

    def test_list_mode_with_range(self, messages):
        result = execute_read_tool("get_messages", {"start": 1, "end": 2}, messages)
        assert result.success
        assert "Found 2 messages" in result.result

    def test_detail_mode_single(self, messages):
        result = execute_read_tool("get_messages", {"message_index": 1}, messages)
        assert result.success
        data = json.loads(result.result)
        assert data["message_index"] == 1
        assert data["role"] == "user"

    def test_detail_mode_invalid_index(self, messages):
        result = execute_read_tool("get_messages", {"message_index": 999}, messages)
        assert not result.success
        assert "Invalid message index" in result.error

    def test_search_mode(self, messages):
        result = execute_read_tool("get_messages", {"search": "refactor"}, messages)
        assert result.success
        # Should find the user message containing "refactor"
        assert "Found" in result.result

    def test_range_detail_mode(self, messages):
        result = execute_read_tool("get_messages", {"start": 1, "end": 2, "limit": 5000}, messages)
        assert result.success
        assert "Message 1" in result.result
        assert "Message 2" in result.result
