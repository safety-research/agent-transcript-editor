import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import { get as idbGet, set as idbSet, del as idbDel } from 'idb-keyval';
import type { Message } from './types';
import type { LLMSession } from './llm/types';
import type { MechanismDict } from './mechanismUtils';
import { normalizeMessages } from './normalizeMessages';

// ── FileKey ─────────────────────────────────────────────────────────────

/** Unique identifier: "{projectDirName}/{fileName}" e.g. "default/attack-v2.jsonl" */
export type FileKey = string;

export function makeFileKey(projectDirName: string, fileName: string): FileKey {
  return `${projectDirName}/${fileName}`;
}

export function parseFileKey(fileKey: FileKey): { projectDirName: string; fileName: string } {
  const slashIndex = fileKey.indexOf('/');
  if (slashIndex === -1) throw new Error(`Invalid FileKey: ${fileKey}`);
  return {
    projectDirName: fileKey.slice(0, slashIndex),
    fileName: fileKey.slice(slashIndex + 1),
  };
}

// ── Data Model ──────────────────────────────────────────────────────────

export interface EgregiousnessEval {
  score: number | null;           // score_with_reasoning mean
  scoreNumberOnly: number | null;
  reasoning: string | null;
  results: Array<{ score: number; scoreNumberOnly: number; reasoning: string; thinking?: string }>;
}

export interface IncriminatingEval {
  score: number | null;
  reasoning: string | null;
  results: Array<{ score: number; reasoning: string; thinking?: string }>;
}

export interface ConfidenceEval {
  score: number | null;
  reasoning: string | null;
  results: Array<{ score: number; reasoning: string; thinking?: string }>;
}

export interface EffectivenessEval {
  score: number | null;
  reasoning: string | null;
  results: Array<{ score: number; reasoning: string; thinking?: string }>;
}

export interface RealismEval {
  score: number | null;
  reasoning: string | null;
  results: Array<{ score: number; reasoning: string; thinking?: string }>;
}

export interface VariantEval {
  variant: string;
  score: number | null;
  reasoning: string | null;
  results: Array<{ score: number; reasoning: string; thinking?: string }>;
}

export interface MonitorEval {
  evalId: string | null;
  status: 'idle' | 'running' | 'done' | 'error';
  score: number | null;           // max across variants (was: single variant mean)
  reasoning: string | null;
  results: Array<{ score: number; reasoning: string; thinking?: string }>;
  variantEvals: VariantEval[];    // per-variant results
  egregiousness: EgregiousnessEval | null;
  incriminating: IncriminatingEval | null;
  effectiveness: EffectivenessEval | null;
  confidence: ConfidenceEval | null;
  realism: RealismEval | null;
  transcriptHash: string | null;
  errorMessage: string | null;
}

interface SidecarEvals {
  suspiciousness?: {
    score?: number;
    reasoning?: string;
    results?: Array<{ score: number; reasoning: string; thinking?: string }>;
    variants?: Record<string, { score: number; reasoning: string; results: Array<{ score: number; reasoning: string; thinking?: string }> }>;
  };
  egregiousness?: EgregiousnessEval;
  incriminating?: IncriminatingEval;
  effectiveness?: EffectivenessEval;
  confidence?: ConfidenceEval;
  realism?: RealismEval;
}

/** Reconstruct a MonitorEval from sidecar metadata (scores + evals). */
export function monitorEvalFromSidecar(
  metadata: { scores?: Record<string, number>; evals?: SidecarEvals; transcript_hash?: string } | null | undefined,
): MonitorEval | undefined {
  if (!metadata?.evals && !metadata?.scores) return undefined;

  const evals = metadata.evals ?? {};
  const scores = metadata.scores ?? {};

  // Need at least one score to reconstruct
  if (Object.keys(scores).length === 0 && Object.keys(evals).length === 0) return undefined;

  const { suspiciousness: susp, egregiousness: egr, incriminating: inc, effectiveness: eff, confidence: conf, realism: real } = evals;

  // Reconstruct variant evals from sidecar data
  const variantEvals: VariantEval[] = [];
  if (susp?.variants) {
    for (const [variant, data] of Object.entries(susp.variants)) {
      variantEvals.push({
        variant,
        score: data.score,
        reasoning: data.reasoning,
        results: data.results ?? [],
      });
    }
  }

  // If no variant data but we have per-variant flat scores, reconstruct from those
  if (variantEvals.length === 0) {
    for (const [key, value] of Object.entries(scores)) {
      if (key.startsWith('suspiciousness_') && typeof value === 'number') {
        const variant = key.slice('suspiciousness_'.length);
        variantEvals.push({ variant, score: value, reasoning: null, results: [] });
      }
    }
  }

  return {
    evalId: null,
    status: 'done',
    score: susp?.score ?? scores.suspiciousness ?? null,
    reasoning: susp?.reasoning ?? null,
    results: susp?.results ?? [],
    variantEvals,
    egregiousness: egr
      ? {
          ...egr,
          results: (egr.results ?? []).map((r: EgregiousnessEval['results'][number] & Record<string, unknown>) => ({
            score: r.score,
            reasoning: r.reasoning,
            // Normalize snake_case from old sidecars to camelCase
            scoreNumberOnly: (r.scoreNumberOnly ?? r.score_number_only ?? null) as number,
            ...(r.thinking ? { thinking: r.thinking } : {}),
          })),
        }
      : (scores.egregiousness != null ? { score: scores.egregiousness, scoreNumberOnly: null, reasoning: null, results: [] } : null),
    incriminating: inc ?? (scores.incriminating != null ? { score: scores.incriminating, reasoning: null, results: [] } : null),
    effectiveness: eff ?? (scores.effectiveness != null ? { score: scores.effectiveness, reasoning: null, results: [] } : null),
    confidence: conf ?? (scores.confidence != null ? { score: scores.confidence, reasoning: null, results: [] } : null),
    realism: real ?? (scores.realism != null ? { score: scores.realism, reasoning: null, results: [] } : null),
    transcriptHash: metadata.transcript_hash ?? null,
    errorMessage: null,
  };
}

