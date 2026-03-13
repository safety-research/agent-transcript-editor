import { useState } from 'react';
import type { Message, ContentBlock as ContentBlockType } from './types';
import { ContentBlock } from './ContentBlock';

interface Props {
  message: Message;
  index: number;
  total: number;
  onUpdate: (message: Message) => void;
  onDelete: () => void;
  onMove: (direction: 'up' | 'down') => void;
  searchQuery: string;
  matchIndexOffset: number;
  activeMatchIndex: number;
  matchesPerBlock: number[];
}

export function MessageItem({ message, index, total, onUpdate, onDelete, onMove, searchQuery, matchIndexOffset, activeMatchIndex, matchesPerBlock }: Props) {
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [editError, setEditError] = useState('');
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const handleEdit = () => {
    setEditValue(JSON.stringify(content, null, 2));
    setEditError('');
    setEditing(true);
  };

  const handleSave = () => {
    try {
      const newContent: ContentBlockType[] = JSON.parse(editValue);
      onUpdate({ ...message, content: newContent });
      setEditing(false);
      setEditError('');
    } catch {
      setEditError('Invalid JSON');
    }
  };

  const content: ContentBlockType[] = Array.isArray(message.content)
    ? message.content
    : [{ type: 'text', text: String(message.content ?? '') }];

  const handleBlockUpdate = (blockIndex: number, block: ContentBlockType) => {
    const newContent = content.map((b, i) => i === blockIndex ? block : b);
    onUpdate({ ...message, content: newContent });
  };

  const toggleRole = () => {
    const newRole = message.role === 'user' ? 'assistant' : 'user';
    onUpdate({ ...message, role: newRole });
  };

  // Compute per-block match offsets from matchesPerBlock
  function blockOffset(blockIdx: number): number {
    let offset = matchIndexOffset;
    for (let i = 0; i < blockIdx; i++) {
      offset += (matchesPerBlock[i] || 0);
    }
    return offset;
  }

  return (
    <div className={`message ${message.role}`}>
      <div className="message-header">
        <span className="index">{index + 1}</span>
        <button className={`role ${message.role}`} onClick={toggleRole} title="Click to toggle role">{message.role}</button>
        {message.cwd && <span className="cwd" title="Working directory">{message.cwd}</span>}
        <div className="actions">
          {confirmingDelete ? (
            <>
              <span className="confirm-delete-label">Delete?</span>
              <button className="confirm-yes" onClick={() => { setConfirmingDelete(false); onDelete(); }}>Yes</button>
              <button className="confirm-no" onClick={() => setConfirmingDelete(false)}>No</button>
            </>
          ) : (
            <>
              <button onClick={() => onMove('up')} disabled={index === 0}>↑</button>
              <button onClick={() => onMove('down')} disabled={index === total - 1}>↓</button>
              <button onClick={handleEdit}>Edit</button>
              <button className="delete" onClick={() => setConfirmingDelete(true)}>×</button>
            </>
          )}
        </div>
      </div>

      {editing ? (
        <div className="edit-area">
          <textarea
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            rows={10}
          />
          <div className="edit-actions">
            <button onClick={handleSave}>Save</button>
            <button onClick={() => setEditing(false)}>Cancel</button>
            {editError && <span className="edit-error">{editError}</span>}
          </div>
        </div>
      ) : (
        <div className="message-content">
          {content.map((block, i) => (
            <ContentBlock
              key={i}
              block={block}
              onUpdate={(updated) => handleBlockUpdate(i, updated)}
              searchQuery={searchQuery}
              matchIndexOffset={blockOffset(i)}
              activeMatchIndex={activeMatchIndex}
              blockMatchCount={matchesPerBlock[i] || 0}
            />
          ))}
        </div>
      )}
    </div>
  );
}
