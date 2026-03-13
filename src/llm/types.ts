export const WRITE_TOOLS = ['insert_message', 'update_message', 'delete_messages', 'move_messages', 'replace_messages', 'update_tool_content', 'find_replace'] as const;

// Streaming content block - preserves order of text, thinking, and tool calls
export type StreamingBlock =
  | { type: 'text'; text: string }
  | { type: 'thinking'; text: string; signature?: string }
  | { type: 'tool_call'; id: string; name: string; input: Record<string, unknown>; result?: string }
  | { type: 'user_message'; text: string };

// Session state for UI rendering (source of truth is the backend Session)
export interface LLMSession {
  contentBlocks: StreamingBlock[];
  hitLimit: boolean;
  isStreaming?: boolean;
  error?: string;
  statusMessage?: string; // Transient status (rate limited, retrying, etc.)
}
