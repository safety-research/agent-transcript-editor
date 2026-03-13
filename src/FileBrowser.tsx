import { useState, useEffect, useRef } from 'react';
import { useStore, parseFileKey } from './store';
import { normalizeMessages } from './normalizeMessages';
import { showToast } from './toast';

interface TranscriptInfo {
  name: string;
  path: string;
  size: number;
  modified: string;
  message_count: number | null;
}

const API_BASE = '/api';

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}K`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`;
}

export function FileBrowser() {
  const [files, setFiles] = useState<TranscriptInfo[]>([]);
  const [loading, setLoading] = useState(true);

  const openFiles = useStore(s => s.openFiles);
  const openFile = useStore(s => s.openFile);
  const activeProjectDirName = useStore(s => {
    const p = s.projects.find(pr => pr.id === s.activeProjectId);
    return p?.dirName ?? 'default';
  });

  // Track which files are open
  const openFileNames = new Set(
    openFiles.map(k => parseFileKey(k).fileName)
  );

  const fetchFilesRef = useRef(async () => {
    try {
      const response = await fetch(`${API_BASE}/files/list`);
      if (response.ok) {
        const data = await response.json();
        setFiles(data);
      }
    } catch {
      // Backend not available
    }
    setLoading(false);
  });

  const fetchFiles = fetchFilesRef.current;

  useEffect(() => {
    let cancelled = false;
    const doFetch = async () => {
      try {
        const response = await fetch(`${API_BASE}/files/list`);
        if (response.ok && !cancelled) {
          const data = await response.json();
          setFiles(data);
        }
      } catch {
        // Backend not available
      }
      if (!cancelled) setLoading(false);
    };

    doFetch();
    const interval = setInterval(doFetch, 10000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  const handleOpen = async (file: TranscriptInfo) => {
    try {
      const response = await fetch(`${API_BASE}/files/load/${encodeURIComponent(file.path)}`);
      if (!response.ok) return;
      const data = await response.json();
      openFile(activeProjectDirName, file.name, normalizeMessages(data.messages));
      if (data.auto_fixed) {
        const parts: string[] = [];
        if (data.auto_fixed.minimized) parts.push('minimized');
        if (data.auto_fixed.tool_ids_fixed) parts.push(`${data.auto_fixed.tool_ids_fixed} tool IDs fixed`);
        showToast(`Auto-fixed ${file.name}: ${parts.join(', ')}`, 'info');
      }
    } catch {
      // Ignore
    }
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
        const response = await fetch(`${API_BASE}/files/upload`, {
          method: 'POST',
          body: formData,
        });
        if (response.ok) {
          const data = await response.json();
          fetchFiles();
          if (data.auto_fixed) {
            const parts: string[] = [];
            if (data.auto_fixed.tool_ids_fixed) parts.push(`${data.auto_fixed.tool_ids_fixed} tool IDs fixed`);
            showToast(`Auto-fixed ${file.name}: ${parts.join(', ')}`, 'info');
          }
        }
      } catch {
        // Ignore
      }
    };
    input.click();
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
    <div className="file-browser">
      <div className="file-browser-header">
        <h4>Files</h4>
        <div style={{ display: 'flex', gap: '4px' }}>
          <button
            className="file-browser-upload"
            onClick={handleSave}
            title="Save current file"
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
        ) : files.length === 0 ? (
          <div className="file-browser-empty">No transcripts yet</div>
        ) : (
          files.map(file => (
            <div
              key={file.path}
              className={`file-browser-item ${openFileNames.has(file.name) ? 'open' : ''}`}
              onClick={() => handleOpen(file)}
              title={`${file.name} (${file.message_count ?? '?'} messages, ${formatSize(file.size)})`}
            >
              <span className="file-icon">{openFileNames.has(file.name) ? '●' : '○'}</span>
              <span className="file-name">{file.name}</span>
              <span className="file-meta">{file.message_count ?? '?'}m</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
