/**
 * Thin API helpers for frontend → backend mutation endpoints.
 *
 * The backend is the sole source of truth for persisted state.
 * These fire-and-forget POSTs; the UI updates reactively via SSE.
 */

import type { Message } from './types';
import type { MechanismDict } from './mechanismUtils';
import { useStore } from './store';

const API = '/api/session';

function encodeFileKey(fk: string): string {
  return fk.split('/').map(encodeURIComponent).join('/');
}

export async function saveMessages(fileKey: string, messages: Message[], messageOp?: { type: string; [key: string]: unknown }): Promise<{ transcript_hash: string; ids_fixed: number }> {
  const res = await fetch(`${API}/${encodeFileKey(fileKey)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, ...(messageOp ? { message_op: messageOp } : {}) }),
  });
  if (!res.ok) throw new Error(`Save failed: ${res.status}`);
  return res.json();
}

export interface MetadataUpdate {
  outcome?: string | null;
  scenario?: string | null;
  mechanism?: MechanismDict | null;
  summary?: MechanismDict | null;
}

export async function saveMetadata(fileKey: string, metadata: MetadataUpdate): Promise<{ transcript_hash: string }> {
  const res = await fetch(`${API}/${encodeFileKey(fileKey)}/metadata`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(metadata),
  });
  if (!res.ok) throw new Error(`Meta save failed: ${res.status}`);
  return res.json();
}

/**
 * Maps backend setting keys (snake_case) → frontend store location + key (camelCase).
 * Single source of truth for the backend↔frontend settings mapping.
 * Used by putGlobalSettings (optimistic updates) and SSE handler (incoming updates).
 *
 * When adding a new global setting:
 * 1. Add the field to GlobalSettingsBody in sessions.py (backend)
 * 2. Add the mapping here
 * 3. Add the store field in store.ts (settings or monitor section)
 */
export const SETTINGS_KEY_MAP: Record<string, { target: 'settings' | 'monitor'; key: string }> = {
  lock_first_message: { target: 'settings', key: 'lockFirstMessage' },
  child_lock_enabled: { target: 'settings', key: 'childLock' },
  prompt_mode: { target: 'settings', key: 'promptMode' },

  llm_model: { target: 'settings', key: 'llmModel' },
  llm_api_key_id: { target: 'settings', key: 'llmApiKeyId' },
  n_evals: { target: 'monitor', key: 'nEvals' },
  n_evals_other: { target: 'monitor', key: 'nEvalsOtherMetrics' },
  prompt_variants: { target: 'monitor', key: 'promptVariants' },
  eval_egregiousness: { target: 'monitor', key: 'evalEgregiousness' },
  eval_incriminating: { target: 'monitor', key: 'evalIncriminating' },
  eval_effectiveness: { target: 'monitor', key: 'evalEffectiveness' },
  eval_confidence: { target: 'monitor', key: 'evalConfidence' },
  eval_realism: { target: 'monitor', key: 'evalRealism' },
  monitor_api_key_id: { target: 'monitor', key: 'apiKeyId' },
  goal_score: { target: 'monitor', key: 'goalScore' },
  auto_eval_enabled: { target: 'monitor', key: 'enabled' },
  auto_eval_on_load: { target: 'monitor', key: 'autoEvalOnLoad' },
  rate_limit_enabled: { target: 'monitor', key: 'rateLimitEnabled' },
  tpm_default: { target: 'monitor', key: 'tpmDefault' },
  tpm_alt: { target: 'monitor', key: 'tpmAlt' },
};

// Keys in the SSE event envelope that are not settings — safe to ignore.
const SSE_ENVELOPE_KEYS = new Set(['type']);

/**
 * Apply a backend settings blob (snake_case keys) to the frontend store.
 * Used by both putGlobalSettings (optimistic) and SSE handler (incoming).
 *
 * Throws on unknown keys to catch drift between backend GlobalSettingsBody
 * and frontend SETTINGS_KEY_MAP.
 */
export function applyGlobalSettingsToStore(data: Record<string, unknown>): void {
  const store = useStore.getState();
  const settingsUpdate: Record<string, unknown> = {};
  const monitorUpdate: Record<string, unknown> = {};
  for (const [backendKey, value] of Object.entries(data)) {
    if (SSE_ENVELOPE_KEYS.has(backendKey)) continue;
    const mapping = SETTINGS_KEY_MAP[backendKey];
    if (!mapping) {
      throw new Error(
        `Unknown global setting key "${backendKey}" — add it to SETTINGS_KEY_MAP in api.ts`
      );
    }
    if (mapping.target === 'settings') {
      settingsUpdate[mapping.key] = value;
    } else {
      monitorUpdate[mapping.key] = value;
    }
  }
  if (Object.keys(settingsUpdate).length > 0) store.updateSettings(settingsUpdate);
  if (Object.keys(monitorUpdate).length > 0) store.updateMonitorSettings(monitorUpdate);
}

/**
 * PUT a partial global settings update to the backend.
 * Optimistically updates the store immediately, then sends to backend.
 * Backend persists to disk and broadcasts via SSE (which is a no-op since store already has the value).
 * This is the ONLY way settings should be changed from the frontend.
 */
/**
 * Reload a transcript from disk, discarding the in-memory session cache.
 * The backend broadcasts a session_reloaded event to all subscribers.
 */
export async function reloadFromDisk(fileKey: string): Promise<{ reloaded: boolean; message_count?: number }> {
  const res = await fetch(`/api/files/reload/${encodeFileKey(fileKey)}`, {
    method: 'POST',
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || `Reload failed: ${res.status}`);
  }
  return res.json();
}

export function putGlobalSettings(update: Record<string, unknown>): void {
  applyGlobalSettingsToStore(update);

  fetch(`${API}/global-settings`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  }).catch((e) => console.error('Failed to persist global settings:', e));
}
