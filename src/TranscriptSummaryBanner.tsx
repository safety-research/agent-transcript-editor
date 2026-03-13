import { useState, useRef, useCallback, useEffect } from 'react';
import type { MechanismDict } from './mechanismUtils';

interface Props {
  summary: MechanismDict | null;
  outcome: string | null;
  scenario: string | null;
  mechanism: MechanismDict | null;
  onUpdateMetadata: (metadata: { summary?: MechanismDict | null; outcome?: string | null; scenario?: string | null; mechanism?: MechanismDict | null }) => void;
  onJumpToMessage: (oneIndexed: number) => void;
}

function StepList({
  label,
  entries,
  onUpdate,
  onDelete,
  onJumpToMessage,
  placeholder,
}: {
  label: string;
  entries: [number, string][];
  onUpdate: (key: number, value: string) => void;
  onDelete: (key: number) => void;
  onJumpToMessage: (oneIndexed: number) => void;
  placeholder: string;
}) {
  const [newStepMsg, setNewStepMsg] = useState('');
  const [newStepDesc, setNewStepDesc] = useState('');

  const handleAdd = () => {
    const msgNum = parseInt(newStepMsg);
    if (isNaN(msgNum) || msgNum < 1 || !newStepDesc.trim()) return;
    onUpdate(msgNum, newStepDesc.trim());
    setNewStepMsg('');
    setNewStepDesc('');
  };

  return (
    <div className="banner-field">
      <label>{label}</label>
      {entries.length > 0 && (
        <div className="mechanism-list">
          {entries.map(([key, desc]) => (
            <div key={key} className="mechanism-row">
              <button
                className="mechanism-step-link"
                onClick={() => onJumpToMessage(key)}
                title={`Jump to message ${key}`}
              >
                [{key}]
              </button>
              <input
                className="mechanism-step-desc"
                value={desc}
                onChange={e => onUpdate(key, e.target.value)}
                placeholder={placeholder}
              />
              <button
                className="mechanism-step-delete"
                onClick={() => onDelete(key)}
                title="Remove step"
              >
                &times;
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="mechanism-add-row">
        <input
          type="number"
          className="mechanism-add-msg"
          value={newStepMsg}
          onChange={e => setNewStepMsg(e.target.value)}
          placeholder="Msg #"
          min={1}
        />
        <input
          className="mechanism-add-desc"
          value={newStepDesc}
          onChange={e => setNewStepDesc(e.target.value)}
          placeholder={placeholder}
          onKeyDown={e => { if (e.key === 'Enter') handleAdd(); }}
        />
        <button
          className="mechanism-add-btn"
          onClick={handleAdd}
          disabled={!newStepMsg || !newStepDesc.trim()}
        >
          Add
        </button>
      </div>
    </div>
  );
}

function AutoTextarea({ value, onCommit, placeholder }: { value: string; onCommit: (v: string) => void; placeholder: string }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [localValue, setLocalValue] = useState(value);
  const editingRef = useRef(false);

  // Sync from props when not actively editing
  useEffect(() => {
    if (!editingRef.current) {
      setLocalValue(value);
    }
  }, [value]);

  const resize = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = el.scrollHeight + 'px';
  }, []);
  useEffect(resize, [localValue, resize]);

  const commitValue = useCallback(() => {
    editingRef.current = false;
    if (localValue !== value) {
      onCommit(localValue);
    }
  }, [localValue, value, onCommit]);

  return (
    <textarea
      ref={ref}
      className="banner-textarea"
      rows={1}
      value={localValue}
      onChange={e => { editingRef.current = true; setLocalValue(e.target.value); }}
      onBlur={commitValue}
      onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commitValue(); } }}
      placeholder={placeholder}
      onInput={resize}
    />
  );
}

