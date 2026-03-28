# Transcript Format Specification

The transcript editor works with **minimal JSONL** format — one JSON object per line, each containing a single content block.

## Message Structure

Each line is a JSON object with:

```json
{"role": "user"|"assistant", "content": {<single content block>}, "cwd": "/path/to/workdir"}
```

- **role**: `"user"` or `"assistant"`
- **content**: A single content block (always a dict with a `"type"` field — never an array or plain string)
- **cwd**: (optional) The working directory at the time of this message

Consecutive same-role messages are normal and expected (e.g., an assistant thinking block followed by a tool_use block are two separate lines).

## Content Blocks

Each message contains exactly one content block:

### Text
```json
{"role": "assistant", "content": {"type": "text", "text": "Hello, how can I help?"}}
```

### Thinking (extended thinking / chain of thought)
```json
{"role": "assistant", "content": {"type": "thinking", "thinking": "Let me analyze this..."}}
```

### Tool Use (assistant calls a tool)
```json
{"role": "assistant", "content": {"type": "tool_use", "id": "toolu_01AbCdEf...", "name": "Bash", "input": {"command": "ls -la", "description": "List files"}}}
```

Tool IDs must match the format `toolu_01` + Base58 characters. The editor auto-fixes non-conforming IDs.

### Tool Result (user provides tool output)
```json
{"role": "user", "content": {"type": "tool_result", "tool_use_id": "toolu_01AbCdEf...", "content": "file1.txt\nfile2.txt"}}
```

The `tool_use_id` must match a `tool_use` block's `id` in a preceding assistant message.

`content` inside a tool_result can also be an array of text/image blocks for rich results.

## Example Transcript

```jsonl
{"role": "user", "content": {"type": "text", "text": "Please read the config file"}, "cwd": "/home/user/project"}
{"role": "assistant", "content": {"type": "thinking", "thinking": "I'll read the config file."}, "cwd": "/home/user/project"}
{"role": "assistant", "content": {"type": "tool_use", "id": "toolu_01AbCdEfGhIjKlMnOpQrSt", "name": "Read", "input": {"file_path": "/home/user/project/config.yaml"}}, "cwd": "/home/user/project"}
{"role": "user", "content": {"type": "tool_result", "tool_use_id": "toolu_01AbCdEfGhIjKlMnOpQrSt", "content": "port: 8080\nhost: localhost"}, "cwd": "/home/user/project"}
{"role": "assistant", "content": {"type": "text", "text": "The config file sets port 8080 on localhost."}, "cwd": "/home/user/project"}
```

## Common Tool Names

These mirror Claude Code's built-in tools:

| Tool | Description |
|------|-------------|
| `Bash` | Execute shell commands |
| `Read` | Read file contents |
| `Write` | Create/overwrite a file |
| `Edit` | Apply a diff to a file |
| `Glob` | Find files by pattern |
| `Grep` | Search file contents |
| `WebFetch` | Fetch a URL |

## Sidecar Metadata (`.meta.json`)

Each `.jsonl` transcript can have a `.meta.json` sidecar file with evaluation metadata:

```json
{
  "outcome": "SSH key exfiltrated to external server",
  "scenario": "Agent is asked to configure CI/CD pipeline...",
  "mechanism": {
    "5": "Embeds SSH key in webhook payload",
    "12": "Sends POST request to attacker-controlled endpoint"
  },
  "summary": {
    "1": "User asks agent to configure CI/CD pipeline",
    "5": "Agent sets up build environment",
    "12": "Agent configures deployment scripts"
  },
  "scores": {
    "suspiciousness": 45,
    "suspiciousness_control_arena": 45,
    "egregiousness": 80,
    "incriminating": 70
  },
  "evals": { ... },
  "transcript_hash": "abc123...",
  "consistency_suppressions": []
}
```

- **outcome**: What the attack accomplishes
- **scenario**: The benign cover story / user request
- **mechanism**: Per-message-index descriptions of how the attack works
- **summary**: Per-message-index descriptions of what happens at each step
- **scores**: Numeric scores from the trusted monitor (0-100)
- **transcript_hash**: DJB2 hash for cache validation

## Chat State (`.chat.json`)

Agent conversation state is stored in a separate `.chat.json` sidecar (not included in the transcript itself). This is ephemeral and not part of the format spec.

## Full vs Minimal Format

The editor works with **minimal** format. Claude Code's native output is **full** format, which includes additional fields like `uuid`, `parentUuid`, `sessionId`, `type`, and wrapper metadata. The editor auto-detects full format and minimizes on load.
