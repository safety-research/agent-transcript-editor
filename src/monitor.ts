/**
 * Monitor API client - interfaces with the backend /api/monitor endpoints
 * for trusted monitor evaluation of transcripts.
 */

import type { Message } from './types';
import { mechanismDictToString } from './mechanismUtils';
import { useStore, parseFileKey } from './store';
import type { FileKey, VariantEval } from './store';

import { API_BASE } from './constants';

/** Prompt variant metadata from trusted-monitor */
export interface PromptVariant {
  name: string;
  description: string;
}

/** Fetch available prompt variants from the backend */
export async function fetchMonitorPrompts(): Promise<PromptVariant[]> {
  try {
    const response = await fetch(`${API_BASE}/monitor/prompts`);
    if (!response.ok) {
      console.error(`Failed to fetch monitor prompts: ${response.status} ${response.statusText}`);
      return [];
    }
    const data = await response.json();
    return data.prompts ?? [];
  } catch (e) {
    console.error('Failed to fetch monitor prompts (backend down?):', e);
    return [];
  }
}

/** Check if monitor is available */
export async function checkMonitorStatus(): Promise<{ available: boolean; error?: string }> {
  try {
    const response = await fetch(`${API_BASE}/monitor/status`);
    return response.json();
  } catch {
    return { available: false, error: 'Backend unreachable' };
  }
}

