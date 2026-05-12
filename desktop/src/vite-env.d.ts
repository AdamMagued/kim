/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** When `"true"`, dev runs real Kim (`App`). Otherwise dev defaults to design mock shell. */
  readonly VITE_KIM_REAL_UI?: string;
}
