import type { ReactNode } from 'react';

export function highlightText(
  text: string,
  query: string,
  startIndex: number,
  activeMatchIndex: number
): { elements: ReactNode[]; matchCount: number } {
  if (!query || query.length === 0) {
    return { elements: [text], matchCount: 0 };
  }

  const escapedQuery = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(escapedQuery, 'gi');
  const elements: ReactNode[] = [];
  let lastIndex = 0;
  let matchCount = 0;

  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    // Add text before this match
    if (match.index > lastIndex) {
      elements.push(text.slice(lastIndex, match.index));
    }

    const globalIndex = startIndex + matchCount;
    const isActive = globalIndex === activeMatchIndex;

    elements.push(
      <mark
        key={`match-${globalIndex}`}
        data-match-index={globalIndex}
        className={isActive ? 'active' : undefined}
      >
        {match[0]}
      </mark>
    );

    lastIndex = match.index + match[0].length;
    matchCount++;
  }

  // Add remaining text after last match
  if (lastIndex < text.length) {
    elements.push(text.slice(lastIndex));
  }

  if (elements.length === 0) {
    elements.push(text);
  }

  return { elements, matchCount };
}
