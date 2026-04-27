// Bloop mascot — pure CSS + SVG
// 6 animation states: idle, thinking, processing, success, error, waiting
import { useEffect } from 'react';

export type BloopState = 'idle' | 'thinking' | 'processing' | 'success' | 'error' | 'waiting';

const BLOOP_STYLES = `
.bloop-shadow {
  position: absolute;
  bottom: 20%;
  width: 56px; height: 9px;
  border-radius: 50%;
  background: radial-gradient(ellipse, rgba(0,0,0,0.22), rgba(0,0,0,0) 70%);
  filter: blur(1px);
  transform-origin: center;
  animation: bloop-shadow-breathe 3.2s ease-in-out infinite;
}
@keyframes bloop-shadow-breathe {
  0%, 100% { transform: scaleX(1); opacity: 0.7; }
  50% { transform: scaleX(0.85); opacity: 0.45; }
}

.bloop-char {
  position: relative;
  width: 92px; height: 88px;
  border-radius: 52% 48% 50% 50% / 58% 58% 42% 42%;
  background:
    radial-gradient(circle at 34% 30%, #fffaf2 0%, #fae4cf 55%, #f0c6a8 100%);
  box-shadow:
    inset -6px -10px 14px rgba(200,130,100,0.22),
    inset 5px 6px 10px rgba(255,255,255,0.55),
    0 10px 24px rgba(200,130,100,0.25);
  transform-origin: 50% 92%;
  animation: bloop-idle-body 3.2s ease-in-out infinite;
}
.bloop-char::before, .bloop-char::after {
  content: '';
  position: absolute;
  top: 54%;
  width: 11px; height: 7px;
  border-radius: 50%;
  background: radial-gradient(ellipse, rgba(255,130,140,0.55), rgba(255,130,140,0) 75%);
  pointer-events: none;
}
.bloop-char::before { left: 14%; }
.bloop-char::after  { right: 14%; }

.bloop-face {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 18px;
  top: -6%;
}
.bloop-eye {
  width: 7px; height: 9px;
  background: #2a2018;
  border-radius: 50%;
  transform-origin: center;
}

.bloop-char[data-state="idle"] { animation: bloop-idle-body 3.4s ease-in-out infinite; }
.bloop-char[data-state="idle"] .bloop-eye { animation: bloop-blink 4.5s infinite; }
@keyframes bloop-idle-body {
  0%, 100% { transform: translateY(0) scale(1, 1); }
  50%      { transform: translateY(-4px) scale(1.02, 0.98); }
}
@keyframes bloop-blink {
  0%, 92%, 100% { transform: scaleY(1); }
  94%, 98%      { transform: scaleY(0.1); }
}

.bloop-char[data-state="thinking"] { animation: bloop-think-body 2.4s ease-in-out infinite; }
.bloop-char[data-state="thinking"] .bloop-eye { animation: bloop-look 2.4s ease-in-out infinite; }
@keyframes bloop-think-body {
  0%, 100% { transform: rotate(-6deg) translateY(-1px); }
  50%      { transform: rotate(6deg)  translateY(-3px); }
}
@keyframes bloop-look {
  0%, 100% { transform: translateX(-1.5px); }
  50%      { transform: translateX(1.5px); }
}

.bloop-char[data-state="processing"] { animation: bloop-proc 0.7s ease-in-out infinite; }
.bloop-char[data-state="processing"] .bloop-eye { animation: bloop-scan 0.7s linear infinite; }
@keyframes bloop-proc {
  0%, 100% { transform: scale(1, 1) translateY(0); }
  25%      { transform: scale(1.08, 0.92) translateY(2px); }
  50%      { transform: scale(0.95, 1.05) translateY(-4px); }
  75%      { transform: scale(1.05, 0.95) translateY(1px); }
}
@keyframes bloop-scan {
  0%, 100% { transform: translateX(-2px); }
  50%      { transform: translateX(2px); }
}

.bloop-char[data-state="success"] { animation: bloop-success 1.6s cubic-bezier(0.3, 1.3, 0.5, 1) infinite; }
.bloop-char[data-state="success"] .bloop-eye {
  background: transparent;
  width: 8px; height: 8px;
  border: 2px solid #2a2018;
  border-color: transparent transparent #2a2018 #2a2018;
  transform: rotate(-45deg);
  border-radius: 50%;
  animation: none;
}
@keyframes bloop-success {
  0%   { transform: translateY(0)   scale(1, 1); }
  25%  { transform: translateY(-14px) scale(0.94, 1.08); }
  50%  { transform: translateY(-2px) scale(1.12, 0.88); }
  70%  { transform: translateY(-6px) scale(0.98, 1.03); }
  100% { transform: translateY(0)   scale(1, 1); }
}

.bloop-char[data-state="error"] { animation: bloop-error 1.4s ease-in-out infinite; }
.bloop-char[data-state="error"] .bloop-face { top: 2%; }
.bloop-char[data-state="error"] .bloop-eye { animation: none; }
.bloop-char[data-state="error"] .bloop-mouth {
  position: absolute;
  bottom: 28%;
  left: 50%;
  transform: translateX(-50%);
  width: 14px; height: 7px;
  border: 2px solid #2a2018;
  border-color: #2a2018 transparent transparent transparent;
  border-radius: 50%;
}
@keyframes bloop-error {
  0%, 100% { transform: translateX(0) rotate(0); }
  15%      { transform: translateX(-4px) rotate(-4deg); }
  30%      { transform: translateX(4px) rotate(4deg); }
  45%      { transform: translateX(-3px) rotate(-3deg); }
  60%      { transform: translateX(3px) rotate(3deg); }
  75%      { transform: translateX(-1px) rotate(-1deg); }
}

.bloop-char[data-state="waiting"] { animation: bloop-wait 2.8s ease-in-out infinite; }
@keyframes bloop-wait {
  0%, 100% { transform: translateY(0) rotate(-3deg); }
  50%      { transform: translateY(-3px) rotate(3deg); }
}
`;

function injectStyles() {
  if (typeof document === 'undefined') return;
  if (document.getElementById('bloop-styles')) return;
  const s = document.createElement('style');
  s.id = 'bloop-styles';
  s.textContent = BLOOP_STYLES;
  document.head.appendChild(s);
}

interface BloopProps {
  state?: BloopState;
  scale?: number;
}

export function Bloop({ state = 'idle', scale = 1 }: BloopProps) {
  useEffect(() => {
    injectStyles();
  }, []);

  return (
    <div
      className="bloop-char"
      data-state={state}
      style={{ transform: `scale(${scale})` }}
    >
      <div className="bloop-face">
        <div className="bloop-eye"></div>
        <div className="bloop-eye"></div>
      </div>
      {state === 'error' && <div className="bloop-mouth"></div>}
    </div>
  );
}
