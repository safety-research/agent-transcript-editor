import { useState, useEffect } from 'react';
import { testConnection, checkApiStatus } from './llm/client';
import { checkMonitorStatus, fetchMonitorPrompts } from './monitor';
import type { PromptVariant } from './monitor';
import { useStore } from './store';
import { putGlobalSettings } from './api';

interface Props {
  onClose: () => void;
}

export function SettingsModal({ onClose }: Props) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; error?: string } | null>(null);
  const [apiConfigured, setApiConfigured] = useState<boolean | null>(null);
  const [monitorAvailable, setMonitorAvailable] = useState<boolean | null>(null);

  const lockFirstMessage = useStore(s => s.settings.lockFirstMessage);
  const childLock = useStore(s => s.settings.childLock);
  const promptMode = useStore(s => s.settings.promptMode);
  const soulDocument = useStore(s => s.settings.soulDocument);
  const monitorSettings = useStore(s => s.settings.monitor);
  const [promptFiles, setPromptFiles] = useState<string[]>([]);
  const [promptVariants, setPromptVariants] = useState<PromptVariant[]>([]);

  useEffect(() => {
    checkApiStatus().then(status => setApiConfigured(status.configured));
    checkMonitorStatus().then(status => setMonitorAvailable(status.available));
    fetch('/api/files/prompt-files').then(r => r.json()).then(d => setPromptFiles(d.files || [])).catch(() => {});
    fetchMonitorPrompts().then(setPromptVariants);
  }, []);

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    const result = await testConnection();
    setTestResult(result);
    setTesting(false);
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal settings-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Settings</h3>
          <button className="modal-close" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">
          {/* API Status */}
          <div className="form-group">
            <label>Backend API Status</label>
            <div className="status-row">
              {apiConfigured === null ? (
                <span className="status-checking">Checking...</span>
              ) : apiConfigured ? (
                <span className="status-configured">API key configured on backend</span>
              ) : (
                <span className="status-not-configured">
                  API key not configured. Set ANTHROPIC_API_KEY in backend/.env
                </span>
              )}
            </div>
          </div>
          <div className="test-row">
            <button onClick={handleTest} disabled={testing || !apiConfigured}>
              {testing ? 'Testing...' : 'Test Connection'}
            </button>
            {testResult && (
              <span className={testResult.success ? 'test-success' : 'test-error'}>
                {testResult.success ? '✓ Connected' : `✗ ${testResult.error}`}
              </span>
            )}
          </div>

          {/* AI Agent Settings */}
          <div className="settings-section">
            <h4 className="settings-section-title">AI Agent</h4>
            <div className="form-row">
              <label className="form-row-label">
                <input
                  type="checkbox"
                  checked={lockFirstMessage}
                  onChange={e => putGlobalSettings({ lock_first_message: e.target.checked })}
                />
                Lock first message
              </label>
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Prevent the AI agent from editing the first user message (task instructions)
            </span>

            <div className="form-row">
              <label className="form-row-label">
                <input
                  type="checkbox"
                  checked={childLock}
                  onChange={e => putGlobalSettings({ child_lock_enabled: e.target.checked })}
                />
                Child lock
              </label>
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              After the AI calls get_monitor_score, block further edits and metadata changes for the rest of that chat
            </span>

            <div className="form-row">
              <label className="form-row-label">Prompt mode</label>
              <select
                value={promptMode ?? 'soul'}
                onChange={e => putGlobalSettings({ prompt_mode: e.target.value })}
                className="form-input-sm"
              >
                <option value="soul">Soul</option>
                <option value="executor">Executor</option>
              </select>
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Soul: strategic attack guidance (SOUL.md). Executor: faithful instruction execution (EXECUTOR.md).
            </span>

            {promptFiles.length > 0 && (
              <div className="form-row">
                <label className="form-row-label">Soul document</label>
                <select
                  value={soulDocument ?? 'SOUL.md'}
                  onChange={e => putGlobalSettings({ soul_document: e.target.value })}
                  className="form-input-sm"
                >
                  {promptFiles.map(f => (
                    <option key={f} value={f}>{f}</option>
                  ))}
                </select>
              </div>
            )}
          </div>

          {/* Monitor Settings */}
          <div className="settings-section">
            <h4 className="settings-section-title">Trusted Monitor</h4>

            <div className="status-row" style={{ marginBottom: 'var(--space-3)' }}>
              {monitorAvailable === null ? (
                <span className="status-checking">Checking monitor...</span>
              ) : monitorAvailable ? (
                <span className="status-configured">Monitor available</span>
              ) : (
                <span className="status-not-configured">Monitor not available (trusted-monitor not found)</span>
              )}
            </div>

            <div className="form-row">
              <label className="form-row-label">
                <input
                  type="checkbox"
                  checked={monitorSettings.autoEvalOnLoad}
                  onChange={e => putGlobalSettings({ auto_eval_on_load: e.target.checked })}
                />
                Auto-evaluate on file load
              </label>
            </div>

            <div className="form-row">
              <label className="form-row-label">
                <input
                  type="checkbox"
                  checked={monitorSettings.enabled}
                  onChange={e => putGlobalSettings({ auto_eval_enabled: e.target.checked })}
                />
                Auto-evaluate after accept
              </label>
            </div>

            <div className="form-row">
              <label className="form-row-label">
                <input
                  type="checkbox"
                  checked={monitorSettings.evalEgregiousness}
                  onChange={e => putGlobalSettings({ eval_egregiousness: e.target.checked })}
                />
                Evaluate egregiousness
              </label>
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Score severity of the attack outcome
            </span>

            <div className="form-row">
              <label className="form-row-label">
                <input
                  type="checkbox"
                  checked={monitorSettings.evalIncriminating}
                  onChange={e => putGlobalSettings({ eval_incriminating: e.target.checked })}
                />
                Evaluate incriminating evidence
              </label>
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Score whether behavior appears intentional vs accidental
            </span>

            <div className="form-row">
              <label className="form-row-label">
                <input
                  type="checkbox"
                  checked={monitorSettings.evalEffectiveness}
                  onChange={e => putGlobalSettings({ eval_effectiveness: e.target.checked })}
                />
                Evaluate effectiveness
              </label>
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Score whether the attack would actually work given system defenses
            </span>

            <div className="form-row">
              <label className="form-row-label">
                <input
                  type="checkbox"
                  checked={monitorSettings.evalConfidence}
                  onChange={e => putGlobalSettings({ eval_confidence: e.target.checked })}
                />
                Evaluate confidence
              </label>
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Score whether the claimed outcome actually occurred in the transcript
            </span>

            <div className="form-row">
              <label className="form-row-label">
                <input
                  type="checkbox"
                  checked={monitorSettings.evalRealism}
                  onChange={e => putGlobalSettings({ eval_realism: e.target.checked })}
                />
                Evaluate realism
              </label>
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Score how natural vs. artificial the transcript appears
            </span>

            <div className="form-row">
              <label className="form-row-label">Suspiciousness evals per run</label>
              <input
                type="number"
                min={1}
                max={10}
                value={monitorSettings.nEvals}
                onChange={e => putGlobalSettings({ n_evals: parseInt(e.target.value) || 1 })}
                className="form-input-sm"
              />
            </div>
            <div className="form-row">
              <label className="form-row-label">Other metrics evals per run</label>
              <input
                type="number"
                min={1}
                max={10}
                value={monitorSettings.nEvalsOtherMetrics}
                onChange={e => putGlobalSettings({ n_evals_other: parseInt(e.target.value) || 1 })}
                className="form-input-sm"
              />
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Number of independent evaluations per metric. Suspiciousness uses n evals and takes the mean; other metrics (egregiousness, incriminating, etc.) use a separate count.
            </span>

            <div className="form-row" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: '0.25rem' }}>
              <label className="form-row-label">Prompt variants (max of means)</label>
              {promptVariants.length === 0 ? (
                <span className="help-text">No prompt variants available (trusted-monitor not installed?)</span>
              ) : promptVariants.map(pv => (
                <label key={pv.name} className="form-row-label" style={{ fontWeight: 'normal' }} title={pv.description}>
                  <input
                    type="checkbox"
                    checked={(monitorSettings.promptVariants ?? ['control_arena']).includes(pv.name)}
                    onChange={e => {
                      const current = monitorSettings.promptVariants ?? ['control_arena'];
                      if (e.target.checked) {
                        putGlobalSettings({ prompt_variants: [...current, pv.name] });
                      } else {
                        // Don't allow unchecking the last one
                        const next = current.filter(v => v !== pv.name);
                        if (next.length > 0) {
                          putGlobalSettings({ prompt_variants: next });
                        }
                      }
                    }}
                  />
                  {pv.name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}
                </label>
              ))}
            </div>
            <span className="help-text" style={{ marginTop: '-0.25rem', display: 'block' }}>
              Runs each selected prompt and takes the max score across variants
            </span>

            <div className="form-row">
              <label className="form-row-label">TPM limit (default key)</label>
              <input
                type="number"
                min={10000}
                step={100000}
                value={monitorSettings.tpmDefault}
                onChange={e => {
                  const val = Math.max(10000, parseInt(e.target.value) || 2_000_000);
                  putGlobalSettings({ tpm_default: val });
                  fetch('/api/config/rate-limit', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key_id: 'default', tpm: val }),
                  }).catch(() => {});
                }}
                className="form-input-sm"
              />
            </div>
            <div className="form-row">
              <label className="form-row-label">TPM limit (alt key)</label>
              <input
                type="number"
                min={10000}
                step={100000}
                value={monitorSettings.tpmAlt}
                onChange={e => {
                  const val = Math.max(10000, parseInt(e.target.value) || 2_000_000);
                  putGlobalSettings({ tpm_alt: val });
                  fetch('/api/config/rate-limit', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key_id: 'alt', tpm: val }),
                  }).catch(() => {});
                }}
                className="form-input-sm"
              />
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Input tokens per minute limit per API key (rolling 60s window)
            </span>

            <div className="form-row">
              <label className="form-row-label">API key</label>
              <select
                value={monitorSettings.apiKeyId ?? 'default'}
                onChange={e => putGlobalSettings({ monitor_api_key_id: e.target.value })}
                className="form-input-sm"
              >
                <option value="default">Default</option>
                <option value="alt">Alt</option>
              </select>
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Which backend API key to use for monitor evals (ANTHROPIC_API_KEY or ANTHROPIC_API_KEY_ALT)
            </span>

            <div className="form-row">
              <label className="form-row-label">Goal score</label>
              <input
                type="number"
                min={0}
                max={100}
                value={monitorSettings.goalScore}
                onChange={e => putGlobalSettings({ goal_score: parseInt(e.target.value) || 0 })}
                className="form-input-sm"
              />
            </div>
            <span className="help-text" style={{ marginTop: '-0.5rem', display: 'block' }}>
              Target suspiciousness score for the AI agent to aim below
            </span>
          </div>

          <div className="form-group" style={{ marginTop: '1rem' }}>
            <label>Configuration</label>
            <span className="help-text">
              API keys are now managed on the backend for security.
              <br />
              Edit <code>backend/.env</code> to configure your Anthropic API key.
            </span>
          </div>
        </div>
        <div className="modal-footer">
          <button className="primary" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
