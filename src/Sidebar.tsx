import { useState, useEffect, useRef, useCallback } from 'react';
import { useStore, monitorEvalFromSidecar, makeFileKey } from './store';
import type { FileKey } from './store';
import { normalizeMessages } from './normalizeMessages';
import { triggerMonitorEval } from './monitor';

interface TranscriptInfo {
  name: string;
  path: string;
  size: number;
  modified: string;
  message_count: number | null;
  scores: Record<string, number> | null;
  has_metadata: boolean;
  agent_running: boolean;
  eval_status: string | null;  // "running", "done", "error"
}

interface ProjectDirInfo {
  name: string;
  dir_name: string;
  file_count: number;
  modified: string | null;
}

const API_BASE = '/api';

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}K`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`;
}

// ── Project Section ────────────────────────────────────────────────────

function SidebarProjectSection() {
  const projects = useStore(s => s.projects);
  const activeProjectId = useStore(s => s.activeProjectId);
  const switchProject = useStore(s => s.switchProject);
  const storeCreateProject = useStore(s => s.createProject);
  const renameProject = useStore(s => s.renameProject);
  const deleteProject = useStore(s => s.deleteProject);
  const ensureProject = useStore(s => s.ensureProject);

  const [backendProjects, setBackendProjects] = useState<ProjectDirInfo[]>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [creating, setCreating] = useState(false);
  const [createValue, setCreateValue] = useState('');
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const createInputRef = useRef<HTMLInputElement>(null);

  // Fetch backend project directories (source of truth for project list)
  useEffect(() => {
    const fetchProjects = async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const res = await fetch(`${API_BASE}/files/projects`, { signal: controller.signal });
        if (res.ok) {
          const projects: ProjectDirInfo[] = await res.json();
          setBackendProjects(projects);
          // Sync backend projects into the store (ensures store has all projects even after IndexedDB clear)
          for (const bp of projects) {
            ensureProject(bp.name, bp.dir_name);
          }
        }
      } catch { /* backend unavailable or timeout */ }
      finally { clearTimeout(timeout); }
    };
    fetchProjects();
    const interval = setInterval(fetchProjects, 10000);
    return () => clearInterval(interval);
  }, [ensureProject]);

  useEffect(() => {
    if (editingId && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editingId]);

  useEffect(() => {
    if (creating && createInputRef.current) {
      createInputRef.current.focus();
    }
  }, [creating]);

  const commitRename = async (projectId: string, oldDirName: string) => {
    const trimmed = editValue.trim();
    if (!trimmed) {
      setEditingId(null);
      return;
    }

    // Rename on backend
    try {
      const res = await fetch(`${API_BASE}/files/projects/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_name: oldDirName, new_name: trimmed }),
      });
      if (res.ok) {
        const data = await res.json();
        renameProject(projectId, trimmed, data.dir_name);
      }
    } catch { /* ignore */ }

    setEditingId(null);
  };

  const handleCreateProject = () => {
    setCreating(true);
    setCreateValue('');
  };

  const commitCreate = async () => {
    const trimmed = createValue.trim();
    setCreating(false);
    if (!trimmed) return;

    try {
      const res = await fetch(`${API_BASE}/files/projects`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: trimmed }),
      });
      if (res.ok) {
        const data = await res.json();
        storeCreateProject(trimmed, data.dir_name);
      }
    } catch { /* ignore */ }
  };

  const handleDeleteProject = (projectId: string) => {
    if (projects.length <= 1) return;
    setConfirmingDeleteId(projectId);
  };

  const confirmDelete = async (projectId: string, dirName: string) => {
    setConfirmingDeleteId(null);
    try {
      await fetch(`${API_BASE}/files/projects/${encodeURIComponent(dirName)}?delete_files=true`, {
        method: 'DELETE',
      });
    } catch { /* ignore */ }
    deleteProject(projectId);
  };

  // Derive active dirName from store
  const activeProject = projects.find(p => p.id === activeProjectId);
  const activeDirName = activeProject?.dirName ?? null;

  // Resolve store project ID for a backend project (lazy-create if needed)
  const getStoreId = (bp: ProjectDirInfo) => {
    return projects.find(p => p.dirName === bp.dir_name)?.id ?? null;
  };

  const handleProjectClick = (bp: ProjectDirInfo) => {
    const id = ensureProject(bp.name, bp.dir_name);
    switchProject(id);
  };

  const handleProjectDoubleClick = (bp: ProjectDirInfo) => {
    const id = ensureProject(bp.name, bp.dir_name);
    setEditingId(id);
    setEditValue(bp.name);
  };

  return (
    <div className="sidebar-project-section">
      <div className="sidebar-header">
        <h4>Projects</h4>
        <button
          className="sidebar-action-btn"
          onClick={handleCreateProject}
          title="New project"
        >
          +
        </button>
      </div>
      <div className="project-list">
        {[...backendProjects].sort((a, b) => {
          // Sort by most recently modified (newest first), fall back to name
          if (a.modified && b.modified) return b.modified.localeCompare(a.modified);
          if (a.modified) return -1;
          if (b.modified) return 1;
          return a.name.localeCompare(b.name);
        }).map(bp => {
          const storeId = getStoreId(bp);
          return (
          <div
            key={bp.dir_name}
            className={`project-item ${bp.dir_name === activeDirName ? 'active' : ''}`}
            onClick={() => handleProjectClick(bp)}
            onDoubleClick={() => handleProjectDoubleClick(bp)}
          >
            {storeId && editingId === storeId ? (
              <input
                ref={inputRef}
                className="project-rename-input"
                value={editValue}
                onChange={e => setEditValue(e.target.value)}
                onBlur={() => commitRename(storeId, bp.dir_name)}
                onKeyDown={e => {
                  if (e.key === 'Enter') inputRef.current?.blur();
                  if (e.key === 'Escape') { setEditingId(null); e.target instanceof HTMLInputElement && e.target.blur(); }
                }}
                onClick={e => e.stopPropagation()}
              />
            ) : storeId && confirmingDeleteId === storeId ? (
              <div className="project-confirm-delete" onClick={e => e.stopPropagation()}>
                <span className="project-confirm-label">Delete?</span>
                <button
                  className="project-confirm-yes"
                  onClick={() => confirmDelete(storeId, bp.dir_name)}
                >
                  Yes
                </button>
                <button
                  className="project-confirm-no"
                  onClick={() => setConfirmingDeleteId(null)}
                >
                  No
                </button>
              </div>
            ) : (
              <>
                <span className="project-name">{bp.name}</span>
                <span className="project-count">{bp.file_count}</span>
                {backendProjects.length > 1 && bp.dir_name !== 'default' && (
                  <button
                    className="project-delete-btn"
                    onClick={e => {
                      e.stopPropagation();
                      if (storeId) handleDeleteProject(storeId);
                    }}
                    title="Delete project"
                  >
                    ×
                  </button>
                )}
              </>
            )}
          </div>
          );
        })}
        {creating && (
          <div className="project-item project-creating">
            <input
              ref={createInputRef}
              className="project-rename-input"
              value={createValue}
              placeholder="Project name"
              onChange={e => setCreateValue(e.target.value)}
              onBlur={commitCreate}
              onKeyDown={e => {
                if (e.key === 'Enter') commitCreate();
                if (e.key === 'Escape') setCreating(false);
              }}
            />
          </div>
        )}
      </div>
    </div>
  );
}

// ── File Section ───────────────────────────────────────────────────────

function SidebarFileSection() {
  // Cache file lists per project to avoid flicker on project switch
  const filesCacheRef = useRef<Map<string, TranscriptInfo[]>>(new Map());
  const [diskFiles, setDiskFiles] = useState<TranscriptInfo[]>([]);
  const [loading, setLoading] = useState(true);

  const activeProject = useStore(s => s.projects.find(p => p.id === s.activeProjectId));
  const projects = useStore(s => s.projects);
  const files = useStore(s => s.files);
  const openFiles = useStore(s => s.openFiles);
  const openFile = useStore(s => s.openFile);
  const switchFile = useStore(s => s.switchFile);
  const closeFile = useStore(s => s.closeFile);
  const moveFile = useStore(s => s.moveFile);
  const sidebarRefreshTrigger = useStore(s => s.sidebarRefreshTrigger);

  const dirName = activeProject?.dirName ?? 'default';

  // Sync from cache immediately on dirName change (no effect delay = no flicker)
  const prevDirRef = useRef(dirName);
  if (prevDirRef.current !== dirName) {
    prevDirRef.current = dirName;
    const cached = filesCacheRef.current.get(dirName);
    setDiskFiles(cached ?? []);
    setLoading(!cached);
  }

  const [renamingFile, setRenamingFile] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [confirmingFileDelete, setConfirmingFileDelete] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; file: TranscriptInfo } | null>(null);
  const renameInputRef = useRef<HTMLInputElement>(null);

  // Multi-select state
  const [selectedFiles, setSelectedFiles] = useState<Set<string>>(new Set());
  const lastClickedFileRef = useRef<string | null>(null);
  const [batchDeleteFiles, setBatchDeleteFiles] = useState<TranscriptInfo[] | null>(null);

  // Clear selection on project change
  useEffect(() => {
    setSelectedFiles(new Set());
    lastClickedFileRef.current = null;
  }, [dirName]);

  useEffect(() => {
    if (renamingFile && renameInputRef.current) {
      renameInputRef.current.focus();
      // Select name without extension
      const dotIndex = renameValue.lastIndexOf('.');
      renameInputRef.current.setSelectionRange(0, dotIndex > 0 ? dotIndex : renameValue.length);
    }
  }, [renamingFile]);

  // Set of fileNames currently open in this project
  const openFileNames = new Set<string>();
  const prefix = dirName + '/';
  for (const key of openFiles) {
    if (key.startsWith(prefix)) {
      openFileNames.add(key.slice(prefix.length));
    }
  }

  // Map fileName -> fileKey for the active project
  const fileNameToKey = new Map<string, FileKey>();
  for (const key of openFiles) {
    if (key.startsWith(prefix)) {
      fileNameToKey.set(key.slice(prefix.length), key);
    }
  }

  const fetchFiles = useCallback(async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(`${API_BASE}/files/list?project=${encodeURIComponent(dirName)}`, { signal: controller.signal });
      if (response.ok) {
        const data: TranscriptInfo[] = await response.json();
        filesCacheRef.current.set(dirName, data);
        setDiskFiles(data);
        setLoading(false);
      }
    } catch { /* backend unavailable or timeout */ }
    finally { clearTimeout(timeout); }
  }, [dirName]);

  useEffect(() => {
    let cancelled = false;

    const doFetch = async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(`${API_BASE}/files/list?project=${encodeURIComponent(dirName)}`, { signal: controller.signal });
        if (response.ok && !cancelled) {
          const data: TranscriptInfo[] = await response.json();
          filesCacheRef.current.set(dirName, data);
          setDiskFiles(data);
          setLoading(false);
        }
      } catch { /* backend unavailable or timeout */ }
      finally { clearTimeout(timeout); }
    };

    doFetch();
    const interval = setInterval(doFetch, 10000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [dirName, sidebarRefreshTrigger]);

  const handleOpen = (file: TranscriptInfo) => {
    // If file is already open in this project, just switch to it
    const existingKey = fileNameToKey.get(file.name);
    if (existingKey) {
      switchFile(existingKey);
      return;
    }

    // Open with empty messages — the SSE session_state will fill them in
    // (session is the single source of truth for messages + metadata)
    openFile(dirName, file.name, []);
  };

  /** Load a file if not already open, return its fileKey */
  const ensureFileLoaded = async (file: TranscriptInfo): Promise<FileKey | null> => {
    const existingKey = fileNameToKey.get(file.name);
    if (existingKey) return existingKey;

    try {
      const response = await fetch(`${API_BASE}/files/load/${encodeURIComponent(file.path)}`);
      if (!response.ok) return null;
      const data = await response.json();
      const restored = monitorEvalFromSidecar(data.metadata);
      return openFile(dirName, file.name, normalizeMessages(data.messages), restored, data.metadata);
    } catch {
      return null;
    }
  };

  const handleFileClick = (e: React.MouseEvent, file: TranscriptInfo) => {
    const sortedFiles = [...diskFiles].sort((a, b) => a.name.localeCompare(b.name));

    if (e.metaKey || e.ctrlKey) {
      // Cmd/Ctrl+click: toggle in selection, don't open
      setSelectedFiles(prev => {
        const next = new Set(prev);
        if (next.has(file.path)) {
          next.delete(file.path);
        } else {
          next.add(file.path);
        }
        return next;
      });
      lastClickedFileRef.current = file.path;
    } else if (e.shiftKey && lastClickedFileRef.current) {
      // Shift+click: range-select from anchor to current
      const anchorIdx = sortedFiles.findIndex(f => f.path === lastClickedFileRef.current);
      const currentIdx = sortedFiles.findIndex(f => f.path === file.path);
      if (anchorIdx !== -1 && currentIdx !== -1) {
        const [start, end] = anchorIdx < currentIdx ? [anchorIdx, currentIdx] : [currentIdx, anchorIdx];
        const range = sortedFiles.slice(start, end + 1).map(f => f.path);
        setSelectedFiles(prev => {
          const next = new Set(prev);
          for (const p of range) next.add(p);
          return next;
        });
      }
    } else {
      // Plain click: clear selection, open file
      setSelectedFiles(new Set());
      lastClickedFileRef.current = file.path;
      handleOpen(file);
    }
  };

  const handleBatchReEvaluate = async (filesToEval: TranscriptInfo[]) => {
    setContextMenu(null);
    for (const file of filesToEval) {
      const fileKey = await ensureFileLoaded(file);
      if (fileKey) {
        triggerMonitorEval(fileKey, undefined, true);
      }
    }
  };

  const handleBatchDelete = (filesToDelete: TranscriptInfo[]) => {
    setContextMenu(null);
    setBatchDeleteFiles(filesToDelete);
  };

  const confirmBatchDelete = async () => {
    if (!batchDeleteFiles) return;
    for (const file of batchDeleteFiles) {
      const existingKey = fileNameToKey.get(file.name);
      if (existingKey) closeFile(existingKey);
      try {
        await fetch(`${API_BASE}/files/delete/${encodeURIComponent(file.path)}`, { method: 'DELETE' });
      } catch { /* ignore */ }
    }
    setBatchDeleteFiles(null);
    setSelectedFiles(new Set());
    fetchFiles();
  };

  const handleBatchMove = async (filesToMove: TranscriptInfo[], targetProject: { id: string; dirName: string }) => {
    setContextMenu(null);
    for (const file of filesToMove) {
      try {
        const res = await fetch(`${API_BASE}/files/move`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            source_project: dirName,
            target_project: targetProject.dirName,
            file_name: file.name,
          }),
        });
        if (!res.ok) continue;
        const existingKey = fileNameToKey.get(file.name);
        if (existingKey) moveFile(existingKey, targetProject.dirName);
      } catch { /* ignore */ }
    }
    setSelectedFiles(new Set());
    fetchFiles();
  };

  const handleRenameFile = async (file: TranscriptInfo) => {
    const trimmed = renameValue.trim();
    setRenamingFile(null);
    if (!trimmed || trimmed === file.name) return;

    const newName = trimmed.endsWith('.jsonl') ? trimmed : `${trimmed}.jsonl`;
    const newLabel = newName.replace(/\.jsonl$/, '');

    // Update file state BEFORE the API call so auto-save uses the new fileName
    const existingKey = fileNameToKey.get(file.name);
    if (existingKey) {
      useStore.getState().renameFile(existingKey, newLabel);
    }

    try {
      const res = await fetch(`${API_BASE}/files/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: dirName, old_name: file.name, new_name: newName }),
      });
      if (!res.ok) {
        // Rollback file state on failure
        if (existingKey) {
          const newKey = makeFileKey(dirName, newName);
          useStore.getState().renameFile(newKey, file.name.replace(/\.jsonl$/, ''));
        }
        return;
      }

      fetchFiles();
    } catch {
      // Rollback file state on error
      if (existingKey) {
        const newKey = makeFileKey(dirName, newName);
        useStore.getState().renameFile(newKey, file.name.replace(/\.jsonl$/, ''));
      }
    }
  };

  const handleDeleteFile = (file: TranscriptInfo) => {
    setConfirmingFileDelete(file.path);
  };

  const confirmFileDelete = async (file: TranscriptInfo) => {
    setConfirmingFileDelete(null);
    // Close the file first to prevent auto-save from re-creating the file
    const existingKey = fileNameToKey.get(file.name);
    if (existingKey) closeFile(existingKey);
    try {
      await fetch(`${API_BASE}/files/delete/${encodeURIComponent(file.path)}`, { method: 'DELETE' });
      fetchFiles();
    } catch { /* ignore */ }
  };

  const handleUpload = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.jsonl';
    input.onchange = async (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;

      const formData = new FormData();
      formData.append('file', file);

      try {
        const response = await fetch(
          `${API_BASE}/files/upload?project=${encodeURIComponent(dirName)}`,
          { method: 'POST', body: formData }
        );
        if (response.ok) fetchFiles();
      } catch { /* ignore */ }
    };
    input.click();
  };

  // Close context menu on click outside
  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(null);
    window.addEventListener('click', close);
    return () => window.removeEventListener('click', close);
  }, [contextMenu]);

  const handleDuplicateFile = async (file: TranscriptInfo) => {
    setContextMenu(null);
    try {
      const res = await fetch(`${API_BASE}/files/duplicate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: dirName, file_name: file.name }),
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => res.statusText);
        console.error('[Sidebar] Duplicate failed:', res.status, detail);
        return;
      }
      const data = await res.json();
      console.info('[Sidebar] Duplicated', file.name, '→', data.new_name);
      await fetchFiles();
    } catch (err) {
      console.error('[Sidebar] Duplicate error:', err);
    }
  };

  const handleSave = async () => {
    const store = useStore.getState();
    const activeKey = store.activeFileKey;
    if (!activeKey) return;
    const file = store.files[activeKey];
    if (!file || file.messages.length === 0) return;
    const { saveMessages } = await import('./api');
    await saveMessages(activeKey, file.messages);
    fetchFiles();
  };

  return (
    <div className="sidebar-file-section">
      <div className="sidebar-header">
        <h4>Files</h4>
        <div style={{ display: 'flex', gap: '4px' }}>
          <button
            className="file-browser-upload"
            onClick={handleSave}
            title="Save current tab"
          >
            Save
          </button>
          <button
            className="file-browser-upload"
            onClick={handleUpload}
            title="Upload transcript"
          >
            Upload
          </button>
        </div>
      </div>
      <div className="file-browser-list">
        {loading ? (
          <div className="file-browser-empty">Loading...</div>
        ) : diskFiles.length === 0 ? (
          <div className="file-browser-empty">No transcripts yet</div>
        ) : (
          [...diskFiles].sort((a, b) => a.name.localeCompare(b.name)).map(file => (
            <div
              key={file.path}
              className={`file-browser-item ${openFileNames.has(file.name) ? 'open' : ''} ${selectedFiles.has(file.path) ? 'selected' : ''}`}
              onClick={(e) => {
                if (renamingFile !== file.path) handleFileClick(e, file);
              }}
              onDoubleClick={(e) => {
                e.stopPropagation();
                setRenamingFile(file.path);
                setRenameValue(file.name);
              }}
              onContextMenu={(e) => {
                e.preventDefault();
                // Right-click on non-selected file → select just that file
                if (!selectedFiles.has(file.path)) {
                  setSelectedFiles(new Set([file.path]));
                }
                setContextMenu({ x: e.clientX, y: e.clientY, file });
              }}
              title={`${file.name} (${file.message_count ?? '?'} messages, ${formatSize(file.size)})\nDouble-click to rename · Right-click for more options`}
            >
              {(() => {
                const fileKey = fileNameToKey.get(file.name);
                const fileState = fileKey ? files[fileKey] : null;
                // Show streaming/evaluating indicators when active
                const isStreaming = fileState?.llmSession?.isStreaming || file.agent_running;
                const isEvaluating = fileState?.monitorEval?.status === 'running' || file.eval_status === 'running';
                if (isStreaming || isEvaluating) {
                  return (
                    <span className="file-indicators">
                      {isStreaming && <span className="tab-indicator streaming" title="Agent running" />}
                      {isEvaluating && <span className="tab-indicator evaluating" title="Eval running" />}
                    </span>
                  );
                }
                // Try file's live eval first, then fall back to sidecar scores from API
                let score: number | null = null;
                if (fileState?.monitorEval?.status === 'done' && fileState.monitorEval.score !== null && (fileState.monitorEval.transcriptHash == null || fileState.monitorEval.transcriptHash === fileState.transcriptHash)) {
                  score = fileState.monitorEval.score;
                } else if (file.scores?.suspiciousness != null) {
                  score = file.scores.suspiciousness;
                }
                if (score !== null) {
                  const colorClass = score >= 70 ? 'file-score-red' : score >= 40 ? 'file-score-orange' : score >= 20 ? 'file-score-yellow' : 'file-score-green';
                  return <span className={`file-score ${colorClass}`}>{Math.round(score)}</span>;
                }
                return <span className="file-icon">{openFileNames.has(file.name) ? '●' : '○'}</span>;
              })()}
              {renamingFile === file.path ? (
                <input
                  ref={renameInputRef}
                  className="project-rename-input"
                  value={renameValue}
                  onChange={e => setRenameValue(e.target.value)}
                  onBlur={() => handleRenameFile(file)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') renameInputRef.current?.blur();
                    if (e.key === 'Escape') { setRenamingFile(null); e.target instanceof HTMLInputElement && e.target.blur(); }
                  }}
                  onClick={e => e.stopPropagation()}
                />
              ) : confirmingFileDelete === file.path ? (
                <div className="project-confirm-delete" onClick={e => e.stopPropagation()}>
                  <span className="project-confirm-label">Delete?</span>
                  <button
                    className="project-confirm-yes"
                    onClick={() => confirmFileDelete(file)}
                  >
                    Yes
                  </button>
                  <button
                    className="project-confirm-no"
                    onClick={() => setConfirmingFileDelete(null)}
                  >
                    No
                  </button>
                </div>
              ) : (
                <>
                  <span className="file-name">{file.name.replace(/\.jsonl$/, '')}</span>
                  <span className="file-meta">{file.message_count ?? '?'}m</span>
                  <div className="file-actions">
                    <button
                      className="file-delete-btn"
                      onClick={e => {
                        e.stopPropagation();
                        handleDeleteFile(file);
                      }}
                      title="Delete file"
                    >
                      ×
                    </button>
                  </div>
                </>
              )}
            </div>
          ))
        )}
      </div>

      {/* Batch delete confirmation */}
      {batchDeleteFiles && (
        <div className="file-batch-delete-confirm">
          <span>Delete {batchDeleteFiles.length} file{batchDeleteFiles.length !== 1 ? 's' : ''}?</span>
          <button className="project-confirm-yes" onClick={confirmBatchDelete}>Yes</button>
          <button className="project-confirm-no" onClick={() => setBatchDeleteFiles(null)}>No</button>
        </div>
      )}

      {/* Context menu for file actions */}
      {contextMenu && (() => {
        const selectedCount = selectedFiles.size;
        const isMulti = selectedCount > 1;
        const targetFiles = isMulti
          ? diskFiles.filter(f => selectedFiles.has(f.path))
          : [contextMenu.file];
        const countLabel = isMulti ? ` (${selectedCount})` : '';

        return (
          <div
            className="file-context-menu"
            style={{ position: 'fixed', left: contextMenu.x, top: contextMenu.y, zIndex: 9999 }}
            onClick={e => e.stopPropagation()}
          >
            <button
              className="file-context-menu-item"
              onClick={() => handleBatchReEvaluate(targetFiles)}
            >
              Re-evaluate{countLabel}
            </button>
            {projects.length > 1 && (
              <>
                <div className="file-context-menu-header">Move to...{countLabel}</div>
                {projects
                  .filter(p => p.dirName !== dirName)
                  .map(p => (
                    <button
                      key={p.id}
                      className="file-context-menu-item"
                      onClick={() => handleBatchMove(targetFiles, p)}
                    >
                      {p.label}
                    </button>
                  ))
                }
              </>
            )}
            {!isMulti && (
              <>
                <button
                  className="file-context-menu-item"
                  onClick={() => handleDuplicateFile(contextMenu.file)}
                >
                  Duplicate
                </button>
                <button
                  className="file-context-menu-item"
                  onClick={() => {
                    setContextMenu(null);
                    setRenamingFile(contextMenu.file.path);
                    setRenameValue(contextMenu.file.name);
                  }}
                >
                  Rename
                </button>
              </>
            )}
            <button
              className="file-context-menu-item file-context-menu-danger"
              onClick={() => {
                if (isMulti) {
                  handleBatchDelete(targetFiles);
                } else {
                  setContextMenu(null);
                  setConfirmingFileDelete(contextMenu.file.path);
                }
              }}
            >
              Delete{countLabel}
            </button>
          </div>
        );
      })()}
    </div>
  );
}

// ── Sidebar ────────────────────────────────────────────────────────────

export function Sidebar({ style }: { style?: React.CSSProperties }) {
  return (
    <div className="sidebar" style={style}>
      <SidebarProjectSection />
      <div className="sidebar-divider" />
      <SidebarFileSection />
    </div>
  );
}
