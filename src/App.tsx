import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import type { Message, ContentBlock as ContentBlockType } from './types';
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso';
import { useStore, monitorEvalFromSidecar } from './store';
import { normalizeMessages } from './normalizeMessages';
import { MessageItem } from './MessageItem';
import { SearchBar } from './SearchBar';
import { SettingsModal } from './SettingsModal';
import { LLMPanel } from './LLMPanel';
import { MonitorPanel } from './MonitorPanel';
import { TabBar } from './TabBar';
import { TranscriptSummaryBanner } from './TranscriptSummaryBanner';
import { ClassifyTranscriptModal } from './ClassifyTranscriptModal';
import { useAgentSession } from './llm/useAgentSession';
import { checkApiStatus } from './llm/client';
import { triggerMonitorEval, minimizeTranscript, fixToolIds, checkConsistency, verifyToolIds } from './monitor';
import { showToast } from './toast';
import { saveMessages, saveMetadata, putGlobalSettings, reloadFromDisk } from './api';
import { Sidebar } from './Sidebar';
import { LengthExtensionDialog } from './LengthExtensionDialog';
import './App.css';

function getSearchableText(block: ContentBlockType): string {
  switch (block.type) {
    case 'text': return block.text;
    case 'thinking': return block.thinking;
    case 'tool_result': return typeof block.content === 'string' ? block.content : JSON.stringify(block.content);
    case 'tool_use': {
      const isWrite = block.name === 'Write';
      const input = block.input as Record<string, unknown>;
      const writeContent = isWrite ? (input.content as string | undefined) : null;
      if (isWrite && writeContent) {
        const filePath = (input.file_path as string) ?? '';
        return filePath + '\n' + writeContent;
      }
      return JSON.stringify(block.input, null, 2);
    }
    default: {
      const _exhaustive: never = block;
      throw new Error(`Unknown content block type: ${(_exhaustive as { type: string }).type}`);
    }
  }
}

function countMatches(text: string, query: string): number {
  if (!query) return 0;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const regex = new RegExp(escaped, 'gi');
  const matches = text.match(regex);
  return matches ? matches.length : 0;
}

/** Returns match counts per block for each message, and total across all messages */
function computeMatchData(messages: Message[], query: string) {
  const perMessage: { perBlock: number[]; total: number }[] = [];
  let grandTotal = 0;

  for (const msg of messages) {
    const perBlock: number[] = [];
    let msgTotal = 0;
    const blocks = Array.isArray(msg.content) ? msg.content : [{ type: 'text' as const, text: String(msg.content) }];
    for (const block of blocks) {
      const text = getSearchableText(block);
      const count = countMatches(text, query);
      perBlock.push(count);
      msgTotal += count;
    }
    perMessage.push({ perBlock, total: msgTotal });
    grandTotal += msgTotal;
  }

  return { perMessage, grandTotal };
}

