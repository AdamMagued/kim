/**
 * PairingModal — desktop side of the phone-relay handshake.
 *
 * Flow:
 *   1. On open we call `relay_pair_init` (Rust). The relay returns a short
 *      `pair_code` valid for ~5 minutes.
 *   2. We render the payload `{url, code}` as a QR code that the phone scans.
 *      The phone's RelayPairingModal accepts either JSON or `kim://pair?u=&c=`,
 *      so we offer both: QR + a copyable JSON blob.
 *   3. We poll `relay_pair_status` every 2s until `claimed=true` or `expired`.
 *      When claimed we show a green "Paired with <device_name>" state and
 *      auto-close after a beat.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { QRCodeSVG } from 'qrcode.react';

interface PairingModalProps {
  open: boolean;
  onClose: () => void;
  /** Optional project_root override — falls through to Rust default. */
  projectRoot?: string;
}

interface PairInit {
  pair_code: string;
  expires_at: string;
  url: string;
}

interface PairStatus {
  claimed: boolean;
  expired: boolean;
  device_name?: string;
  claimed_at?: string;
}

type Phase =
  | { kind: 'loading' }
  | { kind: 'ready'; init: PairInit }
  | { kind: 'claimed'; init: PairInit; deviceName: string }
  | { kind: 'expired' }
  | { kind: 'error'; message: string };

const POLL_INTERVAL_MS = 2000;

export function PairingModal({ open, onClose, projectRoot }: PairingModalProps) {
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' });
  const pollRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const dialogRef = useRef<HTMLDivElement>(null);
  const prevFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => () => { mountedRef.current = false; }, []);

  // Accessibility: save/restore focus and handle Escape to dismiss.
  useEffect(() => {
    if (!open) return;

    prevFocusRef.current = document.activeElement as HTMLElement | null;

    // Defer so the element is in the DOM before we focus it.
    const rafId = requestAnimationFrame(() => {
      dialogRef.current?.focus();
    });

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      cancelAnimationFrame(rafId);
      document.removeEventListener('keydown', handleKeyDown);
      prevFocusRef.current?.focus();
      prevFocusRef.current = null;
    };
  }, [open, onClose]);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const begin = useCallback(async () => {
    setPhase({ kind: 'loading' });
    try {
      const init = await invoke<PairInit>('relay_pair_init', {
        projectRoot: projectRoot ?? null,
      });
      if (!mountedRef.current) return;
      setPhase({ kind: 'ready', init });
    } catch (e) {
      if (!mountedRef.current) return;
      setPhase({ kind: 'error', message: String(e) });
    }
  }, [projectRoot]);

  // Kick off pair_init when the modal opens; reset state when it closes.
  useEffect(() => {
    if (!open) {
      stopPolling();
      setPhase({ kind: 'loading' });
      return;
    }
    begin();
    return stopPolling;
  }, [open, begin, stopPolling]);

  // Poll for completion once we have a pair_code.
  useEffect(() => {
    if (phase.kind !== 'ready') return;
    const code = phase.init.pair_code;
    pollRef.current = window.setInterval(async () => {
      try {
        const status = await invoke<PairStatus>('relay_pair_status', {
          pairCode: code,
          projectRoot: projectRoot ?? null,
        });
        if (!mountedRef.current) return;
        if (status.claimed) {
          stopPolling();
          setPhase({
            kind: 'claimed',
            init: phase.init,
            deviceName: status.device_name ?? 'phone',
          });
        } else if (status.expired) {
          stopPolling();
          setPhase({ kind: 'expired' });
        }
      } catch {
        // Transient network errors are fine — we'll retry on the next tick.
      }
    }, POLL_INTERVAL_MS);
    return stopPolling;
  }, [phase, projectRoot, stopPolling]);

  // Auto-close ~1.5s after a successful pairing so the user sees confirmation.
  useEffect(() => {
    if (phase.kind !== 'claimed') return;
    const t = window.setTimeout(() => onClose(), 1500);
    return () => window.clearTimeout(t);
  }, [phase, onClose]);

  const qrPayload = useMemo(() => {
    if (phase.kind !== 'ready' && phase.kind !== 'claimed') return '';
    return JSON.stringify({
      url: phase.init.url,
      code: phase.init.pair_code,
    });
  }, [phase]);

  if (!open) return null;

  return (
    <div className="kim-modal-backdrop" onClick={onClose}>
      <div
        ref={dialogRef}
        className="kim-modal kim-pairing-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="kim-pairing-modal-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="kim-modal__header">
          <div>
            <div id="kim-pairing-modal-title" className="kim-modal__title">Pair a phone</div>
            <div className="kim-pairing-modal__subtitle">
              Scan from Kim on your phone (Settings → Phone Relay).
            </div>
          </div>
          <button className="kim-modal__close" onClick={onClose} aria-label="Close">×</button>
        </div>

        <div className="kim-modal__body kim-pairing-modal__body">
          {phase.kind === 'loading' && (
            <div className="kim-pairing-modal__loading">Generating pair code…</div>
          )}

          {phase.kind === 'error' && (
            <>
              <div className="kim-pairing-modal__error">{phase.message}</div>
              <button className="kim-btn kim-btn--primary" onClick={begin}>Try again</button>
            </>
          )}

          {phase.kind === 'expired' && (
            <>
              <div className="kim-pairing-modal__error">
                That code expired before it was scanned.
              </div>
              <button className="kim-btn kim-btn--primary" onClick={begin}>
                Generate a new code
              </button>
            </>
          )}

          {phase.kind === 'ready' && (
            <>
              <div className="kim-pairing-modal__qr">
                <QRCodeSVG value={qrPayload} size={232} level="M" includeMargin={false} />
              </div>
              <div className="kim-pairing-modal__code">{phase.init.pair_code}</div>
              <div className="kim-pairing-modal__hint">
                or paste this in the phone's pairing screen
              </div>
              <pre className="kim-pairing-modal__payload">{qrPayload}</pre>
            </>
          )}

          {phase.kind === 'claimed' && (
            <>
              <div className="kim-pairing-modal__success">
                <span className="kim-pairing-modal__dot" /> Paired with {phase.deviceName}
              </div>
              <div className="kim-pairing-modal__hint">
                Your phone can now send prompts to this PC.
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