/** Minimize a transcript (full Claude Code → minimal format) via backend minimizer tool */
export async function minimizeTranscript(messages: Record<string, unknown>[]): Promise<{
  messages: Message[];
  original_count: number;
  minimized_count: number;
  was_full_format: boolean;
}> {
  const response = await fetch(`${API_BASE}/tools/minimize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
  });

  if (!response.ok) {
    throw new Error(`Minimize API error: ${response.status}`);
  }

  return response.json();
}

/** Fix tool IDs in messages */
export async function fixToolIds(messages: Message[]): Promise<{
  messages: Message[];
  ids_fixed: number;
  id_map: Record<string, string>;
}> {
  const response = await fetch(`${API_BASE}/tools/fix-ids`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
  });

  if (!response.ok) {
    throw new Error(`Fix IDs API error: ${response.status}`);
  }

  return response.json();
}

/** Verify tool IDs without fixing */
export async function verifyToolIds(messages: Message[]): Promise<{
  valid: boolean;
  issues: Array<{ line_number: string; block_type: string; id_value: string; issue: string }>;
}> {
  const response = await fetch(`${API_BASE}/tools/fix-ids`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, verify_only: true }),
  });

  if (!response.ok) {
    throw new Error(`Verify IDs API error: ${response.status}`);
  }

  return response.json();
}

/**
 * Trigger a multi-metric monitor evaluation for a file via the backend.
 *
 * The backend runs all applicable metrics concurrently and writes scores
 * to the .meta.json sidecar file on completion. The frontend just polls
 * for progress and updates the UI store.
 */
export async function triggerMonitorEval(fileKey: FileKey, metricsOverride?: string[], force?: boolean, variantOverrides?: string[]): Promise<void> {
  const store = useStore.getState();
  const file = store.getFile(fileKey);
  if (!file || file.messages.length === 0) return;

  const { monitor } = store.settings;
  const snapshotHash = file.transcriptHash;
  const { projectDirName } = parseFileKey(fileKey);

  if (metricsOverride) {
    // Partial rerun — only reset the specific metrics being rerun
    const partialReset: Partial<import('./store').MonitorEval> = { status: 'running', errorMessage: null };
    for (const m of metricsOverride) {
      if (m === 'suspiciousness') {
        // Only clear variantEvals if rerunning ALL variants (no variantOverrides)
        if (!variantOverrides) {
          partialReset.variantEvals = [];
        }
        // If partial variant rerun, keep existing variantEvals — backend merges
      } else {
        (partialReset as Record<string, unknown>)[m] = null;
      }
    }
    store.setMonitorEval(fileKey, partialReset);
  } else {
    store.setMonitorEval(fileKey, { status: 'running', variantEvals: [], egregiousness: null, incriminating: null, effectiveness: null, confidence: null, realism: null, transcriptHash: null, errorMessage: null });
  }

  try {
    // Single request to start all metrics
    const mechanismStr = file.mechanism && Object.keys(file.mechanism).length > 0
      ? mechanismDictToString(file.mechanism)
      : null;

    const requestBody: Record<string, unknown> = {
      project: projectDirName,
      file_name: file.fileName,
      transcript: file.messages,
      outcome: file.outcome || null,
      scenario: file.scenario || null,
      mechanism: mechanismStr,
      n_evals: monitor.nEvals,
      n_evals_other: monitor.nEvalsOtherMetrics,
      prompt_variants: variantOverrides ?? monitor.promptVariants,
      eval_egregiousness: monitor.evalEgregiousness,
      eval_incriminating: monitor.evalIncriminating,
      eval_effectiveness: monitor.evalEffectiveness,
      eval_confidence: monitor.evalConfidence,
      eval_realism: monitor.evalRealism,
      api_key_id: monitor.apiKeyId ?? 'default',
      transcript_hash: snapshotHash,
    };
    if (metricsOverride) {
      requestBody.metrics_override = metricsOverride;
    }
    if (force) {
      requestBody.force = true;
    }

    const response = await fetch(`${API_BASE}/monitor/evaluate-file`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody),
    });

    if (!response.ok) throw new Error(`Monitor API error: ${response.status}`);
    const { eval_id: evalId } = await response.json();
    store.setMonitorEval(fileKey, { evalId });

    await _pollEvalLoop(fileKey, evalId, snapshotHash);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    const isNetwork = detail.includes('Failed to fetch') || detail.includes('NetworkError') || detail.includes('net::');
    const errorMsg = isNetwork
      ? `Cannot reach monitor backend: ${detail}`
      : `Evaluation failed: ${detail}`;
    useStore.getState().setMonitorEval(fileKey, { status: 'error', errorMessage: errorMsg });
  }
}

/**
 * Poll an existing evaluation by eval_id and update the store.
 *
 * Used by the SSE handler when the backend agent triggers an evaluation
 * (monitor_eval_trigger event) — the frontend reuses the same polling
 * + store update logic as the "Evaluate" button.
 */
export function pollExistingEval(fileKey: FileKey, evalId: string, cached?: boolean): void {
  const store = useStore.getState();
  const file = store.getFile(fileKey);
  const snapshotHash = file?.transcriptHash ?? null;

  // Skip the "running" flash when result is already cached — go straight to polling
  if (!cached) {
    store.setMonitorEval(fileKey, { status: 'running', evalId, errorMessage: null });
  }

  // Fire-and-forget — errors are caught internally
  _pollEvalLoop(fileKey, evalId, snapshotHash).catch(err => {
    const detail = err instanceof Error ? err.message : String(err);
    useStore.getState().setMonitorEval(fileKey, { status: 'error', errorMessage: `Evaluation failed: ${detail}` });
  });
}

/** Internal: poll an eval_id until done/error, updating the store with partial results. */
async function _pollEvalLoop(fileKey: FileKey, evalId: string, snapshotHash: string | null): Promise<void> {
  let consecutiveFailures = 0;
  const MAX_CONSECUTIVE_FAILURES = 5;
  let firstPoll = true;

  // eslint-disable-next-line no-constant-condition
  while (true) {
    // Check immediately on first iteration (fast path for cached results)
    if (!firstPoll) {
      await new Promise(r => setTimeout(r, 2000));
    }
    firstPoll = false;

    let status: { status: string; metrics: Record<string, Record<string, unknown>>; scores: Record<string, number>; evals: Record<string, unknown>; error_message: string | null };
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      let pollResp: Response;
      try {
        pollResp = await fetch(`${API_BASE}/monitor/evaluate-file/${evalId}`, { signal: controller.signal });
      } finally { clearTimeout(timeout); }
      if (!pollResp.ok) throw new Error(`Poll error: ${pollResp.status}`);
      status = await pollResp.json();
      consecutiveFailures = 0;
    } catch (err) {
      consecutiveFailures++;
      console.warn(`[monitor] Poll failed (${consecutiveFailures}/${MAX_CONSECUTIVE_FAILURES}):`, err);
      if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
        throw new Error('Backend unreachable after multiple retries');
      }
      continue;
    }

    // Update store with partial results as metrics complete
    _updateStoreFromStatus(fileKey, status);

    if (status.status === 'done' || status.status === 'error' || status.status === 'cancelled') {
      const currentStore = useStore.getState();
      if (status.status === 'cancelled') {
        // Eval was superseded by a newer one — don't show error, just stop polling.
        // The newer eval's poll loop (or SSE trigger) will update the store.
        break;
      }
      if (status.status === 'error' && !status.scores) {
        currentStore.setMonitorEval(fileKey, { status: 'error', errorMessage: status.error_message ?? 'Evaluation failed', runningMetrics: null });
      } else {
        // Build per-metric error details for failed metrics
        const metricErrors: string[] = [];
        for (const [key, metric] of Object.entries(status.metrics)) {
          if (metric.status === 'error') {
            const errMsg = (metric.error as string) ?? 'Unknown error';
            metricErrors.push(`${key}: ${errMsg}`);
          }
        }
        const errorMessage = metricErrors.length > 0
          ? metricErrors.join('\n')
          : status.error_message;
        currentStore.setMonitorEval(fileKey, { status: 'done', transcriptHash: snapshotHash, errorMessage, runningMetrics: null });
      }
      break;
    }
  }
}

/**
 * Build store updates from raw backend metrics, merging new variant evals
 * with any existing ones in the store (for partial variant reruns).
 *
 * Shared by both the HTTP polling path and the SSE event handler.
 */
export function buildMonitorUpdates(
  metrics: Record<string, Record<string, unknown>>,
  existingVariantEvals: VariantEval[],
): Partial<import('./store').MonitorEval> {
  const updates: Partial<import('./store').MonitorEval> = {};

  // Process suspiciousness variants
  const variantEvals: VariantEval[] = [];
  for (const [key, metric] of Object.entries(metrics)) {
    if (!key.startsWith('suspiciousness:')) continue;
    if (metric.status !== 'done') continue;
    const variantName = key.split(':', 2)[1];
    variantEvals.push({
      variant: variantName,
      results: ((metric.results as Array<Record<string, unknown>>) ?? []).map(r => ({
        score: r.score as number,
        reasoning: r.reasoning as string,
        ...(r.thinking ? { thinking: r.thinking as string } : {}),
      })),
    });
  }

  if (variantEvals.length > 0) {
    // Merge with existing variant evals (for partial variant reruns)
    const newVariantNames = new Set(variantEvals.map(v => v.variant));
    updates.variantEvals = [
      ...existingVariantEvals.filter(v => !newVariantNames.has(v.variant)),
      ...variantEvals,
    ];
  }

  // Process other metrics
  const egr = metrics.egregiousness;
  if (egr?.status === 'done') {
    updates.egregiousness = {
      results: ((egr.results as Array<Record<string, unknown>>) ?? []).map(r => ({
        score: r.score as number,
        scoreNumberOnly: r.scoreNumberOnly as number,
        reasoning: r.reasoning as string,
        ...(r.thinking ? { thinking: r.thinking as string } : {}),
      })),
    };
  }

  for (const metricName of ['incriminating', 'effectiveness', 'confidence', 'realism'] as const) {
    const m = metrics[metricName];
    if (m?.status !== 'done') continue;
    updates[metricName] = {
      results: ((m.results as Array<Record<string, unknown>>) ?? []).map(r => ({
        score: r.score as number,
        reasoning: r.reasoning as string,
        ...(r.thinking ? { thinking: r.thinking as string } : {}),
      })),
    };
  }

  return updates;
}

/** Map backend file-eval status to frontend store updates. */
function _updateStoreFromStatus(
  fileKey: FileKey,
  status: { metrics: Record<string, Record<string, unknown>>; scores: Record<string, number> },
) {
  const currentStore = useStore.getState();
  const existingVariants = currentStore.getFile(fileKey)?.monitorEval?.variantEvals ?? [];
  const updates = buildMonitorUpdates(status.metrics, existingVariants);

  // Derive which metrics are currently running/queued from the backend's per-metric status
  const running: string[] = [];
  for (const [key, metric] of Object.entries(status.metrics)) {
    if (metric.status === 'queued' || metric.status === 'running') {
      // Normalize "suspiciousness:variant_name" → "suspiciousness"
      const metricName = key.includes(':') ? key.split(':')[0] : key;
      if (!running.includes(metricName)) running.push(metricName);
    }
  }
  updates.runningMetrics = running.length > 0 ? running : null;

  if (Object.keys(updates).length > 0) {
    currentStore.setMonitorEval(fileKey, updates);
  }
}

/** Check transcript for file-state inconsistencies */
export interface ConsistencyWarning {
  id: string;
  type: string;
  file_path: string;
  message_index: number;
  message: string;
  expected: string | null;
  actual: string | null;
}

export interface ConsistencyResult {
  warnings: ConsistencyWarning[];
  files_tracked: number;
  files_known: number;
  files_fragments: number;
  files_partial: number;
}

export async function checkConsistency(
  messages: Message[],
  suppressions: string[] = [],
  expectSystemReminders: boolean = true,
): Promise<ConsistencyResult> {
  const response = await fetch(`${API_BASE}/tools/check-consistency`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, suppressions, expect_system_reminders: expectSystemReminders }),
  });

  if (!response.ok) {
    throw new Error(`Consistency check API error: ${response.status}`);
  }

  return response.json();
}

/** Count tokens in text */
export async function countTokens(text: string): Promise<{ token_count: number; estimated?: boolean }> {
  const response = await fetch(`${API_BASE}/llm/count-tokens`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });

  if (!response.ok) {
    throw new Error(`Token count API error: ${response.status}`);
  }

  return response.json();
}
