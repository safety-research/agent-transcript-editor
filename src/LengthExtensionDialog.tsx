import { useState, useEffect } from 'react';
import type { Message } from './types';
import Modal from './Modal';

interface Props {
  messages: Message[];
  onExtend: (prompt: string) => void;
  onClose: () => void;
}

const METHODS = [
  { id: 'file_reads', label: 'Add file reads', description: 'Insert Read tool_use + tool_result pairs with realistic file contents' },
  { id: 'increase_files', label: 'Increase file sizes', description: 'Make existing file content blocks longer and more detailed' },
  { id: 'new_tasks', label: 'Add task steps', description: 'Add new requirements (also added to initial user prompt)' },
  { id: 'errors', label: 'Introduce errors/retries', description: 'Add realistic command failures with recovery' },
  { id: 'conversation', label: 'Add user-agent conversation', description: 'Add additional back-and-forth dialogue between user and assistant (follow-up questions, clarifications, new requests)' },
];

export function LengthExtensionDialog({ messages, onExtend, onClose }: Props) {
  const [currentTokens, setCurrentTokens] = useState<number | null>(null);
  const [targetTokens, setTargetTokens] = useState('');
  const [selectedMethods, setSelectedMethods] = useState<string[]>(['file_reads', 'increase_files']);
  const [notes, setNotes] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch('/api/monitor/token-count', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages }),
    })
      .then(res => {
        if (!res.ok) throw new Error('API error');
        return res.json();
      })
      .then(data => {
        setCurrentTokens(data.token_count);
        // Default target: 2x current
        setTargetTokens(String(data.token_count * 2));
        setLoading(false);
      })
      .catch(() => {
        // Fallback estimate
        const text = messages.map(m => JSON.stringify(m)).join('\n');
        const estimate = Math.round(text.length / 4);
        setCurrentTokens(estimate);
        setTargetTokens(String(estimate * 2));
        setLoading(false);
      });
  }, [messages]);

  const toggleMethod = (id: string) => {
    setSelectedMethods(prev =>
      prev.includes(id)
        ? prev.filter(m => m !== id)
        : [...prev, id]
    );
  };

  const handleExtend = () => {
    const selected = METHODS.filter(m => selectedMethods.includes(m.id));
    const unselected = METHODS.filter(m => !selectedMethods.includes(m.id));

    const prompt = [
      `Extend this transcript to approximately ${targetTokens} tokens (currently ${currentTokens}).`,
      `Extend using ONLY the following methods: ${selected.map(m => m.description).join('; ')}.`,
      unselected.length > 0 ? `Do NOT: ${unselected.map(m => m.description.charAt(0).toLowerCase() + m.description.slice(1)).join('; ')}.` : '',
      notes ? `User guidance: ${notes}.` : '',
      `Maintain the conversation's natural flow and existing context.`,
    ].filter(Boolean).join('\n');

    onExtend(prompt);
    onClose();
  };

  return (
    <Modal title="Extend Transcript Length" onClose={onClose} className="length-extension-modal">
        <div className="modal-body">
          {/* Current token count */}
          <div className="form-group">
            <label>Current Token Count</label>
            <div className="token-display">
              {loading ? (
                <span className="status-checking">Counting...</span>
              ) : (
                <span className="token-count">{currentTokens?.toLocaleString()}</span>
              )}
            </div>
          </div>

          {/* Target */}
          <div className="form-group">
            <label>Target Token Count</label>
            <input
              type="number"
              value={targetTokens}
              onChange={e => setTargetTokens(e.target.value)}
              min={currentTokens ?? 0}
              step={1000}
              style={{ width: '100%' }}
            />
          </div>

          {/* Methods */}
          <div className="form-group">
            <label>Extension Methods</label>
            <div className="method-list">
              {METHODS.map(method => (
                <label key={method.id} className="method-item">
                  <input
                    type="checkbox"
                    checked={selectedMethods.includes(method.id)}
                    onChange={() => toggleMethod(method.id)}
                  />
                  <div className="method-text">
                    <div className="method-label">{method.label}</div>
                    <div className="method-desc">{method.description}</div>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {/* Notes */}
          <div className="form-group">
            <label>Additional Notes (optional)</label>
            <textarea
              value={notes}
              onChange={e => setNotes(e.target.value)}
              placeholder="e.g., focus on adding database interactions..."
              rows={3}
              style={{ width: '100%', resize: 'vertical' }}
            />
          </div>
        </div>
        <div className="modal-footer">
          <button onClick={onClose}>Cancel</button>
          <button
            className="primary"
            onClick={handleExtend}
            disabled={!targetTokens || selectedMethods.length === 0}
          >
            Extend
          </button>
        </div>
    </Modal>
  );
}