function App() {
  // ── Store selectors ───────────────────────────────────────────────
  const activeFileKey = useStore(s => s.activeFileKey);
  const activeFile = useStore(s => s.activeFileKey ? s.files[s.activeFileKey] ?? null : null);

  const openFile = useStore(s => s.openFile);
  const rehydrateFile = useStore(s => s.rehydrateFile);
  const triggerSidebarRefresh = useStore(s => s.triggerSidebarRefresh);

  // ── Hash-based deep linking ─────────────────────────────────────────
  // Track whether the initial hash has been applied yet.
  // Until it has, don't let activeFileKey overwrite the URL hash.
  const hashAppliedRef = useRef(false);

  // Sync activeFileKey → hash (but only after initial hash has been processed)
  useEffect(() => {
    if (activeFileKey && hashAppliedRef.current) {
      window.location.hash = `#/${activeFileKey}`;
    }
  }, [activeFileKey]);

  // Open the file specified in the URL hash.
  useEffect(() => {
    const openFromHash = () => {
      const hash = window.location.hash;
      if (!hash || hash.length <= 2) return;
      const path = hash.slice(2);
      const slashIndex = path.indexOf('/');
      if (slashIndex === -1) return;
      const projectDirName = decodeURIComponent(path.slice(0, slashIndex));
      const fileName = decodeURIComponent(path.slice(slashIndex + 1));
      if (!projectDirName || !fileName) return;

      const store = useStore.getState();
      const fileKey = `${projectDirName}/${fileName}`;
      if (store.activeFileKey === fileKey) {
        hashAppliedRef.current = true;
        return;
      }
      store.ensureProject(projectDirName, projectDirName);
      openFile(projectDirName, fileName, []);
      hashAppliedRef.current = true;
    };

    // On mount: wait for Zustand persist to finish rehydrating from IndexedDB,
    // then override activeFileKey with the URL hash.
    const unsub = useStore.persist.onFinishHydration(() => {
      openFromHash();
      unsub();
    });
    // If already hydrated (e.g. synchronous storage), run now
    if (useStore.persist.hasHydrated()) {
      openFromHash();
      unsub();
    }

    // On subsequent hash changes in the same tab
    window.addEventListener('hashchange', openFromHash);
    return () => window.removeEventListener('hashchange', openFromHash);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const goalScore = useStore(s => s.settings.monitor.goalScore);
  const showEgregiousness = useStore(s => s.settings.monitor.evalEgregiousness);
  const showIncriminating = useStore(s => s.settings.monitor.evalIncriminating);
  const showEffectiveness = useStore(s => s.settings.monitor.evalEffectiveness);
  const showConfidence = useStore(s => s.settings.monitor.evalConfidence);
  const showRealism = useStore(s => s.settings.monitor.evalRealism);
  const promptVariants = useStore(s => s.settings.monitor.promptVariants);
  const rateLimitEnabled = useStore(s => s.settings.monitor.rateLimitEnabled);
  const activeProjectDirName = useStore(s => {
    const p = s.projects.find(pr => pr.id === s.activeProjectId);
    return p?.dirName ?? 'default';
  });

  const fileHydrated = activeFile?.hydrated ?? false;
  const fileLoading = activeFile?.loading ?? false;

  // Derived state from active file
  const messages = activeFile?.messages ?? [];
  const transcriptHash = activeFile?.transcriptHash ?? '';
  const fileName = activeFile?.fileName ?? '';
  const llmSession = activeFile?.llmSession ?? {
    contentBlocks: [],
    hitLimit: false,
    isStreaming: false,
  };
  const monitorEval = activeFile?.monitorEval ?? {
    evalId: null,
    status: 'idle' as const,
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
    runningMetrics: null,
  };

  // ── Local UI state ────────────────────────────────────────────────
  const [dragging, setDragging] = useState(false);
  const [searchFocusCount, setSearchFocusCount] = useState(0);
  const [searchQuery, setSearchQuery] = useState('');
  const [currentMatchIndex, setCurrentMatchIndex] = useState(0);
  const [showSettings, setShowSettings] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const [tokenCount, setTokenCount] = useState<number | null>(null);
  const [tokenCountLoading, setTokenCountLoading] = useState(false);
  const [showJumpInput, setShowJumpInput] = useState(false);
  const [jumpValue, setJumpValue] = useState('');
  const [showLengthExtension, setShowLengthExtension] = useState(false);
  const [showBranchInput, setShowBranchInput] = useState(false);
  const [branchValue, setBranchValue] = useState('');
  const jumpInputRef = useRef<HTMLInputElement>(null);
  const branchInputRef = useRef<HTMLInputElement>(null);
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const [showCreation, setShowCreation] = useState(false);
  const [creationPrompt, setCreationPrompt] = useState('');
  const [rightPanelTab, setRightPanelTab] = useState<'assistant' | 'monitor'>('assistant');
  const [backendReady, setBackendReady] = useState<boolean | null>(null);
  const [showClassifyModal, setShowClassifyModal] = useState(false);
  const [rateLimitStatus, setRateLimitStatus] = useState<Record<string, { used_tpm: number; limit_tpm: number; queued: number }> | null>(null);

  // ── Panel resize state ──────────────────────────────────────────
  const [sidebarWidth, setSidebarWidth] = useState(240);
  const [rightPanelWidth, setRightPanelWidth] = useState(420);
  const resizingRef = useRef<'sidebar' | 'right' | null>(null);
  const resizeStartXRef = useRef(0);
  const resizeStartWidthRef = useRef(0);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!resizingRef.current) return;
      e.preventDefault();
      const delta = e.clientX - resizeStartXRef.current;
      if (resizingRef.current === 'sidebar') {
        setSidebarWidth(Math.max(160, Math.min(500, resizeStartWidthRef.current + delta)));
      } else {
        // Right panel: dragging left increases width
        setRightPanelWidth(Math.max(280, Math.min(800, resizeStartWidthRef.current - delta)));
      }
    };
    const handleMouseUp = () => {
      if (resizingRef.current) {
        resizingRef.current = null;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      }
    };
    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
  }, []);

  const startResize = useCallback((panel: 'sidebar' | 'right', e: React.MouseEvent) => {
    resizingRef.current = panel;
    resizeStartXRef.current = e.clientX;
    resizeStartWidthRef.current = panel === 'sidebar' ? sidebarWidth : rightPanelWidth;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, [sidebarWidth, rightPanelWidth]);


  // ── Search ────────────────────────────────────────────────────────
  const matchData = useMemo(
    () => computeMatchData(messages, searchQuery),
    [messages, searchQuery]
  );
  const totalMatches = matchData.grandTotal;

  useEffect(() => {
    if (totalMatches === 0) {
      setCurrentMatchIndex(0);
    } else if (currentMatchIndex >= totalMatches) {
      setCurrentMatchIndex(0);
    }
  }, [totalMatches, currentMatchIndex]);

  // Find which message index contains a given global match index
  const messageIndexForMatch = useCallback((matchIdx: number): number => {
    let cumulative = 0;
    for (let i = 0; i < matchData.perMessage.length; i++) {
      cumulative += matchData.perMessage[i].total;
      if (matchIdx < cumulative) return i;
    }
    return 0;
  }, [matchData]);

  useEffect(() => {
    if (totalMatches === 0 || !searchQuery) return;
    // First scroll the virtuoso list to the right message (so it renders)
    const msgIdx = messageIndexForMatch(currentMatchIndex);
    virtuosoRef.current?.scrollToIndex({ index: msgIdx, align: 'center', behavior: 'smooth' });
    // Then highlight the specific match element after it renders
    const timer = setTimeout(() => {
      const el = document.querySelector(`[data-match-index="${currentMatchIndex}"]`);
      if (el) {
        el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    }, 150);
    return () => clearTimeout(timer);
  }, [currentMatchIndex, totalMatches, searchQuery, messageIndexForMatch]);

  // Clear search when switching files
  const prevFileKeyRef = useRef(activeFileKey);
  if (prevFileKeyRef.current !== activeFileKey) {
    prevFileKeyRef.current = activeFileKey;
    if (searchQuery) {
      setSearchQuery('');
      setCurrentMatchIndex(0);
    }
  }

  // Keyboard shortcuts: Ctrl/Cmd+F for search, Ctrl/Cmd+G for jump-to-message
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault();
        setShowSearch(true);
        setSearchFocusCount(c => c + 1);
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'g') {
        e.preventDefault();
        setShowJumpInput(s => !s);
        setJumpValue('');
      }
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'R') {
        e.preventDefault();
        const fk = useStore.getState().activeFileKey;
        if (fk) {
          reloadFromDisk(fk).catch(err => showToast(`Reload failed: ${err.message}`, 'warning'));
        }
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  // Focus inputs when their bars open (since they're always-rendered now)
  useEffect(() => {
    if (showJumpInput) setTimeout(() => jumpInputRef.current?.focus(), 60);
  }, [showJumpInput]);

  useEffect(() => {
    if (showBranchInput) setTimeout(() => branchInputRef.current?.focus(), 60);
  }, [showBranchInput]);

  // Check backend status, fetch model config, and load global settings from backend on mount
  useEffect(() => {
    checkApiStatus().then(status => setBackendReady(status.configured));
    fetch('/api/llm/config')
      .then(r => r.json())
      .then(config => {
        useStore.setState({ _modelConfig: config });
        // Reset key ID if the persisted one isn't available in the backend config
        const { llmApiKeyId } = useStore.getState().settings;
        if (config?.keys && !(llmApiKeyId in config.keys)) {
          putGlobalSettings({ llm_api_key_id: 'default' });
        }
      })
      .catch(() => {});
    // Fetch global settings from backend (source of truth) and populate store
    fetch('/api/session/global-settings')
      .then(r => r.json())
      .then((gs: Record<string, unknown>) => {
        const store = useStore.getState();
        store.updateSettings({
          lockFirstMessage: gs.lock_first_message as boolean,
          childLock: gs.child_lock_enabled as boolean,
          promptMode: gs.prompt_mode as 'creative' | 'faithful',

          llmModel: gs.llm_model as string,
          llmApiKeyId: gs.llm_api_key_id as string,
        });
        store.updateMonitorSettings({
          nEvals: gs.n_evals as number,
          nEvalsOtherMetrics: gs.n_evals_other as number ?? 1,
          promptVariants: gs.prompt_variants as string[],
          evalEgregiousness: gs.eval_egregiousness as boolean,
          evalIncriminating: gs.eval_incriminating as boolean,
          evalEffectiveness: gs.eval_effectiveness as boolean,
          evalConfidence: gs.eval_confidence as boolean,
          evalRealism: gs.eval_realism as boolean,
          apiKeyId: gs.monitor_api_key_id as string,
          goalScore: gs.goal_score as number,
          enabled: gs.auto_eval_enabled as boolean,
          autoEvalOnLoad: gs.auto_eval_on_load as boolean,
          rateLimitEnabled: gs.rate_limit_enabled as boolean,
          tpmDefault: gs.tpm_default as number,
          tpmAlt: gs.tpm_alt as number,
        });
      })
      .catch(() => {});
  }, []);

  // ── Poll rate limit status (display only) ─────────
  useEffect(() => {
    let mounted = true;
    const poll = async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 3000);
      try {
        const res = await fetch('/api/config/rate-limit', { signal: controller.signal });
        if (res.ok && mounted) {
          const data = await res.json();
          setRateLimitStatus(data);
        }
      } catch { /* backend unavailable or timeout */ }
      finally { clearTimeout(timeout); }
    };
    poll();
    const interval = setInterval(poll, 2000);
    return () => { mounted = false; clearInterval(interval); };
  }, []);

  // ── Rehydrate active file from disk on switch ─────────────────────
  useEffect(() => {
    if (activeFileKey && !fileHydrated && !fileLoading) {
      rehydrateFile(activeFileKey);
    }
  }, [activeFileKey, fileHydrated, fileLoading, rehydrateFile]);

  // ── Token count (debounced, tools-only) ─────────────────────────
  useEffect(() => {
    if (messages.length === 0) {
      setTokenCount(null);
      return;
    }
    setTokenCountLoading(true);
    const timer = setTimeout(async () => {
      try {
        const res = await fetch('/api/monitor/token-count', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages }),
        });
        if (res.ok) {
          const data = await res.json();
          setTokenCount(data.token_count);
        }
      } catch { /* backend unavailable */ }
      setTokenCountLoading(false);
    }, 1000);
    return () => clearTimeout(timer);
  }, [messages]);

  // ── Jump to message ──────────────────────────────────────────────
  const jumpToMessage = useCallback((index: number) => {
    virtuosoRef.current?.scrollToIndex({ index, align: 'center', behavior: 'smooth' });
    // Highlight after scroll completes and element renders
    setTimeout(() => {
      const el = document.querySelector(`[data-message-index="${index}"]`);
      if (el) {
        el.classList.add('message-jump-highlight');
        setTimeout(() => el.classList.remove('message-jump-highlight'), 1500);
      }
    }, 200);
    setShowJumpInput(false);
  }, []);

  const handleJumpSubmit = useCallback(() => {
    const num = parseInt(jumpValue);
    if (!isNaN(num) && num >= 0 && num < messages.length) {
      jumpToMessage(num);
    }
    setShowJumpInput(false);
  }, [jumpValue, messages.length, jumpToMessage]);

  const fileMode = activeFile?.mode ?? 'edit';

  // ── Agent session hook (file-scoped, SSE + HTTP) ────────────────────────
  const { submit, stop, reset, updateSettings: updateWsSettings } = useAgentSession({
    fileKey: activeFileKey,
  });

  // Sync per-session settings to backend session whenever they change
  useEffect(() => {
    updateWsSettings({
      mode: fileMode,
    });
  }, [fileMode, updateWsSettings]);

  // Auto-submit pending prompt when switching to a file with one
  const pendingPrompt = activeFile?.pendingPrompt ?? null;
  useEffect(() => {
    if (activeFileKey && pendingPrompt && backendReady && !llmSession.isStreaming) {
      useStore.getState().setPendingPrompt(activeFileKey, null);
      submit(pendingPrompt);
    }
  }, [activeFileKey, pendingPrompt, backendReady, llmSession.isStreaming, submit]);

  const handleClearChat = () => {
    reset();
  };

  const handleSubmit = (prompt: string) => {
    if (!backendReady) {
      setShowSettings(true);
      return;
    }
    submit(prompt);
  };

  const handleApplyAndClear = (prompt: string) => {
    if (!backendReady) {
      setShowSettings(true);
      return;
    }
    reset();
    setTimeout(() => submit(prompt), 0);
  };

  const handleForkWithPrompt = async (prompt: string) => {
    if (!activeFileKey || messages.length === 0) return;
    const baseName = fileName.replace(/\.jsonl$/, '');
    const forkName = `${baseName}-fork.jsonl`;

    // Check if file already exists
    const checkRes = await fetch(`/api/files/load/${activeProjectDirName}/${forkName}`);
    if (checkRes.ok) {
      showToast(`File "${forkName}" already exists — choose a different name`, 'warning');
      return;
    }

    try {
      const res = await fetch('/api/files/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: forkName,
          messages,
          project: activeProjectDirName,
        }),
      });
      if (!res.ok) throw new Error('Save failed');

      const inheritedEval = monitorEval.status === 'done'
        ? { ...monitorEval, results: [...monitorEval.results] }
        : undefined;
      const newFileKey = openFile(activeProjectDirName, forkName, [...messages], inheritedEval);

      if (activeFile?.outcome || activeFile?.scenario || activeFile?.mechanism || activeFile?.summary) {
        saveMetadata(newFileKey, { outcome: activeFile.outcome, scenario: activeFile.scenario, mechanism: activeFile.mechanism, summary: activeFile.summary });
      }

      useStore.getState().setPendingPrompt(newFileKey, prompt);
    } catch { /* ignore */ }
  };

  // ── File handling ─────────────────────────────────────────────────
  const handleLoadFile = useCallback((file: File) => {
    const reader = new FileReader();
    reader.onload = async (e) => {
      const text = e.target?.result as string;
      const lines = text.trim().split('\n').filter(Boolean);
      const raw = lines.map(line => JSON.parse(line));

      // Step 1: Minimize via backend (handles full Claude Code → minimal conversion)
      let messages: Message[];
      try {
        const minimized = await minimizeTranscript(raw);
        messages = normalizeMessages(minimized.messages);
        if (minimized.was_full_format) {
          showToast(`Auto-minimized from full Claude Code format (${minimized.original_count} → ${minimized.minimized_count} messages)`, 'info');
        }
      } catch {
        // Backend unavailable — fall back to local normalizeMessages
        messages = normalizeMessages(raw);
      }

      // Step 2: Fix tool IDs synchronously (invariant: all transcripts have valid IDs)
      try {
        const fixed = await fixToolIds(messages);
        const fixCount = Object.entries(fixed.id_map).filter(([old, n]) => old !== n).length;
        if (fixCount > 0) {
          messages = fixed.messages as Message[];
          showToast(`Fixed ${fixCount} tool ID${fixCount === 1 ? '' : 's'} on import`, 'warning');
        }
      } catch {
        // Backend unavailable — proceed with unfixed IDs
      }

      // Step 3: Upload minimized version to backend project dir
      try {
        const content = messages.map(m => JSON.stringify(m)).join('\n');
        const blob = new Blob([content], { type: 'application/jsonl' });
        const minimizedFile = new File([blob], file.name, { type: 'application/jsonl' });
        const formData = new FormData();
        formData.append('file', minimizedFile);
        await fetch(`/api/files/upload?project=${encodeURIComponent(activeProjectDirName)}`, {
          method: 'POST',
          body: formData,
        });
      } catch { /* ignore */ }

      // Check if sidecar metadata exists on disk
      let metadata: { outcome?: string | null; scenario?: string | null; mechanism?: Record<string, string> | null } | null = null;
      try {
        const loadRes = await fetch(`/api/files/load/${encodeURIComponent(`${activeProjectDirName}/${file.name}`)}`);
        if (loadRes.ok) {
          const loadData = await loadRes.json();
          metadata = loadData.metadata;
        }
      } catch { /* ignore */ }

      // Load into a new file in the active project
      const restored = monitorEvalFromSidecar(metadata as Record<string, unknown> | null);
      const newFileKey = openFile(activeProjectDirName, file.name, messages, restored, metadata);

      // Auto-evaluate if monitor auto-eval on load is enabled (skip if restored from sidecar)
      if (!restored && useStore.getState().settings.monitor.autoEvalOnLoad) {
        triggerMonitorEval(newFileKey);
      }

      // Show classify modal for new files without sidecar metadata
      if (!metadata) {
        setShowClassifyModal(true);
      }
    };
    reader.readAsText(file);
  }, [activeProjectDirName, openFile]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    if (file) handleLoadFile(file);
  }, [handleLoadFile]);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) handleLoadFile(file);
  }, [handleLoadFile]);

  const exportFile = () => {
    const content = messages.map(m => JSON.stringify(m)).join('\n');
    const blob = new Blob([content], { type: 'application/jsonl' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = fileName;
    a.click();
    URL.revokeObjectURL(url);
  };

  const startBranch = () => {
    if (!activeFileKey || messages.length === 0) return;
    const baseName = fileName.replace(/\.jsonl$/, '');
    setBranchValue(`${baseName}-copy.jsonl`);
    setShowBranchInput(true);
  };

  const commitBranch = async () => {
    const newName = branchValue.trim();
    setShowBranchInput(false);
    if (!newName) return;

    try {
      const res = await fetch('/api/files/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: newName,
          messages,
          project: activeProjectDirName,
        }),
      });
      if (!res.ok) throw new Error('Save failed');

      // Open the branch in a new file, inheriting the monitor score
      const inheritedEval = monitorEval.status === 'done'
        ? { ...monitorEval, results: [...monitorEval.results] }
        : undefined;
      const newFileKey = openFile(activeProjectDirName, newName.endsWith('.jsonl') ? newName : `${newName}.jsonl`, [...messages], inheritedEval);

      // Copy outcome/scenario/mechanism/summary to branched file
      if (activeFile?.outcome || activeFile?.scenario || activeFile?.mechanism || activeFile?.summary) {
        saveMetadata(newFileKey, { outcome: activeFile.outcome, scenario: activeFile.scenario, mechanism: activeFile.mechanism, summary: activeFile.summary });
      }

      // Refresh sidebar so the new file appears in the file list
      triggerSidebarRefresh();
    } catch { /* ignore */ }
  };

  const updateMessage = (index: number, message: Message) => {
    if (!activeFileKey) return;
    const newMessages = messages.map((m, i) => i === index ? message : m);
    saveMessages(activeFileKey, newMessages);
    // SSE will deliver the update to the store
  };

  const deleteMessage = (index: number) => {
    if (!activeFileKey) return;
    const newMessages = messages.filter((_, i) => i !== index);
    saveMessages(activeFileKey, newMessages, { type: 'delete', start: index + 1, end: index + 1 });
  };

  const moveMessage = (index: number, direction: 'up' | 'down') => {
    if (!activeFileKey) return;
    const newIndex = direction === 'up' ? index - 1 : index + 1;
    if (newIndex < 0 || newIndex >= messages.length) return;
    const copy = [...messages];
    [copy[index], copy[newIndex]] = [copy[newIndex], copy[index]];

    const fromUi = index + 1;
    const toUi = direction === 'up' ? index : index + 2;
    saveMessages(activeFileKey, copy, { type: 'move', fromStart: fromUi, fromEnd: fromUi, to: toUi });
  };

  const insertMessage = (index: number) => {
    if (!activeFileKey) return;
    const role = messages[index - 1]?.role === 'user' ? 'assistant' : 'user';
    const newMessage: Message = {
      role,
      content: [{ type: 'text', text: '' }],
      cwd: '',
    };
    const newMessages = [...messages.slice(0, index), newMessage, ...messages.slice(index)];
    saveMessages(activeFileKey, newMessages, { type: 'insert', index: index + 1 });
  };

  const runCheck = async () => {
    if (!messages.length) return;
    try {
      const [consistency, toolIdVerify] = await Promise.all([
        checkConsistency(messages),
        verifyToolIds(messages),
      ]);
      const issues: string[] = [];
      for (const w of consistency.warnings) {
        issues.push(`[msg ${w.message_index}] ${w.message}`);
      }
      for (const i of toolIdVerify.issues) {
        issues.push(`[msg ${i.line_number}] ${i.issue}: ${i.id_value}`);
      }
      if (issues.length === 0) {
        showToast('No issues found', 'success');
      } else {
        showToast(`${issues.length} issue(s) found:\n${issues.join('\n')}`, 'warning');
      }
    } catch (e) {
      showToast(`Check failed: ${e instanceof Error ? e.message : e}`, 'warning');
    }
  };

  // Search handlers
  const handleSearchQueryChange = useCallback((query: string) => {
    setSearchQuery(query);
    setCurrentMatchIndex(0);
  }, []);

  const handleSearchNext = useCallback(() => {
    if (totalMatches === 0) return;
    setCurrentMatchIndex(prev => (prev + 1) % totalMatches);
  }, [totalMatches]);

  const handleSearchPrev = useCallback(() => {
    if (totalMatches === 0) return;
    setCurrentMatchIndex(prev => (prev - 1 + totalMatches) % totalMatches);
  }, [totalMatches]);

  const handleSearchClose = useCallback(() => {
    setShowSearch(false);
    setSearchQuery('');
    setCurrentMatchIndex(0);
  }, []);

  function messageMatchOffset(msgIndex: number): number {
    let offset = 0;
    for (let i = 0; i < msgIndex; i++) {
      offset += matchData.perMessage[i].total;
    }
    return offset;
  }

  // ── Creation from prompt ────────────────────────────────────────────
  const handleCreate = () => {
    if (!creationPrompt.trim() || !activeFileKey) return;
    const store = useStore.getState();

    // Set the current file to creation mode.
    // IMPORTANT: Do NOT call renameFile here — it changes the file key,
    // which triggers an SSE reconnect to a different session.
    // The submit() below sends the prompt using the old file key from
    // this render's closure, so renaming before submit causes the agent
    // to run on a disconnected session.
    // The agent can rename the file via set_tab_label after creation.
    store.setFileMode(activeFileKey, 'create');

    // Submit the creation prompt
    handleSubmit(`Create a transcript where: ${creationPrompt.trim()}`);
    setShowCreation(false);
    setCreationPrompt('');
  };

  // ── Empty state ───────────────────────────────────────────────
  const showEmptyState = !activeFile || (messages.length === 0 && !llmSession.isStreaming && !fileLoading);
  // Detect format error: file is hydrated (loaded from disk) but normalization produced 0 messages
  // Don't show if the file was genuinely empty on disk (new file or empty .jsonl)
  const hasFormatError = activeFile && fileHydrated && !fileLoading && messages.length === 0 && !llmSession.isStreaming && activeFile.mode === 'edit' && !activeFile.diskWasEmpty;

  const dropzoneContent = (
    <div
      className={`dropzone ${dragging ? 'dragging' : ''}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => document.getElementById('file-input')?.click()}
    >
      <div className="dropzone-icon">📄</div>
      <div>Drop a .jsonl file or click to select</div>
      <div className="hint">Supports minimal and full Claude Code JSONL</div>
      <input
        id="file-input"
        type="file"
        accept=".jsonl"
        onChange={handleFileSelect}
        hidden
      />
    </div>
  );

  return (
    <div className="app">
      <header>
        <h1>Agent Transcript Editor</h1>
        <div className="toolbar">
          {!showEmptyState && (
            <>
              <span className="filename">{fileName}</span>
              <span className="count">
                {messages.length} msgs{tokenCount !== null && (
                  <span className="toolbar-tokens"> · {tokenCountLoading ? '...' : `${(tokenCount / 1000).toFixed(1)}K tok`}</span>
                )}
              </span>
              <button
                className={showSearch ? 'toolbar-active' : ''}
                onClick={() => { setShowSearch(s => !s); if (!showSearch) setSearchFocusCount(c => c + 1); }}
                title="Search (Ctrl+F)"
              >
                Search
              </button>
              <button
                className={showJumpInput ? 'toolbar-active' : ''}
                onClick={() => { setShowJumpInput(s => !s); setJumpValue(''); }}
                title="Go to message (Ctrl+G)"
              >
                Go to #
              </button>
              <button className={showBranchInput ? 'toolbar-active' : ''} onClick={startBranch} title="Create a copy of this transcript">Duplicate</button>
              <button onClick={exportFile}>Export</button>
              <button onClick={() => setShowLengthExtension(true)}>Extend</button>
            </>
          )}
          {rateLimitStatus && rateLimitEnabled && (
            <span
              className="api-workers-badge"
              style={Object.values(rateLimitStatus).some(v => v.used_tpm > 0 || v.queued > 0) ? undefined : { opacity: 0.5 }}
              title={Object.entries(rateLimitStatus).map(([k, v]) => `${k}: ${Math.round(v.used_tpm / 1000)}k/${Math.round(v.limit_tpm / 1000)}k TPM${v.queued > 0 ? ` (${v.queued} queued)` : ''}`).join(' | ')}
            >
              {Object.entries(rateLimitStatus).map(([k, v]) =>
                `${k}: ${Math.round(v.used_tpm / 1000)}k/${Math.round(v.limit_tpm / 1000)}k`
              ).join(' · ')}
            </span>
          )}
          {backendReady === false && (
            <span className="backend-warning" title="Backend API key not configured">⚠️</span>
          )}
          {!showEmptyState && <button onClick={runCheck} title="Check for file consistency and tool ID issues">Check</button>}
          <button onClick={() => setShowSettings(true)}>Settings</button>
        </div>
      </header>

      <TabBar />

      <div className={`jump-bar-wrapper ${showJumpInput ? 'open' : ''}`}>
        <div className="jump-bar">
          <label>Go to message #</label>
          <input
            ref={jumpInputRef}
            type="number"
            min={0}
            max={messages.length - 1}
            value={jumpValue}
            onChange={e => setJumpValue(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') handleJumpSubmit();
              if (e.key === 'Escape') setShowJumpInput(false);
            }}
            placeholder={`0–${messages.length - 1}`}
          />
          <button onClick={handleJumpSubmit}>Go</button>
          <button onClick={() => setShowJumpInput(false)} className="jump-close">×</button>
        </div>
      </div>

      <div className={`jump-bar-wrapper ${showBranchInput ? 'open' : ''}`}>
        <div className="jump-bar">
          <label>Branch name</label>
          <input
            ref={branchInputRef}
            value={branchValue}
            onChange={e => setBranchValue(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') commitBranch();
              if (e.key === 'Escape') setShowBranchInput(false);
            }}
            style={{ minWidth: '220px' }}
          />
          <button onClick={commitBranch}>Create</button>
          <button onClick={() => setShowBranchInput(false)} className="jump-close">×</button>
        </div>
      </div>

      {!showEmptyState && (
        <div className={`search-bar-wrapper ${showSearch ? 'open' : ''}`}>
          <SearchBar
            currentMatch={currentMatchIndex}
            totalMatches={totalMatches}
            onQueryChange={handleSearchQueryChange}
            onNext={handleSearchNext}
            onPrev={handleSearchPrev}
            onClose={handleSearchClose}
            focusTrigger={searchFocusCount}
          />
        </div>
      )}
      <div className="app-body">
        <Sidebar style={{ width: sidebarWidth }} />
        <div className={`resize-handle${resizingRef.current === 'sidebar' ? ' dragging' : ''}`} onMouseDown={e => startResize('sidebar', e)} />
        {showEmptyState ? (
          <main style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', paddingTop: '2rem', flex: 1 }}>
            {hasFormatError && (
              <div style={{
                background: 'rgba(239, 68, 68, 0.1)',
                border: '1px solid rgba(239, 68, 68, 0.3)',
                borderRadius: '8px',
                padding: '12px 16px',
                marginBottom: '16px',
                maxWidth: '600px',
                width: '100%',
                color: 'var(--text-primary)',
                fontSize: 'var(--text-sm)',
              }}>
                <strong style={{ color: '#ef4444' }}>Failed to parse transcript:</strong>{' '}
                <span>"{activeFile?.fileName}" loaded but produced 0 messages. Expected minimized JSONL with <code>{'{role, content}'}</code> entries (see TRANSCRIPT_FORMAT.md).</span>
              </div>
            )}
            {dropzoneContent}
            <div className="creation-option">
              <span style={{ color: 'var(--text-muted)', fontSize: 'var(--text-sm)' }}>or</span>
              <button className="creation-btn" onClick={() => setShowCreation(true)}>
                Create from prompt
              </button>
            </div>
            {showCreation && (
              <div className="creation-dialog">
                <textarea
                  value={creationPrompt}
                  onChange={e => setCreationPrompt(e.target.value)}
                  placeholder="Describe the scenario... e.g. 'A Claude Code session where the user asks to refactor a Python API'"
                  autoFocus
                  onKeyDown={e => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      handleCreate();
                    }
                  }}
                />
                <div style={{ display: 'flex', gap: 'var(--space-2)', justifyContent: 'flex-end' }}>
                  <button onClick={() => { setShowCreation(false); setCreationPrompt(''); }} style={{
                    background: 'transparent', border: '1px solid var(--border-default)',
                    color: 'var(--text-secondary)', padding: '6px 16px', borderRadius: '6px',
                    cursor: 'pointer', fontSize: 'var(--text-sm)', fontFamily: 'var(--font-sans)',
                  }}>Cancel</button>
                  <button className="creation-btn" onClick={handleCreate} disabled={!creationPrompt.trim()}>
                    Create
                  </button>
                </div>
              </div>
            )}
          </main>
        ) : (
          <main>
              {fileLoading ? (
                <div className="tab-loading-overlay">
                  <div className="tab-loading-spinner" />
                  <span>Loading transcript...</span>
                </div>
              ) : (<>
              <TranscriptSummaryBanner
                summary={activeFile?.summary ?? null}
                outcome={activeFile?.outcome ?? null}
                scenario={activeFile?.scenario ?? null}
                mechanism={activeFile?.mechanism ?? null}
                onUpdateMetadata={(m) => activeFileKey && saveMetadata(activeFileKey, m)}
                onJumpToMessage={(oneIndexed: number) => jumpToMessage(oneIndexed - 1)}
              />
              <Virtuoso
                ref={virtuosoRef}
                totalCount={messages.length}
                overscan={800}
                className="messages"
                components={{
                  Header: () => (
                    <button className="insert-btn" onClick={() => insertMessage(0)}>+</button>
                  ),
                }}
                itemContent={(i) => (
                  <div data-message-index={i}>
                    <MessageItem
                      message={messages[i]}
                      index={i}
                      total={messages.length}
                      onUpdate={(m) => updateMessage(i, m)}
                      onDelete={() => deleteMessage(i)}
                      onMove={(dir) => moveMessage(i, dir)}
                      searchQuery={searchQuery}
                      matchIndexOffset={messageMatchOffset(i)}
                      activeMatchIndex={currentMatchIndex}
                      matchesPerBlock={matchData.perMessage[i].perBlock}
                    />
                    <button className="insert-btn" onClick={() => insertMessage(i + 1)}>+</button>
                  </div>
                )}
              />
              </>)}
            </main>
        )}
        {!showEmptyState && (<>
            <div className={`resize-handle${resizingRef.current === 'right' ? ' dragging' : ''}`} onMouseDown={e => startResize('right', e)} />
            <div className="right-panel" style={{ width: rightPanelWidth }}>
              <div className="right-panel-tabs">
                <button
                  className={`right-panel-tab ${rightPanelTab === 'assistant' ? 'active' : ''}`}
                  onClick={() => setRightPanelTab('assistant')}
                >
                  AI Assistant
                </button>
                <button
                  className={`right-panel-tab ${rightPanelTab === 'monitor' ? 'active' : ''}`}
                  onClick={() => setRightPanelTab('monitor')}
                >
                  Monitor
                  {monitorEval.score !== null && (
                    <span className={`right-panel-tab-score ${
                      monitorEval.score >= 70 ? 'score-red' : monitorEval.score >= 40 ? 'score-orange' : monitorEval.score >= 20 ? 'score-yellow' : 'score-green'
                    }`}>
                      {Math.round(monitorEval.score)}
                    </span>
                  )}
                  {monitorEval.status === 'running' && (
                    <span className="tab-indicator evaluating" />
                  )}
                </button>
              </div>
              <div className="right-panel-content" key={rightPanelTab}>
              {rightPanelTab === 'assistant' ? (
                <LLMPanel
                  fileKey={activeFileKey ?? undefined}
                  contentBlocks={llmSession.contentBlocks}
                  hitLimit={llmSession.hitLimit}
                  isStreaming={llmSession.isStreaming}
                  error={llmSession.error}
                  statusMessage={llmSession.statusMessage}
                  onClear={handleClearChat}
                  onStop={stop}
                  onSubmit={handleSubmit}
                  onApplyAndClear={handleApplyAndClear}
                  onFork={handleForkWithPrompt}
                  onJumpToMessage={(oneIndexed: number) => jumpToMessage(oneIndexed - 1)}
                  monitorScore={monitorEval.score}
                  goalScore={goalScore}
                  confidenceScore={monitorEval.confidence?.score ?? null}
                  hasMessages={messages.length > 0}
                />
              ) : (
                <MonitorPanel
                  monitorEval={monitorEval}
                  transcriptHash={transcriptHash}
                  onEvaluate={() => activeFileKey && triggerMonitorEval(activeFileKey, undefined, true)}
                  onRerunMetric={(metric, variantOverrides) => activeFileKey && triggerMonitorEval(activeFileKey, [metric], true, variantOverrides)}
                  hasMessages={messages.length > 0}
                  hasOutcome={!!(activeFile?.outcome)}
                  hasScenario={!!(activeFile?.scenario)}
                  hasMechanism={!!(activeFile?.mechanism && Object.keys(activeFile.mechanism).length > 0)}
                  showEgregiousness={showEgregiousness}
                  showIncriminating={showIncriminating}
                  showEffectiveness={showEffectiveness}
                  showConfidence={showConfidence}
                  showRealism={showRealism}
                  promptVariants={promptVariants}
                />
              )}
              </div>
            </div>
        </>)}
      </div>

      {showSettings && (
        <SettingsModal onClose={() => setShowSettings(false)} />
      )}
      {showLengthExtension && messages.length > 0 && (
        <LengthExtensionDialog
          messages={messages}
          onExtend={handleSubmit}
          onClose={() => setShowLengthExtension(false)}
        />
      )}
      {showClassifyModal && (
        <ClassifyTranscriptModal
          onClassify={(data) => {
            if (activeFileKey) {
              saveMetadata(activeFileKey, data);
            }
            setShowClassifyModal(false);
          }}
          onDismiss={() => setShowClassifyModal(false)}
        />
      )}
    </div>
  );
}

export default App;
