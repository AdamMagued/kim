import { useState, useEffect, useRef, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { KimAccount } from '../types';

// ── WebGL chrome shader ────────────────────────────────────────────────────────

const VERT_SRC = `
attribute vec2 a_pos;
void main(){ gl_Position = vec4(a_pos,0.,1.); }
`;

const FRAG_SRC = `
precision highp float;
uniform vec2 u_res; uniform vec2 u_mouse; uniform float u_time;
float hash(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p);float a=hash(i),b=hash(i+vec2(1,0)),c=hash(i+vec2(0,1)),d=hash(i+vec2(1,1));vec2 u=f*f*(3.-2.*f);return mix(a,b,u.x)+(c-a)*u.y*(1.-u.x)+(d-b)*u.x*u.y;}
float fbm(vec2 p){float v=0.,a=.5;for(int i=0;i<5;i++){v+=a*noise(p);p*=2.03;a*=.5;}return v;}
vec3 chrome(float t){t=clamp(t,0.,1.);vec3 a=vec3(.03,.04,.05),b=vec3(.55,.58,.62),c=vec3(.92,.94,.97);return mix(mix(a,b,smoothstep(0.,.6,t)),c,smoothstep(.7,.98,t));}
void main(){
  vec2 uv=gl_FragCoord.xy/u_res.xy;
  vec2 p=uv;p.x*=u_res.x/u_res.y;
  vec2 m=u_mouse;m.x*=u_res.x/u_res.y;
  float t=u_time*.05;
  vec2 q=vec2(fbm(p*2.+t),fbm(p*2.+vec2(3.4,1.2)-t));
  vec2 r=vec2(fbm(p*3.+q*2.2+vec2(1.7,9.2)+t*.7),fbm(p*3.+q*2.2+vec2(8.3,2.8)-t*.7));
  float d=distance(p,m);float well=exp(-d*d*10.);
  r+=(m-p)*well*.9;
  float f=fbm(p*2.2+r*1.5)-well*.18;
  vec3 col=chrome(f);
  col+=(hash(gl_FragCoord.xy+u_time)-.5)*.012;
  float vg=smoothstep(1.4,.3,length((uv-.5)*vec2(1.8,1.)));
  col*=mix(.18,1.,vg);
  gl_FragColor=vec4(col,1.);
}
`;

function useWebGL(canvasRef: React.RefObject<HTMLCanvasElement | null>) {
  const mouseRef = useRef({ x: 0.5, y: 0.5, tx: 0.5, ty: 0.5 });
  const rafRef = useRef<number>(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const gl = canvas.getContext('webgl', { antialias: false });
    if (!gl) return;

    const sh = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src);
      gl.compileShader(s);
      return s;
    };
    const prog = gl.createProgram()!;
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT_SRC));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG_SRC));
    gl.linkProgram(prog);

    const buf = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW);

    const onMove = (e: MouseEvent) => {
      mouseRef.current.tx = e.clientX / window.innerWidth;
      mouseRef.current.ty = 1 - e.clientY / window.innerHeight;
    };
    window.addEventListener('mousemove', onMove);

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio ?? 1, 1.75);
      canvas.width  = Math.floor(window.innerWidth  * dpr);
      canvas.height = Math.floor(window.innerHeight * dpr);
    };
    window.addEventListener('resize', resize);
    resize();

    const t0 = performance.now();
    const ul = (n: string) => gl.getUniformLocation(prog, n);

    const frame = () => {
      const m = mouseRef.current;
      m.x += (m.tx - m.x) * 0.06;
      m.y += (m.ty - m.y) * 0.06;

      gl.useProgram(prog);
      const ap = gl.getAttribLocation(prog, 'a_pos');
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.enableVertexAttribArray(ap);
      gl.vertexAttribPointer(ap, 2, gl.FLOAT, false, 0, 0);
      gl.uniform2f(ul('u_res')!, canvas.width, canvas.height);
      gl.uniform2f(ul('u_mouse')!, m.x, m.y);
      gl.uniform1f(ul('u_time')!, (performance.now() - t0) / 1000);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      rafRef.current = requestAnimationFrame(frame);
    };
    rafRef.current = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(rafRef.current);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('resize', resize);
    };
  }, [canvasRef]);
}

