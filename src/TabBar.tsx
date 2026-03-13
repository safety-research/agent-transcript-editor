import { useState, useRef, useEffect } from 'react';
import { useStore, parseFileKey } from './store';
import type { FileState, FileKey } from './store';

import { API_BASE } from './constants';

function ScoreBadge({ score }: { score: number }) {
  let colorClass = 'score-green';
  if (score >= 70) colorClass = 'score-red';
  else if (score >= 40) colorClass = 'score-orange';
  else if (score >= 20) colorClass = 'score-yellow';

  return (
    <span className={`tab-score ${colorClass}`} title={`Monitor score: ${score}`}>
      {Math.round(score)}
    </span>
  );
}

function TabItem({ file, isActive, onSwitch, onClose, onRename }: {
  file: FileState;
  isActive: boolean;
  onSwitch: () => void;
  onClose: () => void;
  onRename: (label: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(file.label);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const commitRename = () => {
    const trimmed = editValue.trim();
    if (trimmed && trimmed !== file.label) {
      onRename(trimmed);
    }
    setEditing(false);
  };

  return (
    <div
      className={`tab-item ${isActive ? 'active' : ''}`}
      onClick={onSwitch}
      onDoubleClick={() => { setEditing(true); setEditValue(file.label); }}
      title={file.fileName}
    >
      {/* Status indicators */}
      <div className="tab-indicators">
        {file.llmSession.isStreaming && (
          <span className="tab-indicator streaming" title="Agent running" />
        )}
        {file.monitorEval.status === 'running' && (
          <span className="tab-indicator evaluating" title="Eval running" />
        )}
      </div>

      {/* Label */}
      {editing ? (
        <input
          ref={inputRef}
          className="tab-rename-input"
          value={editValue}
          onChange={e => setEditValue(e.target.value)}
          onBlur={commitRename}
          onKeyDown={e => {
            if (e.key === 'Enter') inputRef.current?.blur();
            if (e.key === 'Escape') { setEditing(false); inputRef.current?.blur(); }
          }}
          onClick={e => e.stopPropagation()}
        />
      ) : (
        <span className="tab-label">{file.label}</span>
      )}

      {/* Monitor score */}
      {file.monitorEval.status === 'done' && file.monitorEval.score !== null && (
        <ScoreBadge score={file.monitorEval.score} />
      )}

      {/* Close button */}
      <button
        className="tab-close"
        onClick={e => { e.stopPropagation(); onClose(); }}
        title="Close tab"
      >
        ×
      </button>
    </div>
  );
}

export function TabBar() {
  const activeProject = useStore(s => s.projects.find(p => p.id === s.activeProjectId));
  const files = useStore(s => s.files);
  const openFiles = useStore(s => s.openFiles);
  const activeFileKey = useStore(s => s.activeFileKey);

  const switchFile = useStore(s => s.switchFile);
  const closeFile = useStore(s => s.closeFile);
  const renameFile = useStore(s => s.renameFile);
  const openNewFile = useStore(s => s.openNewFile);

  const handleRename = async (fileKey: FileKey, newLabel: string) => {
    const file = files[fileKey];
    if (!file || !activeProject) return;

    const oldFileName = file.fileName;
    const newFileName = `${newLabel}.jsonl`;
    const { projectDirName } = parseFileKey(fileKey);

    // Update store immediately (label + fileName + re-key)
    renameFile(fileKey, newLabel);

    // Rename on disk
    try {
      const res = await fetch(`${API_BASE}/files/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: projectDirName,
          old_name: oldFileName,
          new_name: newFileName,
        }),
      });
      if (!res.ok) {
        // Rollback — re-key back to old name
        const newFileKey = `${projectDirName}/${newFileName}`;
        renameFile(newFileKey, oldFileName.replace(/\.jsonl$/, ''));
      }
    } catch {
      // Rollback on network error
      const newFileKey = `${projectDirName}/${newFileName}`;
      renameFile(newFileKey, oldFileName.replace(/\.jsonl$/, ''));
    }
  };

  if (!activeProject) return null;

  // Filter openFiles to show only files from the active project
  const prefix = activeProject.dirName + '/';
  const projectOpenFiles = openFiles.filter(k => k.startsWith(prefix));

  return (
    <div className="tab-bar">
      <div className="tab-bar-inner">
        <div className="tab-group-tabs">
          {projectOpenFiles.map(fileKey => {
            const file = files[fileKey];
            if (!file) return null;
            return (
              <TabItem
                key={fileKey}
                file={file}
                isActive={fileKey === activeFileKey}
                onSwitch={() => switchFile(fileKey)}
                onClose={() => closeFile(fileKey)}
                onRename={(label) => handleRename(fileKey, label)}
              />
            );
          })}
          <button
            className="tab-add"
            onClick={() => openNewFile(activeProject.dirName)}
            title="New tab"
          >
            +
          </button>
        </div>
      </div>
    </div>
  );
}