export interface FileSettings {
  // Placeholder — previously held autoAccept, now empty but kept for migration compat
}

export interface FileState {
  fileKey: FileKey;
  label: string;
  fileName: string;
  messages: Message[];
  transcriptHash: string;
  llmSession: LLMSession;
  monitorEval: MonitorEval;
  settings: FileSettings;
  /** 'edit' = loaded from file/branched, 'create' = creating from prompt */
  mode: 'edit' | 'create';
  /** Pending prompt to auto-submit when file becomes active */
  pendingPrompt: string | null;
  outcome: string | null;
  scenario: string | null;
  mechanism: MechanismDict | null;
  summary: MechanismDict | null;
  /** Suppressed file-consistency warnings (persisted to meta.json) */
  consistencySuppressions: Array<{ id: string; reason: string }>;
  /** True while messages are being fetched from disk */
  loading: boolean;
  /** True once messages + metadata have been loaded from disk */
  hydrated: boolean;
}

export interface Project {
  id: string;
  label: string;
  dirName: string;
}

// ── Helpers ─────────────────────────────────────────────────────────────

function generateId(): string {
  return crypto.randomUUID();
}

function defaultLLMSession(): LLMSession {
  return {
    contentBlocks: [],
    hitLimit: false,
    isStreaming: false,
  };
}

function defaultMonitorEval(): MonitorEval {
  return {
    evalId: null,
    status: 'idle',
    score: null,
    reasoning: null,
    results: [],
    variantEvals: [],
    egregiousness: null,
    incriminating: null,
    effectiveness: null,
    confidence: null,
    realism: null,
    transcriptHash: null,
    errorMessage: null,
  };
}

function createFileState(overrides: Partial<FileState> & { fileKey: FileKey }): FileState {
  const { fileKey, ...rest } = overrides;
  const fileName = rest.fileName ?? parseFileKey(fileKey).fileName;
  return {
    fileKey,
    label: fileName.replace(/\.jsonl$/, ''),
    messages: [],
    transcriptHash: '',
    fileName,
    llmSession: defaultLLMSession(),
    monitorEval: defaultMonitorEval(),
    settings: {},
    mode: 'edit',
    pendingPrompt: null,
    outcome: null,
    scenario: null,
    mechanism: null,
    summary: null,
    consistencySuppressions: [],
    loading: false,
    hydrated: false,
    ...rest,
  };
}

function slugify(text: string): string {
  return text.toLowerCase().replace(/[^\w\s-]/g, '').trim().replace(/[\s]+/g, '-');
}

function generateFileName(files: Record<FileKey, FileState>, projectDirName: string): string {
  const prefix = projectDirName + '/';
  const existingNames = new Set(
    Object.keys(files)
      .filter(k => k.startsWith(prefix))
      .map(k => k.slice(prefix.length))
  );
  let n = 1;
  while (existingNames.has(`untitled-${n}.jsonl`)) {
    n++;
  }
  return `untitled-${n}.jsonl`;
}

function createProject(label: string, dirName: string): Project {
  return {
    id: generateId(),
    label,
    dirName,
  };
}

// ── Settings ────────────────────────────────────────────────────────────

export interface MonitorSettings {
  enabled: boolean;
  autoEvalOnLoad: boolean;
  nEvals: number;
  nEvalsOtherMetrics: number;
  promptVariants: string[];
  tpmDefault: number;
  tpmAlt: number;
  goalScore: number;
  evalEgregiousness: boolean;
  evalIncriminating: boolean;
  evalEffectiveness: boolean;
  evalConfidence: boolean;
  evalRealism: boolean;
  apiKeyId: string;  // 'default' | 'alt'
}

export interface WorkspaceSettings {
  lockFirstMessage: boolean;
  childLock: boolean;
  promptMode: 'creative' | 'faithful';
  creativeDocument: string;
  llmModel: string;
  llmApiKeyId: string;  // 'default' | 'alt'
  monitor: MonitorSettings;
}

// ── Store Interface ─────────────────────────────────────────────────────

interface WorkspaceState {
  projects: Project[];
  files: Record<FileKey, FileState>;
  openFiles: FileKey[];              // ordered list = tab bar
  activeFileKey: FileKey | null;
  activeProjectId: string;
  settings: WorkspaceSettings;
  /** Incremented to trigger immediate sidebar file list re-fetch */
  sidebarRefreshTrigger: number;
  /** Available models per API key, fetched from backend /llm/config (non-persisted) */
  _modelConfig: { keys: Record<string, { models: string[] }> } | null;

  // Project actions
  createProject: (label?: string, dirName?: string) => string;
  deleteProject: (projectId: string) => void;
  renameProject: (projectId: string, label: string, dirName?: string) => void;
  switchProject: (projectId: string) => void;
  ensureProject: (label: string, dirName: string) => string;

  // File actions
  openNewFile: (projectDirName: string, overrides?: Partial<FileState>) => FileKey;
  closeFile: (fileKey: FileKey) => void;
  switchFile: (fileKey: FileKey) => void;
  renameFile: (fileKey: FileKey, label: string) => void;
  moveFile: (fileKey: FileKey, targetDirName: string) => void;

