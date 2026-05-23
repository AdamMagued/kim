import { useEffect, useState } from 'react';

export type ToastKind = 'info' | 'success' | 'error' | 'warning';

export interface ToastMessage {
  id: number;
  kind: ToastKind;
  text: string;
  duration?: number; // ms, default 4000
}

let _idCounter = 0;
let _setToasts: React.Dispatch<React.SetStateAction<ToastMessage[]>> | null = null;

export function toast(text: string, kind: ToastKind = 'info', duration = 4000) {
  if (!_setToasts) return;
  const id = ++_idCounter;
  _setToasts(prev => [...prev, { id, kind, text, duration }]);
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 8l3.5 3.5L13 4" />
    </svg>
  );
}
function InfoIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6" /><path d="M8 7v4M8 5.5v.5" />
    </svg>
  );
}
function WarnIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 2L1 14h14L8 2z" /><path d="M8 6v4M8 11.5v.5" />
    </svg>
  );
}
function XIcon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}

const ICONS: Record<ToastKind, React.ReactNode> = {
  success: <CheckIcon />,
  info:    <InfoIcon />,
  warning: <WarnIcon />,
  error:   <WarnIcon />,
};

function ToastItem({ t, onRemove }: { t: ToastMessage; onRemove: (id: number) => void }) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    // Enter animation
    const show = setTimeout(() => setVisible(true), 10);
    // Auto-dismiss
    const hide = setTimeout(() => {
      setVisible(false);
      setTimeout(() => onRemove(t.id), 250);
    }, t.duration ?? 4000);
    return () => { clearTimeout(show); clearTimeout(hide); };
  }, [t.id, t.duration, onRemove]);

  return (
    <div
      className={`kim-toast kim-toast--${t.kind}${visible ? ' kim-toast--visible' : ''}`}
      role="status"
    >
      <span className="kim-toast__icon">{ICONS[t.kind]}</span>
      <span className="kim-toast__text">{t.text}</span>
      <button
        className="kim-toast__close"
        onClick={() => { setVisible(false); setTimeout(() => onRemove(t.id), 250); }}
        aria-label="Dismiss"
      >
        <XIcon />
      </button>
    </div>
  );
}

export function ToastProvider() {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  useEffect(() => {
    _setToasts = setToasts;
    return () => { _setToasts = null; };
  }, []);

  function remove(id: number) {
    setToasts(prev => prev.filter(t => t.id !== id));
  }

  if (toasts.length === 0) return null;

  return (
    <div className="kim-toast-container" aria-live="polite">
      {toasts.map(t => (
        <ToastItem key={t.id} t={t} onRemove={remove} />
      ))}
    </div>
  );
}
