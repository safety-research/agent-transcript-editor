# Transcript Editor

A web-based editor for AI conversation transcripts with AI-assisted editing and integrated evaluation via a trusted monitor.

Built for red-teaming research: craft attack transcripts, score them for suspiciousness, and iterate to find monitor blind spots.

## Quick Start

### Option 1: start.sh (recommended)

```bash
./start.sh
```

The script auto-creates a Python venv, installs dependencies, and starts both servers. Edit `backend/.env` to add your `ANTHROPIC_API_KEY`.

### Option 2: Docker

```bash
cp backend/.env.example backend/.env
# Edit backend/.env to add your ANTHROPIC_API_KEY
docker-compose up
```

### Option 3: Manual

```bash
# Terminal 1: Backend
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit to add API key
python main.py

# Terminal 2: Frontend
npm install
npm run dev
```

Open http://localhost:5173 in your browser.

## Features

### Transcript Editing
- Load, edit, and save JSONL transcripts organized by project
- Insert, delete, reorder, and modify messages
- Edit tool call inputs and results directly
- Auto-detects and minimizes full Claude Code format on load
- Auto-fixes non-standard tool IDs to realistic format

### AI-Assisted Editing
- Natural language instructions to modify transcripts
- Agent reads transcript structure and makes targeted edits
- Fork transcripts into variants to try different approaches in parallel
- Configurable system prompts (CREATIVE.md, FAITHFUL.md, or custom)

### Trusted Monitor Integration
- Score transcripts for suspiciousness (0-100) across multiple prompt variants
- Evaluate egregiousness, incriminating evidence, effectiveness, confidence, realism
- Cached scoring with automatic invalidation on transcript changes
- Real-time progress via SSE streaming

### File Consistency Checking
- Virtual filesystem validation: detects Read/Write/Edit mismatches
- Suppressible warnings for known false positives

## Configuration

### Backend (`backend/.env`)

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...

# Optional
ANTHROPIC_API_KEY_ALT=sk-ant-...     # Alternate key for load balancing
DEFAULT_MODEL=claude-opus-4-6        # Default model for agent + token counting
TRANSCRIPTS_DIR=./transcripts        # Transcript storage directory
HOST=127.0.0.1                       # Server bind address
PORT=8000                            # Server port
CORS_ORIGINS=http://localhost:5173   # Comma-separated allowed origins
```

### Settings UI

Access via the gear icon. Configurable at runtime:
- **Model selection**: Choose model and API key per session
- **Prompt mode**: Creative (strategic guidance), Faithful (faithful execution), or both
- **Creative document**: Select which .md file to use as agent system prompt
- **Monitor settings**: Number of evaluations, prompt variants, enabled metrics
- **Rate limits**: Per-key TPM limits
- **Child lock**: Prevent edits after monitor scoring

## Transcript Format

Minimal JSONL — one JSON object per line:

```json
{"role": "user", "content": [{"type": "text", "text": "Fix the failing test"}], "cwd": "/home/user/project"}
{"role": "assistant", "content": [{"type": "thinking", "thinking": "Let me look at the test..."}, {"type": "tool_use", "id": "toolu_01ABC...", "name": "Read", "input": {"file_path": "test.py"}}]}
{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01ABC...", "content": "def test_foo():..."}]}
```

See [TRANSCRIPT_FORMAT.md](TRANSCRIPT_FORMAT.md) for the full specification.

## Architecture

```
Frontend (React 19 + TypeScript + Zustand)
    │
    ├── SSE stream (/api/session/{file}/events)
    │   └── Real-time: agent streaming, monitor progress, rate limit status
    │
    └── HTTP commands
        ├── /api/session/{file}/submit  - Start agent
        ├── /api/session/{file}/stop    - Stop agent
        ├── /api/files/*               - File CRUD + metadata
        └── /api/monitor/*             - Evaluation triggers + polling

Backend (FastAPI + AsyncAnthropic)
    │
    ├── Sessions: one per file, multiple subscribers (UI + CLI)
    ├── Agent loop: streaming API calls, tool execution, rate limiting
    ├── Monitor: concurrent metric evaluation, preprocessing cache
    └── Disk: .jsonl (transcript) + .meta.json (scores) + .chat.json (agent state)
```

## API Endpoints

### Health & Config
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Backend status, API keys, monitor, sessions, rate limits |
| `/api/config/rate-limit` | GET/PUT | Per-key TPM rate limit config |

### Files (`/api/files`)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/projects` | GET | List projects with file counts |
| `/list?project=...` | GET | List transcripts in project |
| `/load/{path}` | GET | Load transcript (auto-minimizes, auto-fixes IDs) |
| `/save` | POST | Save transcript |
| `/upload` | POST | Upload .jsonl file |
| `/delete/{path}` | DELETE | Delete transcript + sidecars |
| `/rename` | POST | Rename file atomically |
| `/move` | POST | Move between projects |
| `/duplicate` | POST | Copy file + metadata |
| `/meta` | POST | Write/merge sidecar metadata |


### Session (`/api/session`)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/{file}/events` | GET | SSE stream (agent events, monitor progress) |
| `/{file}/submit` | POST | Submit prompt to agent |
| `/{file}/stop` | POST | Stop running agent |
| `/{file}/reset` | POST | Clear chat history |
| `/{file}/state` | GET | Get current session state |
| `/{file}/settings` | PUT | Per-session model/prompt config |
| `/{file}/messages` | POST | Update messages (from frontend edits) |
| `/{file}/metadata` | POST | Update outcome/scenario/mechanism |
| `/global-settings` | GET/PUT | Global editor configuration |

### Monitor (`/api/monitor`)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/evaluate-file` | POST | Start multi-metric evaluation |
| `/evaluate-file/{id}` | GET | Poll evaluation progress |
| `/token-count` | POST | Count tokens in preprocessed transcript |
| `/status` | GET | Check trusted-monitor availability |

## AI Agent Tools

The agent has access to these tools for transcript manipulation:

| Tool | Description |
|------|-------------|
| `get_messages` | Read messages (list, detail, block, search modes) |
| `insert_message` | Insert new message at position |
| `update_message` | Replace message content |
| `delete_messages` | Remove message range |
| `move_messages` | Reorder messages |
| `replace_messages` | Bulk replace message range |
| `update_tool_content` | Edit specific tool_use input fields |
| `find_replace` | Search and replace across transcript |
| `fork_to_new_tab` | Create variant transcript |
| `set_transcript_metadata` | Set outcome/scenario/mechanism |
| `get_monitor_score` | Trigger and retrieve monitor evaluation |
| `count_tokens` | Count tokens in transcript |
| `check_consistency` | Run file consistency validation |
| `finish_editing` | Signal editing complete |

## CLI Client

For programmatic access (e.g., from Claude Code):

```bash
cd backend
./venv/bin/python cli_client.py PROJECT/FILE.jsonl --prompt "Make the attack stealthier"
```

## Development

```bash
# Backend (auto-reload)
cd backend && python main.py

# Frontend (hot reload)
npm run dev

# Type check
npx tsc --noEmit

# Tests
cd backend && pytest tests/

# Production build
npm run build
```

## Dependencies

**Backend**: FastAPI, Anthropic SDK, aiofiles, uvicorn, trusted-monitor
**Frontend**: React 19, Zustand, Vite, TypeScript, react-markdown
