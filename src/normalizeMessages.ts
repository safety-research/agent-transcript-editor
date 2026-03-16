import type { Message, ContentBlock as ContentBlockType } from './types';

/** Normalize minimized JSONL entries into Message[].
 *  Expects already-minimized format: {role, content: ContentBlock[], cwd?}.
 *  Content MUST be an array of content blocks, not a string.
 *  For full Claude Code format, use the backend /api/tools/minimize endpoint first.
 */
export function normalizeMessages(raw: unknown[]): Message[] {
  return raw.flatMap((entry, idx) => {
    const obj = entry as Record<string, unknown>;

    const role = obj.role as string | undefined;
    if (role !== 'user' && role !== 'assistant') return [];

    if (!Array.isArray(obj.content)) {
      throw new Error(`Line ${idx}: content must be an array, got ${typeof obj.content}`);
    }

    return [{
      role: role as 'user' | 'assistant',
      content: obj.content as ContentBlockType[],
      cwd: (obj.cwd as string) ?? '',
    }];
  });
}
