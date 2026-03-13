import { useState } from 'react';
import type { MechanismDict } from './mechanismUtils';

interface Props {
  onClassify: (data: { outcome: string | null; scenario: string | null; mechanism: MechanismDict | null }) => void;
  onDismiss: () => void;
}

export function ClassifyTranscriptModal({ onClassify, onDismiss }: Props) {
  const [mode, setMode] = useState<'choose' | 'attack'>('choose');
  const [outcome, setOutcome] = useState('');
  const [scenario, setScenario] = useState('');
  const [steps, setSteps] = useState<Array<{ msg: string; desc: string }>>([{ msg: '', desc: '' }]);

  const handleSave = () => {
    const mechanism: MechanismDict = {};
    for (const step of steps) {
      const num = parseInt(step.msg);
      if (!isNaN(num) && num >= 1 && step.desc.trim()) {
        mechanism[num] = step.desc.trim();
      }
    }
    onClassify({
      outcome: outcome.trim() || null,
      scenario: scenario.trim() || null,
      mechanism: Object.keys(mechanism).length > 0 ? mechanism : null,
    });
  };

  const addStep = () => {
    setSteps(s => [...s, { msg: '', desc: '' }]);
  };

  const updateStep = (index: number, field: 'msg' | 'desc', value: string) => {
    setSteps(s => s.map((step, i) => i === index ? { ...step, [field]: value } : step));
  };

  const removeStep = (index: number) => {
    setSteps(s => s.filter((_, i) => i !== index));
  };

  return (
    <div className="modal-overlay" onClick={onDismiss}>
      <div className="modal classify-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Classify Transcript</h3>
          <button className="modal-close" onClick={onDismiss}>&times;</button>
        </div>
        <div className="modal-body">
          {mode === 'choose' ? (
            <div className="classify-choices">
              <button className="classify-choice benign" onClick={() => { onClassify({ outcome: null, scenario: null, mechanism: null }); }}>
                <span className="classify-choice-icon">&#x2713;</span>
                <span className="classify-choice-label">Benign</span>
                <span className="classify-choice-desc">No attack in this transcript</span>
              </button>
              <button className="classify-choice attack" onClick={() => setMode('attack')}>
                <span className="classify-choice-icon">&#x26A0;</span>
                <span className="classify-choice-label">Has Attack</span>
                <span className="classify-choice-desc">Describe the attack outcome and mechanism</span>
              </button>
            </div>
          ) : (
            <div className="classify-attack-form">
              <div className="banner-field">
                <label>Outcome</label>
                <input
                  type="text"
                  value={outcome}
                  onChange={e => setOutcome(e.target.value)}
                  placeholder="e.g. 'SSH keys were sent to an external server'"
                  autoFocus
                />
              </div>
              <div className="banner-field">
                <label>Scenario</label>
                <input
                  type="text"
                  value={scenario}
                  onChange={e => setScenario(e.target.value)}
                  placeholder="e.g. 'Internal vLLM server on shared GPU cluster serving 20 ML researchers'"
                />
              </div>
              <div className="banner-field">
                <label>Mechanism Steps</label>
                {steps.map((step, i) => (
                  <div key={i} className="mechanism-row" style={{ marginBottom: '4px' }}>
                    <input
                      type="number"
                      className="mechanism-add-msg"
                      value={step.msg}
                      onChange={e => updateStep(i, 'msg', e.target.value)}
                      placeholder="#"
                      min={1}
                    />
                    <input
                      className="mechanism-add-desc"
                      value={step.desc}
                      onChange={e => updateStep(i, 'desc', e.target.value)}
                      placeholder="The agent [factual action]"
                    />
                    {steps.length > 1 && (
                      <button className="mechanism-step-delete" onClick={() => removeStep(i)}>&times;</button>
                    )}
                  </div>
                ))}
                <button className="mechanism-add-btn" onClick={addStep} style={{ marginTop: '4px' }}>
                  + Add step
                </button>
              </div>
            </div>
          )}
        </div>
        <div className="modal-footer">
          <button onClick={onDismiss}>Skip</button>
          {mode === 'attack' && (
            <button className="primary" onClick={handleSave}>Save</button>
          )}
        </div>
      </div>
    </div>
  );
}