// ── Icons ─────────────────────────────────────────────────────────────────────

function ArrowIcon() {
  return (
    <svg viewBox="0 0 15 15" fill="none" xmlns="http://www.w3.org/2000/svg" style={{ width: 15, height: 15 }}>
      <path d="M2 7.5h11M9 3.5l4 4-4 4" stroke="#0c0c0c" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function GitHubIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" fill="none" width="14" height="14">
      <circle cx="8" cy="8" r="6.5" stroke="rgba(100,220,120,.8)" strokeWidth="1.3" />
      <path d="M5 8.5l2 2 4-4" stroke="rgba(100,220,120,.9)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ── KimLogoMark ───────────────────────────────────────────────────────────────

function KimAsterisk() {
  return (
    <div className="kim-ob__logo-mark">
      <svg viewBox="0 0 28 28" fill="none">
        <line x1="14" y1="3" x2="14" y2="25" stroke="rgba(255,255,255,.88)" strokeWidth="1.5" strokeLinecap="round" />
        <line x1="3.8" y1="8.5" x2="24.2" y2="19.5" stroke="rgba(255,255,255,.88)" strokeWidth="1.5" strokeLinecap="round" />
        <line x1="3.8" y1="19.5" x2="24.2" y2="8.5" stroke="rgba(255,255,255,.88)" strokeWidth="1.5" strokeLinecap="round" />
        <line x1="11.2" y1="6.5" x2="14" y2="9.8" stroke="rgba(255,255,255,.38)" strokeWidth="1" strokeLinecap="round" />
        <line x1="16.8" y1="6.5" x2="14" y2="9.8" stroke="rgba(255,255,255,.38)" strokeWidth="1" strokeLinecap="round" />
        <line x1="11.2" y1="21.5" x2="14" y2="18.2" stroke="rgba(255,255,255,.38)" strokeWidth="1" strokeLinecap="round" />
        <line x1="16.8" y1="21.5" x2="14" y2="18.2" stroke="rgba(255,255,255,.38)" strokeWidth="1" strokeLinecap="round" />
      </svg>
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

interface Props {
  onComplete: (account: KimAccount) => void;
}

export function OnboardingFlow({ onComplete }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useWebGL(canvasRef);

  // Step: 'name' → 'github' → 'done'
  const [step, setStep] = useState<'name' | 'github'>('name');
  const [leaving, setLeaving] = useState(false); // triggers exit animation before step change

  // Name step
  const [name, setName] = useState('');
  const nameReady = name.trim().length >= 1;

  // GitHub step
  const [token, setToken] = useState('');
  const [verifying, setVerifying] = useState(false);
  const [githubUser, setGithubUser] = useState<{ login: string; name: string | null; avatar_url: string } | null>(null);
  const [tokenError, setTokenError] = useState('');
  const [saving, setSaving] = useState(false);

  const nameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const t = setTimeout(() => nameInputRef.current?.focus(), 900);
    return () => clearTimeout(t);
  }, []);

  function goToGithub() {
    if (!nameReady) return;
    setLeaving(true);
    setTimeout(() => {
      setStep('github');
      setLeaving(false);
    }, 380);
  }

  async function verifyToken() {
    if (!token.trim()) return;
    setVerifying(true);
    setTokenError('');
    try {
      const user = await invoke<{ login: string; name: string | null; avatar_url: string }>(
        'verify_github_pat',
        { token: token.trim() }
      );
      setGithubUser(user);
    } catch (err) {
      setTokenError(typeof err === 'string' ? err : 'Could not verify token.');
    } finally {
      setVerifying(false);
    }
  }

  const handleFinish = useCallback(async () => {
    if (!nameReady || saving) return;
    setSaving(true);
    const account: KimAccount = {
      display_name: name.trim(),
      github_username: githubUser?.login,
      github_token: githubUser ? token.trim() : undefined,
      github_avatar_url: githubUser?.avatar_url,
      gist_id: undefined,
      created_at: new Date().toISOString(),
    };
    try {
      await invoke('save_account', { account });
      // Exit animation before calling onComplete
      setLeaving(true);
      setTimeout(() => onComplete(account), 500);
    } catch {
      setSaving(false);
    }
  }, [nameReady, saving, name, githubUser, token, onComplete]);

  function skipGitHub() {
    handleFinish();
  }

  const screenClass = `kim-ob__screen${leaving ? ' kim-ob__screen--out' : ' kim-ob__screen--in'}`;

  return (
    <div className="kim-ob">
      {/* Full-bleed WebGL canvas */}
      <canvas ref={canvasRef} className="kim-ob__canvas" />

      {/* Radial scrim */}
      <div className="kim-ob__scrim" />

      {/* Content */}
      {step === 'name' && (
        <div className={screenClass} key="name">
          <div className="kim-ob__inner">
            <KimAsterisk />

            <div className="kim-ob__wordmark">Kim</div>
            <div className="kim-ob__tagline">What should I call you?</div>

            <div className="kim-ob__input-shell">
              <div className="kim-ob__input-row">
                <input
                  ref={nameInputRef}
                  type="text"
                  placeholder="Your name…"
                  autoComplete="given-name"
                  spellCheck={false}
                  value={name}
                  onChange={e => setName(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter' && nameReady) goToGithub(); }}
                  className="kim-ob__input"
                />
                <button
                  className={`kim-ob__send-btn${nameReady ? ' kim-ob__send-btn--ready' : ''}`}
                  onClick={goToGithub}
                  disabled={!nameReady}
                  aria-label="Continue"
                >
                  <ArrowIcon />
                </button>
              </div>
            </div>

            <div className="kim-ob__footer">Kim · your data stays private</div>
          </div>
        </div>
      )}

      {step === 'github' && (
        <div className={screenClass} key="github">
          <div className="kim-ob__inner">
            <div className="kim-ob__github-title">
              <GitHubIcon />
              Connect GitHub <span style={{ opacity: 0.45, fontWeight: 400 }}>— optional</span>
            </div>
            <div className="kim-ob__github-desc">
              Link a personal access token to back up your Kim account to a private Gist.
              Create one at <strong>github.com/settings/tokens</strong> with{' '}
              <code>gist</code> + <code>read:user</code> scopes.
            </div>

            <div className="kim-ob__token-row">
              <input
                type="password"
                placeholder="ghp_…"
                spellCheck={false}
                value={token}
                onChange={e => { setToken(e.target.value); setGithubUser(null); setTokenError(''); }}
                onKeyDown={e => { if (e.key === 'Enter' && token.trim() && !githubUser) verifyToken(); }}
                className="kim-ob__token-input"
                autoFocus
              />
              <button
                className={`kim-ob__verify-btn${githubUser ? ' kim-ob__verify-btn--done' : ''}`}
                onClick={verifyToken}
                disabled={!token.trim() || verifying || !!githubUser}
              >
                {verifying ? 'Checking…' : githubUser ? <><CheckIcon /> Connected</> : 'Verify'}
              </button>
            </div>

            {tokenError && <div className="kim-ob__token-error">{tokenError}</div>}
            {githubUser && (
              <div className="kim-ob__token-success">
                <CheckIcon />
                Signed in as <strong>{githubUser.name ?? githubUser.login}</strong>
              </div>
            )}

            <div className="kim-ob__github-actions">
              <button className="kim-ob__skip-btn" onClick={skipGitHub} disabled={saving}>
                {saving ? 'Setting up…' : 'Skip for now'}
              </button>
              <button
                className={`kim-ob__send-btn kim-ob__send-btn--wide${githubUser && !saving ? ' kim-ob__send-btn--ready' : ''}`}
                onClick={handleFinish}
                disabled={saving}
              >
                {saving ? 'Setting up…' : 'Get started →'}
              </button>
            </div>

            <div className="kim-ob__footer">Hi, {name} — welcome to Kim</div>
          </div>
        </div>
      )}
    </div>
  );
}
