import type { Message, ContentBlock as ContentBlockType } from './types';

/** Normalize minimized JSONL entries into Message[].
 *  Expects already-minimized format: {role, content, cwd?}.
 *  For full Claude Code format, use the backend /api/tools/minimize endpoint first.
 *  Also normalizes string content to [{type: 'text', text: content}].
 */
export function normalizeMessages(raw: unknown[]): Message[] {
  return raw.flatMap(entry => {
    const obj = entry as Record<string, unknown>;

    const role = obj.role as string | undefined;
    if (role !== 'user' && role !== 'assistant') return [];

    // Normalize string content to array
    const content = typeof obj.content === 'string'
      ? [{ type: 'text' as const, text: obj.content }]
      : Array.isArray(obj.content) ? (obj.content as ContentBlockType[])
      : [{ type: 'text' as const, text: String(obj.content ?? '') }];

    return [{
      role: role as 'user' | 'assistant',
      content,
      cwd: (obj.cwd as string) ?? '',
    }];
  });
}