  // File data actions
  setMessages: (fileKey: FileKey, messages: Message[]) => void;
  updateLLMSession: (fileKey: FileKey, session: Partial<LLMSession>) => void;
  setLLMSession: (fileKey: FileKey, session: LLMSession) => void;
  setMonitorEval: (fileKey: FileKey, eval_: Partial<MonitorEval>) => void;
  setFileMode: (fileKey: FileKey, mode: 'edit' | 'create') => void;
  setPendingPrompt: (fileKey: FileKey, prompt: string | null) => void;
  setFileMetadata: (fileKey: FileKey, metadata: { outcome?: string | null; scenario?: string | null; mechanism?: MechanismDict | null; summary?: MechanismDict | null }, transcriptHash?: string) => void;
  setFileHydrated: (fileKey: FileKey, hydrated: boolean) => void;
  updateFileMessages: (fileKey: FileKey, messages: Message[], transcriptHash?: string) => void;
  rehydrateFile: (fileKey: FileKey) => Promise<void>;

  // Convenience getters
  getActiveFile: () => FileState | null;
  getActiveProject: () => Project | null;
  getFile: (fileKey: FileKey) => FileState | null;
  getProjectForFile: (fileKey: FileKey) => Project | null;

  // Bulk actions
  openFile: (projectDirName: string, fileName: string, messages: Message[], monitorEval?: MonitorEval, metadata?: { outcome?: string | null; scenario?: string | null; mechanism?: MechanismDict | null; summary?: MechanismDict | null } | null) => FileKey;

  // Settings
  updateSettings: (settings: Partial<WorkspaceSettings>) => void;
  updateMonitorSettings: (settings: Partial<MonitorSettings>) => void;

  // Sidebar
  triggerSidebarRefresh: () => void;
}

// ── IndexedDB Storage Adapter ───────────────────────────────────────────

const idbStorage = createJSONStorage<WorkspaceState>(() => ({
  getItem: async (name: string) => {
    const value = await idbGet(name);
    return value ?? null;
  },
  setItem: async (name: string, value: string) => {
    await idbSet(name, value);
  },
  removeItem: async (name: string) => {
    await idbDel(name);
  },
}));

// ── Store ───────────────────────────────────────────────────────────────

const defaultProjectId = generateId();