export function TranscriptSummaryBanner({ summary, outcome, scenario, mechanism, onUpdateMetadata, onJumpToMessage }: Props) {
  const [expanded, setExpanded] = useState(false);

  // Keys may be integers ("5") or ranges ("2-5"); parseInt handles both
  const toEntries = (dict: MechanismDict): [number, string][] =>
    Object.entries(dict)
      .map(([k, v]) => [parseInt(k), v] as [number, string])
      .filter(([k]) => !isNaN(k))
      .sort(([a], [b]) => a - b);

  const summaryEntries = summary ? toEntries(summary) : [];
  const mechanismEntries = mechanism ? toEntries(mechanism) : [];

  const isAttack = !!(outcome || mechanismEntries.length > 0);
  const stepCount = mechanismEntries.length;
  const summaryCount = summaryEntries.length;

  const handleMarkBenign = () => {
    onUpdateMetadata({ outcome: null, mechanism: null });
  };

  return (
    <div className={`transcript-summary-banner ${isAttack ? 'attack' : 'benign'}`}>
      <div
        className="banner-collapsed"
        onClick={() => setExpanded(e => !e)}
      >
        <span className={`banner-chevron ${expanded ? 'open' : ''}`}>&#9656;</span>
        {isAttack ? (
          <span className="banner-summary">
            {summaryCount > 0 && (
              <span className="banner-summary-count">{summaryCount} summary step{summaryCount !== 1 ? 's' : ''} &middot; </span>
            )}
            <span className="banner-label">Attack</span>
            {outcome && <span className="banner-outcome">: {outcome}</span>}
            {stepCount > 0 && (
              <span className="banner-step-count"> &middot; {stepCount} mechanism step{stepCount !== 1 ? 's' : ''}</span>
            )}
          </span>
        ) : (
          <span className="banner-summary">
            {summaryCount > 0 ? (
              <span className="banner-summary-count">{summaryCount} summary step{summaryCount !== 1 ? 's' : ''} &middot; </span>
            ) : null}
            <span className="banner-label benign-label">Benign transcript</span>
            <span className="banner-hint"> &middot; click to classify</span>
          </span>
        )}
      </div>

      <div className={`banner-expand-wrapper ${expanded ? 'open' : ''}`}>
        <div className="banner-expanded">
          <div className="banner-field">
            <label>Outcome</label>
            <AutoTextarea
              value={outcome ?? ''}
              onCommit={v => onUpdateMetadata({ outcome: v || null })}
              placeholder="Describe what happened (passive, no actor) e.g. 'SSH keys were sent to an external server'"
            />
          </div>

          <StepList
            label="Mechanism Steps"
            entries={mechanismEntries}
            onUpdate={(key, value) => {
              const updated = { ...(mechanism ?? {}), [key]: value };
              onUpdateMetadata({ mechanism: updated });
            }}
            onDelete={(key) => {
              if (!mechanism) return;
              const updated = { ...mechanism };
              delete updated[key];
              onUpdateMetadata({ mechanism: Object.keys(updated).length > 0 ? updated : null });
            }}
            onJumpToMessage={onJumpToMessage}
            placeholder="The agent [factual action at this step]"
          />

          <div className="banner-field">
            <label>Scenario</label>
            <AutoTextarea
              value={scenario ?? ''}
              onCommit={v => onUpdateMetadata({ scenario: v || null })}
              placeholder="Deployment context e.g. 'Internal vLLM server on shared GPU cluster serving models to 20 ML researchers'"
            />
          </div>

          <StepList
            label="Summary"
            entries={summaryEntries}
            onUpdate={(key, value) => {
              const updated = { ...(summary ?? {}), [key]: value };
              onUpdateMetadata({ summary: updated });
            }}
            onDelete={(key) => {
              if (!summary) return;
              const updated = { ...summary };
              delete updated[key];
              onUpdateMetadata({ summary: Object.keys(updated).length > 0 ? updated : null });
            }}
            onJumpToMessage={onJumpToMessage}
            placeholder="Describe what happens at this step"
          />

          {isAttack && (
            <button className="banner-benign-link" onClick={handleMarkBenign}>
              Mark as benign
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
