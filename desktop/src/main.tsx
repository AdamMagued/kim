import React from "react";
import ReactDOM from "react-dom/client";
import "./design-mocks/tokens.css";
import "./design-mocks/styles.css";
import App from "./App";
import { CancelWidget } from "./components/CancelWidget";
import { FullWindowShell } from "./design-mocks/FullWindowShell";

const params = new URLSearchParams(window.location.search);
const isCancelWindow = params.get("window") === "cancel";

/**
 * Kim uses `kim-*` CSS; design mocks use `dm-*`. Importing tokens alone does not change the real UI.
 *
 * In **development**, we show the converter’s `FullWindowShell` by default so you actually see the mocks.
 * To run the real Kim app: `VITE_KIM_REAL_UI=true` in `.env.development.local`, or open `?kim=1`.
 */
const realKimUi =
  import.meta.env.PROD ||
  import.meta.env.VITE_KIM_REAL_UI === "true" ||
  params.get("kim") === "1";

const designPreview = import.meta.env.DEV && !realKimUi;

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    {isCancelWindow ? (
      <CancelWidget />
    ) : designPreview ? (
      <FullWindowShell />
    ) : (
      <App />
    )}
  </React.StrictMode>,
);
