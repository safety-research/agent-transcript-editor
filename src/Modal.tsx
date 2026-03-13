import type { ReactNode } from 'react';

interface ModalProps {
  title: string;
  onClose: () => void;
  className?: string;
  children: ReactNode;
}

/** Shared modal shell: overlay + header with close button. Children should include modal-body and optional modal-footer. */
export default function Modal({ title, onClose, className, children }: ModalProps) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className={`modal${className ? ` ${className}` : ''}`} onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>{title}</h3>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        {children}
      </div>
    </div>
  );
}