export const useStore = create<WorkspaceState>()(
  persist(
    (set, get) => ({
      projects: [{
        id: defaultProjectId,
        label: 'Default',
        dirName: 'default',
      }],
      files: {},
      openFiles: [],
      activeProjectId: defaultProjectId,
      activeFileKey: null,
      _modelConfig: null,
      sidebarRefreshTrigger: 0,
      settings: {
        lockFirstMessage: false,
        childLock: false,
        promptMode: 'creative',
        creativeDocument: 'CREATIVE.md',
        llmModel: 'claude-opus-4-6',
        llmApiKeyId: 'default',
        monitor: {
          enabled: false,
          autoEvalOnLoad: false,
          nEvals: 5,
          nEvalsOtherMetrics: 1,
          promptVariants: ['control_arena'],
          tpmDefault: 2_000_000,
          tpmAlt: 2_000_000,
          goalScore: 10,
          evalEgregiousness: true,
          evalIncriminating: true,
          evalEffectiveness: true,
          evalConfidence: true,
          evalRealism: true,
          apiKeyId: 'default',
        },
      },

      // ── Project Actions ─────────────────────────────────────────────

      createProject: (label = 'New Project', dirName?: string) => {
        const dn = dirName ?? slugify(label);
        const project = createProject(label, dn);
        set(state => ({
          projects: [...state.projects, project],
          activeProjectId: project.id,
          activeFileKey: null,
        }));
        return project.id;
      },

      deleteProject: (projectId) => {
        const state = get();
        const project = state.projects.find(p => p.id === projectId);
        if (!project) return;
        if (state.projects.length <= 1) return; // Don't delete last project

        const prefix = project.dirName + '/';
        const newFiles = { ...state.files };
        for (const key of Object.keys(newFiles)) {
          if (key.startsWith(prefix)) delete newFiles[key];
        }

        const newOpenFiles = state.openFiles.filter(k => !k.startsWith(prefix));
        const newProjects = state.projects.filter(p => p.id !== projectId);
        const needsNewActive = state.activeProjectId === projectId;

        let newActiveFileKey = state.activeFileKey;
        if (state.activeFileKey?.startsWith(prefix)) {
          newActiveFileKey = newOpenFiles.length > 0 ? newOpenFiles[0] : null;
        }

        set({
          projects: newProjects,
          files: newFiles,
          openFiles: newOpenFiles,
          activeProjectId: needsNewActive ? newProjects[0].id : state.activeProjectId,
          activeFileKey: newActiveFileKey,
        });
      },

      renameProject: (projectId, label, dirName?) => {
        set(state => ({
          projects: state.projects.map(p =>
            p.id === projectId
              ? { ...p, label, ...(dirName !== undefined ? { dirName } : {}) }
              : p
          ),
        }));
      },

      switchProject: (projectId) => {
        const state = get();
        const project = state.projects.find(p => p.id === projectId);
        if (!project) return;

        // Find first open file in this project
        const prefix = project.dirName + '/';
        const firstOpenFile = state.openFiles.find(k => k.startsWith(prefix));

        set({
          activeProjectId: projectId,
          activeFileKey: firstOpenFile ?? null,
        });
      },

      ensureProject: (label, dirName) => {
        const existing = get().projects.find(p => p.dirName === dirName);
        if (existing) return existing.id;

        const project = createProject(label, dirName);
        set(s => ({
          projects: [...s.projects, project],
        }));
        return project.id;
      },

      // ── File Actions ─────────────────────────────────────────────────

      openNewFile: (projectDirName, overrides = {}) => {
        const state = get();
        // Extract `activate` before passing to createFileState
        const { activate = true, ...fileOverrides } = overrides as Partial<FileState> & { activate?: boolean };
        const fileName = fileOverrides.fileName ?? generateFileName(state.files, projectDirName);
        const fileKey = makeFileKey(projectDirName, fileName);
        const project = state.projects.find(p => p.dirName === projectDirName);

        const fileState = createFileState({
          fileKey,
          fileName,
          ...fileOverrides,
        });

        set(s => ({
          files: { ...s.files, [fileKey]: fileState },
          openFiles: [...s.openFiles, fileKey],
          ...(activate ? {
            activeFileKey: fileKey,
            activeProjectId: project?.id ?? s.activeProjectId,
          } : {}),
        }));
        return fileKey;
      },

      closeFile: (fileKey) => {
        const state = get();
        if (!state.openFiles.includes(fileKey)) return;

        const newOpenFiles = state.openFiles.filter(k => k !== fileKey);

        // Pick new active file if we closed the active one
        let newActiveFileKey = state.activeFileKey;
        if (state.activeFileKey === fileKey) {
          const { projectDirName } = parseFileKey(fileKey);
          const prefix = projectDirName + '/';
          const oldProjectFiles = state.openFiles.filter(k => k.startsWith(prefix));
          const newProjectFiles = newOpenFiles.filter(k => k.startsWith(prefix));
          const closedIdx = oldProjectFiles.indexOf(fileKey);

          if (newProjectFiles.length > 0) {
            const newIdx = Math.min(closedIdx, newProjectFiles.length - 1);
            newActiveFileKey = newProjectFiles[newIdx];
          } else {
            newActiveFileKey = null;
          }
        }

        set({
          openFiles: newOpenFiles,
          activeFileKey: newActiveFileKey,
        });
      },

      switchFile: (fileKey) => {
        const state = get();
        if (!state.files[fileKey]) return;

        // Also switch to the project containing this file
        const { projectDirName } = parseFileKey(fileKey);
        const project = state.projects.find(p => p.dirName === projectDirName);
        set({
          activeFileKey: fileKey,
          activeProjectId: project ? project.id : state.activeProjectId,
        });
      },

      renameFile: (fileKey, label) => {
        const state = get();
        const file = state.files[fileKey];
        if (!file) return;

        const { projectDirName } = parseFileKey(fileKey);
        const newFileName = `${label}.jsonl`;
        const newFileKey = makeFileKey(projectDirName, newFileName);

        // If key didn't change, just update label
        if (newFileKey === fileKey) return;

        const newFiles = { ...state.files };
        delete newFiles[fileKey];
        newFiles[newFileKey] = { ...file, fileKey: newFileKey, label, fileName: newFileName };

        set({
          files: newFiles,
          openFiles: state.openFiles.map(k => k === fileKey ? newFileKey : k),
          activeFileKey: state.activeFileKey === fileKey ? newFileKey : state.activeFileKey,
        });
      },

      moveFile: (fileKey, targetDirName) => {
        const state = get();
        const file = state.files[fileKey];
        if (!file) return;

        const { projectDirName: sourceDirName } = parseFileKey(fileKey);
        if (sourceDirName === targetDirName) return;

        const newFileKey = makeFileKey(targetDirName, file.fileName);

        const newFiles = { ...state.files };
        delete newFiles[fileKey];
        newFiles[newFileKey] = { ...file, fileKey: newFileKey };

        set({
          files: newFiles,
          openFiles: state.openFiles.map(k => k === fileKey ? newFileKey : k),
          activeFileKey: state.activeFileKey === fileKey ? newFileKey : state.activeFileKey,
        });
      },

      // ── File Data Actions ────────────────────────────────────────────

      setMessages: (fileKey, messages) => {
        set(state => {
          const file = state.files[fileKey];
          if (!file) return state;

          return {
            files: {
              ...state.files,
              [fileKey]: {
                ...file,
                messages,
                // Hash is computed by backend and delivered via SSE
              },
            },
          };
        });
      },

      updateLLMSession: (fileKey, sessionUpdate) => {
        set(state => {
          const file = state.files[fileKey];
          if (!file) return state;
          return {
            files: {
              ...state.files,
              [fileKey]: {
                ...file,
                llmSession: { ...file.llmSession, ...sessionUpdate },
              },
            },
          };
        });
      },

      setLLMSession: (fileKey, session) => {
        set(state => ({
          files: {
            ...state.files,
            [fileKey]: state.files[fileKey]
              ? { ...state.files[fileKey], llmSession: session }
              : state.files[fileKey],
          },
        }));
      },

      setMonitorEval: (fileKey, evalUpdate) => {
        set(state => {
          const file = state.files[fileKey];
          if (!file) return state;

          const newEval = { ...file.monitorEval, ...evalUpdate };

          return {
            files: {
              ...state.files,
              [fileKey]: { ...file, monitorEval: newEval },
            },
          };
        });
      },

      setFileMode: (fileKey, mode) => {
        set(state => ({
          files: {
            ...state.files,
            [fileKey]: state.files[fileKey]
              ? { ...state.files[fileKey], mode }
              : state.files[fileKey],
          },
        }));
      },

      setPendingPrompt: (fileKey, prompt) => {
        set(state => ({
          files: {
            ...state.files,
            [fileKey]: state.files[fileKey]
              ? { ...state.files[fileKey], pendingPrompt: prompt }
              : state.files[fileKey],
          },
        }));
      },

      setFileMetadata: (fileKey, metadata, transcriptHash) => {
        set(state => {
          const file = state.files[fileKey];
          if (!file) return state;

          const merged = { ...file, ...metadata };
          return {
            files: {
              ...state.files,
              [fileKey]: {
                ...merged,
                // Use backend-provided hash if available, otherwise keep existing
                ...(transcriptHash ? { transcriptHash } : {}),
              },
            },
          };
        });
      },

      setFileHydrated: (fileKey, hydrated) => {
        set(s => {
          const file = s.files[fileKey];
          if (!file) return s;
          return { files: { ...s.files, [fileKey]: { ...file, hydrated } } };
        });
      },

      updateFileMessages: (fileKey, messages, transcriptHash) => {
        set(s => {
          const file = s.files[fileKey];
          if (!file) return s;
          const normalized = normalizeMessages(messages);
          return {
            files: {
              ...s.files,
              [fileKey]: {
                ...file,
                messages: normalized,
                // Use backend-provided hash if available, otherwise keep existing
                ...(transcriptHash ? { transcriptHash } : {}),
                hydrated: true,
                loading: false,
              },
            },
          };
        });
      },

      rehydrateFile: async (fileKey) => {
        const state = get();
        const file = state.files[fileKey];
        if (!file) return;
        if (file.hydrated || file.loading) return;
        // 'create' mode files may not have anything on disk yet —
        // but if they do (agent already wrote), we should still load.
        // Proceed to the normal load path; a 404 will mark hydrated with empty messages.

        // fileKey IS the backend path — no project lookup needed
        const filePath = fileKey;

        // Mark loading
        set(s => ({
          files: {
            ...s.files,
            [fileKey]: s.files[fileKey] ? { ...s.files[fileKey], loading: true } : s.files[fileKey],
          },
        }));

        try {
          const response = await fetch(`/api/files/load/${encodeURIComponent(filePath)}`);
          if (!response.ok) {
            if (response.status === 404) {
              // File not yet saved to disk — mark as hydrated with empty messages
              set(s => ({
                files: {
                  ...s.files,
                  [fileKey]: s.files[fileKey] ? { ...s.files[fileKey], loading: false, hydrated: true } : s.files[fileKey],
                },
              }));
              return;
            }
            throw new Error(`Load failed: ${response.status}`);
          }

          const data = await response.json();

          // Verify file still exists before applying
          const current = get().files[fileKey];
          if (!current) return;

          const messages = normalizeMessages(data.messages);
          if (messages.length === 0 && data.messages.length > 0) {
            console.error(`File ${fileKey}: backend returned ${data.messages.length} messages but normalizeMessages produced 0. Raw format may be unsupported.`);
          }
          const metadata = data.metadata as { outcome?: string; scenario?: string; mechanism?: Record<string, string>; summary?: Record<string, string>; scores?: Record<string, number>; evals?: Record<string, unknown>; transcript_hash?: string; consistency_suppressions?: Array<{ id: string; reason: string }> } | null;
          const restored = monitorEvalFromSidecar(metadata);

          set(s => {
            const prev = s.files[fileKey];
            if (!prev) return s;
            const merged = {
              ...prev,
              messages,
              loading: false,
              hydrated: true,
              ...(metadata?.outcome !== undefined ? { outcome: metadata.outcome } : {}),
              ...(metadata?.scenario !== undefined ? { scenario: metadata.scenario } : {}),
              ...(metadata?.mechanism !== undefined ? { mechanism: metadata.mechanism as unknown as MechanismDict } : {}),
              ...(metadata?.summary !== undefined ? { summary: metadata.summary as unknown as MechanismDict } : {}),
              ...(metadata?.consistency_suppressions ? { consistencySuppressions: metadata.consistency_suppressions } : {}),
              ...(restored ? { monitorEval: restored } : {}),
            };
            return {
              files: {
                ...s.files,
                [fileKey]: {
                  ...merged,
                  // Prefer hash already set by SSE session_state (may arrive before this fetch).
                  // Fall back to '' so SSE can fill it in if it arrives later.
                  transcriptHash: prev.transcriptHash || '',
                },
              },
            };
          });
        } catch (err) {
          // Mark hydrated to stop retry loop — user can close/reopen file to retry
          console.error(`Failed to load file ${fileKey}:`, err);
          set(s => ({
            files: {
              ...s.files,
              [fileKey]: s.files[fileKey] ? { ...s.files[fileKey], loading: false, hydrated: true } : s.files[fileKey],
            },
          }));
        }
      },

      // ── Getters ─────────────────────────────────────────────────────

      getActiveFile: () => {
        const state = get();
        return state.activeFileKey ? state.files[state.activeFileKey] ?? null : null;
      },

      getActiveProject: () => {
        const state = get();
        return state.projects.find(p => p.id === state.activeProjectId) ?? null;
      },

      getFile: (fileKey) => {
        return get().files[fileKey] ?? null;
      },

      getProjectForFile: (fileKey) => {
        const { projectDirName } = parseFileKey(fileKey);
        return get().projects.find(p => p.dirName === projectDirName) ?? null;
      },

      // ── Bulk Actions ────────────────────────────────────────────────

      openFile: (projectDirName, fileName, messages, monitorEval, metadata) => {
        const fileKey = makeFileKey(projectDirName, fileName);
        const state = get();

        // Deduplication: if already open, just switch to it
        if (state.openFiles.includes(fileKey)) {
          const project = state.projects.find(p => p.dirName === projectDirName);
          set({
            activeFileKey: fileKey,
            activeProjectId: project?.id ?? state.activeProjectId,
          });
          return fileKey;
        }

        const project = state.projects.find(p => p.dirName === projectDirName);

        const metaFields = metadata ? { outcome: metadata.outcome ?? null, scenario: metadata.scenario ?? null, mechanism: metadata.mechanism ?? null, summary: metadata.summary ?? null } : {};
        // If opened with empty messages, mark as loading — SSE session_state will fill them in
        const isLoading = messages.length === 0;
        const fileState = createFileState({
          fileKey,
          label: fileName.replace(/\.jsonl$/, ''),
          fileName,
          messages,
          // Hash will be provided by backend via SSE session_state
          transcriptHash: '',
          hydrated: true,
          loading: isLoading,
          ...(monitorEval ? { monitorEval } : {}),
          ...metaFields,
        });

        set(s => ({
          files: { ...s.files, [fileKey]: fileState },
          openFiles: [...s.openFiles, fileKey],
          activeFileKey: fileKey,
          activeProjectId: project?.id ?? s.activeProjectId,
        }));

        return fileKey;
      },

      // ── Settings ────────────────────────────────────────────────────

      updateSettings: (settingsUpdate) => {
        set(state => ({
          settings: { ...state.settings, ...settingsUpdate },
        }));
      },

      updateMonitorSettings: (monitorUpdate) => {
        set(state => ({
          settings: {
            ...state.settings,
            monitor: { ...state.settings.monitor, ...monitorUpdate },
          },
        }));
      },

      triggerSidebarRefresh: () => {
        set(state => ({ sidebarRefreshTrigger: state.sidebarRefreshTrigger + 1 }));
      },
    }),
    {
      name: 'agent-transcript-editor-workspace',
      version: 21,
      storage: idbStorage,
      migrate: (persisted: unknown, version: number) => {
        const state = persisted as Record<string, unknown>;
        if (version <= 0) {
          // Add dirName to existing projects
          const s = state as { projects?: Array<Record<string, unknown>> };
          if (s.projects) {
            s.projects = s.projects.map((p, i) => ({
              ...p,
              dirName: (p.dirName as string) ?? (i === 0 ? 'default' : slugify(String(p.label || 'project'))),
            }));
          }
        }
        if (version <= 1) {
          // Add outcome/mechanism to tabs, egregiousness/incriminating to monitorEval, new monitor settings
          const s = state as { tabs?: Record<string, Record<string, unknown>>; settings?: Record<string, unknown> };
          if (s.tabs) {
            for (const tab of Object.values(s.tabs)) {
              if (tab.outcome === undefined) tab.outcome = null;
              if (tab.mechanism === undefined) tab.mechanism = null;
              const eval_ = tab.monitorEval as Record<string, unknown> | undefined;
              if (eval_) {
                if (eval_.egregiousness === undefined) eval_.egregiousness = null;
                if (eval_.incriminating === undefined) eval_.incriminating = null;
              }
            }
          }
          if (s.settings) {
            const monitor = s.settings.monitor as Record<string, unknown> | undefined;
            if (monitor) {
              if (monitor.evalEgregiousness === undefined) monitor.evalEgregiousness = false;
              if (monitor.evalIncriminating === undefined) monitor.evalIncriminating = false;
            }
          }
        }
        if (version <= 2) {
          // Assign untitled-N.jsonl to any tab with null/missing fileName
          const s = state as { projects?: Array<{ tabIds: string[] }>; tabs?: Record<string, { fileName?: string | null }> };
          if (s.tabs && s.projects) {
            for (const project of s.projects) {
              const usedNames = new Set<string>();
              for (const tabId of project.tabIds) {
                const tab = s.tabs[tabId];
                if (tab?.fileName) usedNames.add(tab.fileName);
              }
              for (const tabId of project.tabIds) {
                const tab = s.tabs[tabId];
                if (tab && !tab.fileName) {
                  let n = 1;
                  while (usedNames.has(`untitled-${n}.jsonl`)) n++;
                  tab.fileName = `untitled-${n}.jsonl`;
                  usedNames.add(tab.fileName);
                }
              }
            }
          }
        }
        if (version <= 3) {
          // Add realism to monitorEval and evalRealism to monitor settings
          const s = state as { tabs?: Record<string, Record<string, unknown>>; settings?: Record<string, unknown> };
          if (s.tabs) {
            for (const tab of Object.values(s.tabs)) {
              const eval_ = tab.monitorEval as Record<string, unknown> | undefined;
              if (eval_) {
                if (eval_.realism === undefined) eval_.realism = null;
              }
            }
          }
          if (s.settings) {
            const monitor = s.settings.monitor as Record<string, unknown> | undefined;
            if (monitor) {
              if (monitor.evalRealism === undefined) monitor.evalRealism = false;
            }
          }
        }
        if (version <= 4) {
          // Convert string mechanism to MechanismDict
          const s = state as { tabs?: Record<string, Record<string, unknown>>; fileMetadata?: unknown };
          if (s.tabs) {
            for (const tab of Object.values(s.tabs)) {
              if (typeof tab.mechanism === 'string' && tab.mechanism) {
                tab.mechanism = { 0: tab.mechanism };
              } else if (tab.mechanism === undefined) {
                tab.mechanism = null;
              }
            }
          }
          if (!s.fileMetadata) {
            s.fileMetadata = {};
          }
        }
        if (version <= 5) {
          // v6: Strip heavyweight data from tabs, stash fileScores/fileMetadata for one-time migration
          const s = state as {
            tabs?: Record<string, Record<string, unknown>>;
            fileScores?: Record<string, unknown>;
            fileMetadata?: Record<string, unknown>;
            _migrateFileScores?: unknown;
            _migrateFileMetadata?: unknown;
          };
          // Stash old data for migration in onRehydrateStorage
          if (s.fileScores && Object.keys(s.fileScores).length > 0) {
            s._migrateFileScores = s.fileScores;
          }
          if (s.fileMetadata && Object.keys(s.fileMetadata).length > 0) {
            s._migrateFileMetadata = s.fileMetadata;
          }
          delete s.fileScores;
          delete s.fileMetadata;

          // Strip heavyweight data from tabs — they'll rehydrate from disk
          if (s.tabs) {
            for (const tab of Object.values(s.tabs)) {
              tab.messages = [];
              tab.llmSession = defaultLLMSession();
              tab.monitorEval = defaultMonitorEval();
              tab.loading = false;
              tab.hydrated = false;
              tab.pendingPrompt = null;
            }
          }
        }
        if (version <= 6) {
          // Add scenario field to tabs
          const s = state as { tabs?: Record<string, Record<string, unknown>> };
          if (s.tabs) {
            for (const tab of Object.values(s.tabs)) {
              if (tab.scenario === undefined) tab.scenario = null;
            }
          }
        }
        if (version <= 7) {
          // Sync label ↔ fileName: label is always derived from fileName
          const s = state as { tabs?: Record<string, Record<string, unknown>> };
          if (s.tabs) {
            for (const tab of Object.values(s.tabs)) {
              if (tab.fileName && typeof tab.fileName === 'string') {
                tab.label = (tab.fileName as string).replace(/\.jsonl$/, '');
              }
            }
          }
        }
        if (version <= 8) {
          // Add LLM model config to settings
          const s = state as { settings?: Record<string, unknown> };
          if (s.settings) {
            if (s.settings.llmModel === undefined) s.settings.llmModel = 'claude-opus-4-6';
            if (s.settings.llmApiKeyId === undefined) s.settings.llmApiKeyId = 'default';
          }
        }
        if (version <= 9) {
          // Add childLock to settings, childLockActive to tab settings
          const s = state as { settings?: Record<string, unknown>; tabs?: Record<string, Record<string, unknown>> };
          if (s.settings) {
            if (s.settings.childLock === undefined) s.settings.childLock = false;
          }
          if (s.tabs) {
            for (const tab of Object.values(s.tabs)) {
              const tabSettings = tab.settings as Record<string, unknown> | undefined;
              if (tabSettings && tabSettings.childLockActive === undefined) {
                tabSettings.childLockActive = false;
              }
            }
          }
        }
        if (version <= 10) {
          // Add transcriptHash to tabs, replace stale with transcriptHash on monitorEval
          const s = state as { tabs?: Record<string, Record<string, unknown>> };
          if (s.tabs) {
            for (const tab of Object.values(s.tabs)) {
              if (tab.transcriptHash === undefined) tab.transcriptHash = '';
              const eval_ = tab.monitorEval as Record<string, unknown> | undefined;
              if (eval_) {
                delete eval_.stale;
                if (eval_.transcriptHash === undefined) eval_.transcriptHash = null;
              }
            }
          }
        }
        if (version <= 11) {
          // ── v12: Migrate from tab-based to file-based state ──────────
          const s = state as {
            projects?: Array<{ id: string; dirName: string; tabIds?: string[] }>;
            tabs?: Record<string, Record<string, unknown>>;
            activeTabId?: string | null;
          };

          const files: Record<string, Record<string, unknown>> = {};
          const openFiles: string[] = [];
          const seen = new Set<string>();

          // Build tabId → projectDirName map for activeTabId resolution
          const tabToProject = new Map<string, string>();

          if (s.projects && s.tabs) {
            for (const project of s.projects) {
              for (const tabId of (project.tabIds ?? [])) {
                tabToProject.set(tabId, project.dirName);
                const tab = s.tabs[tabId];
                if (!tab) continue;
                const fileName = tab.fileName as string;
                if (!fileName) continue;
                const fileKey = `${project.dirName}/${fileName}`;
                if (seen.has(fileKey)) continue; // Skip duplicates
                seen.add(fileKey);

                // Copy tab data, replacing id with fileKey
                const tabObj = tab as Record<string, unknown>;
                const { id: _tabId, ...tabWithoutId } = tabObj;
                tabWithoutId.fileKey = fileKey;
                (files as Record<string, unknown>)[fileKey] = tabWithoutId;
                openFiles.push(fileKey);
              }
              // Remove tabIds from project
              delete (project as Record<string, unknown>).tabIds;
            }
          }

          // Map activeTabId → activeFileKey
          let activeFileKey: string | null = null;
          if (s.activeTabId && s.tabs?.[s.activeTabId]) {
            const activeTab = s.tabs[s.activeTabId];
            const fileName = activeTab.fileName as string;
            const projectDirName = tabToProject.get(s.activeTabId);
            if (fileName && projectDirName) {
              activeFileKey = `${projectDirName}/${fileName}`;
            }
          }

          // Replace old state with new
          (state as Record<string, unknown>).files = files;
          (state as Record<string, unknown>).openFiles = openFiles;
          (state as Record<string, unknown>).activeFileKey = activeFileKey;
          delete (state as Record<string, unknown>).tabs;
          delete (state as Record<string, unknown>).activeTabId;
        }
        if (version <= 12) {
          // ── v13: Migrate promptVariant → promptVariants, add variantEvals ──
          const s = state as { settings?: Record<string, unknown> };
          if (s.settings) {
            const monitor = s.settings.monitor as Record<string, unknown> | undefined;
            if (monitor) {
              // Convert old string promptVariant to array promptVariants
              if (typeof monitor.promptVariant === 'string') {
                monitor.promptVariants = [monitor.promptVariant];
                delete monitor.promptVariant;
              } else if (!monitor.promptVariants) {
                monitor.promptVariants = ['control_arena'];
              }
            }
          }
        }
        if (version <= 13) {
          // ── v14: Add consistencySuppressions to files ──
          const s = state as { files?: Record<string, Record<string, unknown>> };
          if (s.files) {
            for (const file of Object.values(s.files)) {
              if (file.consistencySuppressions === undefined) {
                file.consistencySuppressions = [];
              }
            }
          }
        }
        if (version <= 14) {
          // ── v15: Split confidence out of incriminating into its own eval ──
          const s = state as { files?: Record<string, Record<string, unknown>>; settings?: Record<string, unknown> };
          if (s.files) {
            for (const file of Object.values(s.files)) {
              const eval_ = file.monitorEval as Record<string, unknown> | undefined;
              if (eval_) {
                if (eval_.confidence === undefined) eval_.confidence = null;
                // Remove confidence from incriminating if present (old format)
                const inc = eval_.incriminating as Record<string, unknown> | undefined;
                if (inc) {
                  delete inc.confidence;
                  if (inc.results && Array.isArray(inc.results)) {
                    for (const r of inc.results as Record<string, unknown>[]) {
                      delete r.confidence;
                    }
                  }
                }
              }
            }
          }
          if (s.settings) {
            const monitor = s.settings.monitor as Record<string, unknown> | undefined;
            if (monitor) {
              if (monitor.evalConfidence === undefined) monitor.evalConfidence = false;
            }
          }
        }
        if (version <= 15) {
          // ── v16: Add effectiveness eval ──
          const s = state as { files?: Record<string, Record<string, unknown>>; settings?: Record<string, unknown> };
          if (s.files) {
            for (const file of Object.values(s.files)) {
              const eval_ = file.monitorEval as Record<string, unknown> | undefined;
              if (eval_ && eval_.effectiveness === undefined) {
                eval_.effectiveness = null;
              }
            }
          }
          if (s.settings) {
            const monitor = s.settings.monitor as Record<string, unknown> | undefined;
            if (monitor && monitor.evalEffectiveness === undefined) {
              monitor.evalEffectiveness = true;
            }
          }
        }
        if (version <= 16) {
          // ── v17: Clean up old backendManaged flag (removed), ensure consistencySuppressions ──
          const s = state as { files?: Record<string, Record<string, unknown>> };
          if (s.files) {
            for (const file of Object.values(s.files)) {
              delete file.backendManaged;
              if (file.consistencySuppressions === undefined) file.consistencySuppressions = [];
            }
          }
        }
        if (version <= 17) {
          // ── v18: Add promptMode to settings ──
          const s = state as { settings?: Record<string, unknown> };
          if (s.settings) {
            if (s.settings.promptMode === undefined) s.settings.promptMode = 'creative';
          }
        }
        if (version <= 18) {
          // ── v19: Remove backendManaged (backend is now always source of truth) ──
          const s = state as { files?: Record<string, Record<string, unknown>> };
          if (s.files) {
            for (const file of Object.values(s.files)) {
              delete file.backendManaged;
            }
          }
        }
        if (version <= 19) {
          // ── v20: Initialize apiKeyId for monitor settings (was added without migration) ──
          const s = state as { settings?: Record<string, unknown> };
          if (s.settings) {
            const monitor = s.settings.monitor as Record<string, unknown> | undefined;
            if (monitor && monitor.apiKeyId === undefined) {
              monitor.apiKeyId = 'default';
            }
          }
        }
        if (version <= 20) {
          // ── v21: Stop persisting projects and settings (backend is source of truth) ──
          // Clear stale projects so they're derived from backend on load
          delete (state as Record<string, unknown>).projects;
          // Clear stale settings so they're fetched from backend on mount
          delete (state as Record<string, unknown>).settings;
        }
        return state as unknown as WorkspaceState;
      },
      // On hydration, fix monitor eval state consistency + migrate old data to sidecar
      onRehydrateStorage: () => (state) => {
        if (!state) return;

        // Ensure promptVariants exists (migration may not have run if async hydration)
        const mon = state.settings.monitor as unknown as Record<string, unknown>;
        if (!mon.promptVariants) {
          if (typeof mon.promptVariant === 'string') {
            mon.promptVariants = [mon.promptVariant];
            delete mon.promptVariant;
          } else {
            mon.promptVariants = ['control_arena'];
          }
        }

        for (const file of Object.values(state.files)) {
          if (file.monitorEval.status === 'running') {
            file.monitorEval = { ...file.monitorEval, status: 'idle' };
          }
          if (file.monitorEval.score !== null && file.monitorEval.results.length > 0 && file.monitorEval.status !== 'done') {
            file.monitorEval = { ...file.monitorEval, status: 'done' };
          }
          // Ensure variantEvals exists
          if (!file.monitorEval.variantEvals) {
            file.monitorEval = { ...file.monitorEval, variantEvals: [] };
          }
        }

        // One-time migration: write old fileScores/fileMetadata to sidecar files
        const raw = state as unknown as Record<string, unknown>;
        const migrateScores = raw._migrateFileScores as Record<string, { score: number }> | undefined;
        const migrateMeta = raw._migrateFileMetadata as Record<string, { outcome: string | null; mechanism: Record<string, string> | null }> | undefined;

        if (migrateScores || migrateMeta) {
          // Collect all keys and fire-and-forget POST each
          const allKeys = new Set([
            ...Object.keys(migrateScores ?? {}),
            ...Object.keys(migrateMeta ?? {}),
          ]);

          for (const key of allKeys) {
            const [project, fileName] = key.split(':');
            if (!project || !fileName) continue;

            const body: Record<string, unknown> = { project, file_name: fileName };
            const scoreEntry = migrateScores?.[key];
            if (scoreEntry) {
              body.scores = { suspiciousness: scoreEntry.score };
            }
            const metaEntry = migrateMeta?.[key];
            if (metaEntry) {
              if (metaEntry.outcome) body.outcome = metaEntry.outcome;
              if (metaEntry.mechanism) body.mechanism = metaEntry.mechanism;
            }

            fetch('/api/files/meta', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(body),
            }).catch(() => {});
          }

          // Clear migration data from persisted state
          delete raw._migrateFileScores;
          delete raw._migrateFileMetadata;
        }
      },
      // Only persist files that are in openFiles — closed files exist only in-session memory
      // Only persist UI navigation state — settings come from backend, projects from backend API
      partialize: (state) => ({
        files: Object.fromEntries(
          state.openFiles
            .filter(key => state.files[key])
            .map(key => {
              const file = state.files[key];
              return [
                key,
                {
                  fileKey: key,
                  label: file.label,
                  fileName: file.fileName,
                  mode: file.mode,
                  // Reset to defaults — will rehydrate from disk/SSE
                  messages: [],
                  transcriptHash: '',
                  llmSession: defaultLLMSession(),
                  monitorEval: defaultMonitorEval(),
                  settings: {},
                  pendingPrompt: null,
                  outcome: null,
                  scenario: null,
                  mechanism: null,
                  summary: null,
                  consistencySuppressions: [],
                  loading: false,
                  hydrated: false,
                },
              ];
            })
        ),
        openFiles: state.openFiles,
        activeFileKey: state.activeFileKey,
        activeProjectId: state.activeProjectId,
      } as unknown as WorkspaceState),
    }
  )
);
