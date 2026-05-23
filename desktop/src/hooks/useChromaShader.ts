/**
 * Shared WebGL "liquid chrome" shader hook.
 * Used by OnboardingFlow and SettingsPanel to render the same
 * animated chrome/metal background.
 */
import { useEffect, useRef } from 'react';

const VERT = `
attribute vec2 a_pos;
void main(){ gl_Position = vec4(a_pos,0.,1.); }
`;

const FRAG = `
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

export function useChromaShader(
  canvasRef: React.RefObject<HTMLCanvasElement | null>,
  /** Bound the mouse tracking to this element (defaults to window) */
  trackEl?: React.RefObject<HTMLElement | null>,
) {
  const mouseRef = useRef({ x: 0.5, y: 0.5, tx: 0.5, ty: 0.5 });
  const rafRef   = useRef<number>(0);

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
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);

    const buf = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW);

    const target = trackEl?.current ?? window as unknown as HTMLElement;
    const onMove = (e: Event) => {
      const me = e as MouseEvent;
      const el = trackEl?.current;
      if (el) {
        const r = el.getBoundingClientRect();
        mouseRef.current.tx = (me.clientX - r.left) / r.width;
        mouseRef.current.ty = 1 - (me.clientY - r.top)  / r.height;
      } else {
        mouseRef.current.tx = me.clientX / window.innerWidth;
        mouseRef.current.ty = 1 - me.clientY / window.innerHeight;
      }
    };
    target.addEventListener('mousemove', onMove);

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio ?? 1, 1.75);
      const r   = canvas.parentElement?.getBoundingClientRect();
      canvas.width  = Math.floor((r?.width  ?? window.innerWidth)  * dpr);
      canvas.height = Math.floor((r?.height ?? window.innerHeight) * dpr);
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
      gl.uniform2f(ul('u_res')!,   canvas.width, canvas.height);
      gl.uniform2f(ul('u_mouse')!, m.x, m.y);
      gl.uniform1f(ul('u_time')!,  (performance.now() - t0) / 1000);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      rafRef.current = requestAnimationFrame(frame);
    };
    rafRef.current = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(rafRef.current);
      target.removeEventListener('mousemove', onMove);
      window.removeEventListener('resize', resize);
    };
  }, [canvasRef, trackEl]);
}
