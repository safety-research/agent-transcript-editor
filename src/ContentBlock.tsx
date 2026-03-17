import { useState, useEffect, useMemo } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ContentBlock as ContentBlockType, ToolUseBlock } from './types';
import { highlightText } from './highlightText';
import { StringModal } from './StringModal';
import { getToolSummary, isShortContent } from './toolSummary';

interface Props {
  block: ContentBlockType;
  onUpdate?: (block: ContentBlockType) => void;
  searchQuery: string;
  matchIndexOffset: number;
  activeMatchIndex: number;
  blockMatchCount: number;
}

function renderHighlighted(text: string, query: string, offset: number, activeIndex: number) {
  const { elements, matchCount } = highlightText(text, query, offset, activeIndex);
  return { rendered: <>{elements}</>, matchCount };
}

export function ContentBlockComponent({ block, onUpdate, searchQuery, matchIndexOffset, activeMatchIndex, blockMatchCount }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [showStringModal, setShowStringModal] = useState(false);

  // Detect string values in tool_use input worth expanding (multiline or long)
  const expandableFields = useMemo(() => {
    if (block.type !== 'tool_use') return [];
    const fields: Array<{ key: string; value: string }> = [];
    const input = block.input as Record<string, unknown>;
    for (const [key, val] of Object.entries(input)) {
      if (typeof val === 'string' && (val.includes('\n') || val.length > 80)) {
        fields.push({ key, value: val });
      }
    }
    return fields;
  }, [block]);

  // Auto-expand when the active match is inside this collapsed block
  useEffect(() => {
    if (blockMatchCount > 0 && activeMatchIndex >= matchIndexOffset && activeMatchIndex < matchIndexOffset + blockMatchCount) {
      setExpanded(true);
    }
  }, [activeMatchIndex, matchIndexOffset, blockMatchCount]);

  if (block.type === 'text') {
    if (searchQuery) {
      // During search, use plain text with highlighting for accurate match tracking
      const { rendered, matchCount } = renderHighlighted(block.text, searchQuery, matchIndexOffset, activeMatchIndex);
      return <div className="text-block text-block-plain" data-match-count={matchCount}>{rendered}</div>;
    }
    return (
      <div className="text-block text-block-markdown">
        <Markdown remarkPlugins={[remarkGfm]}>{block.text}</Markdown>
      </div>
    );
  }

  if (block.type === 'thinking') {
    const text = block.thinking;
    const { rendered, matchCount } = renderHighlighted(text, searchQuery, matchIndexOffset, activeMatchIndex);
    const short = isShortContent(text);

    if (short) {
      return (
        <div className="thinking-block" data-match-count={matchCount}>
          <div className="block-header">
            <span className="label">Thinking</span>
          </div>
          <div className="block-content text-block-markdown"><Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown></div>
        </div>
      );
    }

    return (
      <div className="thinking-block" data-match-count={matchCount}>
        <div className="block-header" onClick={() => setExpanded(!expanded)}>
          <span className="chevron">{expanded ? '▼' : '▶'}</span>
          <span className="label">Thinking</span>
          {!expanded && <span className="preview">{text.slice(0, 80)}...</span>}
        </div>
        {expanded && (
          searchQuery
            ? <pre className="block-content">{rendered}</pre>
            : <div className="block-content text-block-markdown"><Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown></div>
        )}
      </div>
    );
  }

  if (block.type === 'tool_use') {
    const isWrite = block.name === 'Write';
    const writeContent = isWrite ? (block.input as { content?: string }).content : null;
    const writeFilePath = isWrite ? ((block.input as { file_path?: string }).file_path ?? '') : '';
    const toolInput = block.input as Record<string, unknown>;
    const { summary, isComplete } = getToolSummary(block.name, toolInput);

    // For Write blocks, highlight file path (in header) and content separately.
    // Match offsets stay consistent with getSearchableText (filePath + '\n' + content).
    const isWriteWithContent = isWrite && writeContent;
    const pathHighlight = isWriteWithContent
      ? renderHighlighted(writeFilePath, searchQuery, matchIndexOffset, activeMatchIndex)
      : null;
    // +1 for the '\n' separator in getSearchableText that we don't render
    const contentOffset = matchIndexOffset + (pathHighlight ? pathHighlight.matchCount : 0);

    // For complete inline tools, search text is the summary; for others, it's the full display text
    const displayText = isWriteWithContent ? writeContent : (isComplete ? summary : JSON.stringify(block.input, null, 2));
    const { rendered, matchCount: contentMatchCount } = renderHighlighted(displayText, searchQuery, contentOffset, activeMatchIndex);
    const matchCount = (pathHighlight?.matchCount ?? 0) + contentMatchCount;

    const handleEditWrite = () => {
      if (writeContent !== undefined) {
        setEditValue(writeContent || '');
        setEditing(true);
        setExpanded(true);
      }
    };

    const handleSave = () => {
      if (onUpdate && isWrite) {
        onUpdate({
          ...block,
          input: { ...block.input, content: editValue }
        } as ToolUseBlock);
      }
      setEditing(false);
    };

    const handleStringModalSave = (updates: Record<string, string>) => {
      if (onUpdate) {
        const newInput = { ...block.input };
        for (const [key, val] of Object.entries(updates)) {
          (newInput as Record<string, unknown>)[key] = val;
        }
        onUpdate({ ...block, input: newInput } as ToolUseBlock);
      }
      setShowStringModal(false);
    };

    // Complete inline tools: flat single line, no expand
    if (isComplete && !isWrite) {
      return (
        <>
          <div className="tool-use-block tool-use-inline" data-match-count={matchCount}>
            <div className="block-header">
              <span className="tool-name">{block.name}</span>
              <span className="tool-summary">{searchQuery ? rendered : summary}</span>
              {expandableFields.length > 0 && (
                <button className="expand-multiline-btn" onClick={(e) => { e.stopPropagation(); setShowStringModal(true); }} title="View/edit fields">
                  &#x2922;
                </button>
              )}
            </div>
          </div>
          {showStringModal && (
            <StringModal
              fields={expandableFields}
              onSave={handleStringModalSave}
              onClose={() => setShowStringModal(false)}
            />
          )}
        </>
      );
    }

    return (
      <>
        <div className="tool-use-block" data-match-count={matchCount}>
          <div className="block-header" onClick={() => setExpanded(!expanded)}>
            <span className="chevron">{expanded ? '▼' : '▶'}</span>
            <span className="tool-name">{block.name}</span>
            {(isWrite && writeFilePath) ? (
              <span className="file-path">{pathHighlight ? pathHighlight.rendered : writeFilePath}</span>
            ) : summary ? (
              <span className="tool-summary">{summary}</span>
            ) : null}
            {isWrite && onUpdate && (
              <button className="edit-btn" onClick={(e) => { e.stopPropagation(); handleEditWrite(); }}>
                Edit
              </button>
            )}
            {expandableFields.length > 0 && (
              <button className="expand-multiline-btn" onClick={(e) => { e.stopPropagation(); setShowStringModal(true); }} title="View/edit fields">
                &#x2922;
              </button>
            )}
          </div>
          {expanded && (
            <div className="block-content">
              {editing && isWrite ? (
                <div className="edit-area">
                  <textarea
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    spellCheck={false}
                  />
                  <div className="edit-actions">
                    <button onClick={handleSave}>Save</button>
                    <button onClick={() => setEditing(false)}>Cancel</button>
                  </div>
                </div>
              ) : (
                <pre>{rendered}</pre>
              )}
            </div>
          )}
        </div>
        {showStringModal && (
          <StringModal
            fields={expandableFields}
            onSave={handleStringModalSave}
            onClose={() => setShowStringModal(false)}
          />
        )}
      </>
    );
  }

  if (block.type === 'tool_result') {
    // Normalize content: may be a string or an array of {type, text} blocks
    const contentText = typeof block.content === 'string'
      ? block.content
      : Array.isArray(block.content)
        ? (block.content as Array<{type: string; text: string}>).map(b => b.text ?? '').join('\n')
        : JSON.stringify(block.content);
    const { rendered, matchCount } = renderHighlighted(contentText, searchQuery, matchIndexOffset, activeMatchIndex);
    const short = isShortContent(contentText);

    // Short results: show without expand chevron
    if (short) {
      const isEmpty = !contentText.trim();
      return (
        <div className={`tool-result-block tool-result-inline ${block.is_error ? 'error' : ''}`} data-match-count={matchCount}>
          <div className="block-header">
            <span className="label">Tool Result {block.is_error && '(Error)'}</span>
            {isEmpty && <span className="tool-result-empty">(empty)</span>}
          </div>
          {!isEmpty && <pre className="block-content">{searchQuery ? rendered : contentText}</pre>}
        </div>
      );
    }

    return (
      <div className={`tool-result-block ${block.is_error ? 'error' : ''}`} data-match-count={matchCount}>
        <div className="block-header" onClick={() => setExpanded(!expanded)}>
          <span className="chevron">{expanded ? '▼' : '▶'}</span>
          <span className="label">Tool Result {block.is_error && '(Error)'}</span>
          {!expanded && <span className="preview">{contentText.slice(0, 80)}...</span>}
        </div>
        {expanded && <pre className="block-content">{rendered}</pre>}
      </div>
    );
  }

  return null;
}

// Re-export with the original name for backwards compatibility
export { ContentBlockComponent as ContentBlock };
