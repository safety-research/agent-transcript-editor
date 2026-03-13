import { useState, useEffect, useRef } from 'react';

interface MultilineField {
  key: string;
  value: string;
}

interface Props {
  fields: MultilineField[];
  onSave: (updates: Record<string, string>) => void;
  onClose: () => void;
}

export function StringModal({ fields, onSave, onClose }: Props) {
  const [values, setValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const f of fields) {
      init[f.key] = f.value;
    }
    return init;
  });
  const overlayRef = useRef<HTMLDivElement>(null);

  const hasChanges = fields.some(f => values[f.key] !== f.value);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === overlayRef.current) {
      onClose();
    }
  };

  const handleSave = () => {
    const updates: Record<string, string> = {};
    for (const f of fields) {
      if (values[f.key] !== f.value) {
        updates[f.key] = values[f.key];
      }
    }
    onSave(updates);
  };

  const handleCopy = (key: string) => {
    navigator.clipboard.writeText(values[key]);
  };

  return (
    <div className="string-modal-overlay" ref={overlayRef} onClick={handleOverlayClick}>
      <div className="string-modal">
        <div className="string-modal-header">
          <span className="string-modal-title">
            {fields.length === 1 ? fields[0].key : 'Multiline Fields'}
          </span>
          <button className="string-modal-close" onClick={onClose}>&times;</button>
        </div>
        <div className="string-modal-body">
          {fields.map(f => (
            <div key={f.key} className="string-modal-field">
              {fields.length > 1 && (
                <div className="string-modal-field-header">
                  <span className="string-modal-field-label">{f.key}</span>
                  <button className="string-modal-copy-btn" onClick={() => handleCopy(f.key)} title="Copy to clipboard">
                    Copy
                  </button>
                </div>
              )}
              <textarea
                className="string-modal-textarea"
                value={values[f.key]}
                onChange={e => setValues(prev => ({ ...prev, [f.key]: e.target.value }))}
                spellCheck={false}
              />
              {fields.length === 1 && (
                <div className="string-modal-single-actions">
                  <button className="string-modal-copy-btn" onClick={() => handleCopy(f.key)} title="Copy to clipboard">
                    Copy
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
        <div className="string-modal-footer">
          <button className="string-modal-cancel" onClick={onClose}>Cancel</button>
          <button
            className="string-modal-save"
            onClick={handleSave}
            disabled={!hasChanges}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
