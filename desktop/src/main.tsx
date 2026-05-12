import React from "react";
import ReactDOM from "react-dom/client";
import "./design-mocks/tokens.css";
import "./design-mocks/styles.css";
import App from "./App";
import { CancelWidget } from "./components/CancelWidget";
import { FullWindowShell } from "./design-mocks/FullWindowShell";

const params = new URLSearchParams(window.location.search);
const isCancelWindow = params.get("window") === "cancel";
/** Dev-only: full mock shell — Kim UI uses `kim-*`; mocks use `dm-*`, so normal app looks unchanged until we merge styles or enable preview. */
const designPreview =
  import.meta.env.DEV &&
  (import.meta.env.VITE_DESIGN_PREVIEW === "true" || params.get("design") === "1");

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
