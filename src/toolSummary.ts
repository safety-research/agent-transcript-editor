/**
 * Smart summary extraction for tool_use blocks.
 * Used by both ContentBlock (display) and App (search indexing).
 *
 * `summary`: human-readable one-liner for the header
 * `isComplete`: if true, the summary captures all meaningful info — no expand needed
 */
export function getToolSummary(
  name: string,
  input: Record<string, unknown>
): { summary: string; isComplete: boolean } {
  switch (name) {
    case 'Read':
      return { summary: String(input.file_path || ''), isComplete: true };

    case 'Glob': {
      const p = input.path ? ` in ${input.path}` : '';
      return { summary: `${input.pattern}${p}`, isComplete: true };
    }

    case 'Grep': {
      const p = input.path ? ` in ${input.path}` : '';
      const g = input.glob ? ` (${input.glob})` : '';
      return { summary: `"${input.pattern}"${p}${g}`, isComplete: true };
    }

    case 'Bash': {
      const cmd = String(input.command || '');
      const desc = input.description ? String(input.description) : '';
      const isShort = cmd.length <= 100 && !cmd.includes('\n');
      if (isShort) {
        return { summary: cmd, isComplete: true };
      }
      return { summary: desc || cmd.slice(0, 100) + '…', isComplete: false };
    }

    case 'Edit':
      return { summary: String(input.file_path || ''), isComplete: false };

    case 'Write':
      return { summary: String(input.file_path || ''), isComplete: false };

    default: {
      // Unknown tool — show flat if the JSON is short enough
      const json = JSON.stringify(input);
      if (json.length <= 120 && !json.includes('\n')) {
        return { summary: json, isComplete: true };
      }
      return { summary: '', isComplete: false };
    }
  }
}

/**
 * For tool_result blocks: determine if content is short enough to show inline.
 */
export function isShortContent(text: string): boolean {
  const lines = text.split('\n').length;
  return text.length <= 200 && lines <= 3;
}
