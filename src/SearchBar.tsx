import { useState, useEffect, useRef, useCallback } from 'react';

interface Props {
  currentMatch: number;
  totalMatches: number;
  onQueryChange: (query: string) => void;
  onNext: () => void;
  onPrev: () => void;
  onClose: () => void;
  focusTrigger: number;
}

export function SearchBar({ currentMatch, totalMatches, onQueryChange, onNext, onPrev, onClose, focusTrigger }: Props) {
  const [inputValue, setInputValue] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  // Focus input when triggered by Ctrl+F
  useEffect(() => {
    if (focusTrigger) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [focusTrigger]);

  const handleChange = useCallback((value: string) => {
    setInputValue(value);
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
    }
    debounceRef.current = setTimeout(() => {
      onQueryChange(value);
    }, 150);
  }, [onQueryChange]);

  // Cleanup debounce on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, []);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (e.shiftKey) {
        onPrev();
      } else {
        onNext();
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  };

  return (
    <div className="search-bar">
      <input
        ref={inputRef}
        type="text"
        className="search-input"
        placeholder="Search transcript..."
        value={inputValue}
        onChange={(e) => handleChange(e.target.value)}
        onKeyDown={handleKeyDown}
        spellCheck={false}
      />
      <span className="search-count">
        {inputValue && totalMatches > 0
          ? `${currentMatch + 1} of ${totalMatches}`
          : inputValue
            ? 'No matches'
            : ''}
      </span>
      <button
        className="search-nav-btn"
        onClick={onPrev}
        disabled={totalMatches === 0}
        title="Previous match (Shift+Enter)"
      >
        ▲
      </button>
      <button
        className="search-nav-btn"
        onClick={onNext}
        disabled={totalMatches === 0}
        title="Next match (Enter)"
      >
        ▼
      </button>
      <button
        className="search-close-btn"
        onClick={onClose}
        title="Close search (Escape)"
      >
        ×
      </button>
    </div>
  );
}
