import React from "react";
import ReactDOM from "react-dom/client";
import "./styles/design-tokens.css";
import App from "./App";
import { CancelWidget } from "./components/CancelWidget";
import { ErrorBoundary } from "./components/ErrorBoundary";

// Surface unhandled promise rejections so they appear in the console and
// are not silently swallowed by Tauri's webview.
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason instanceof Error
    ? `${event.reason.message}\n${event.reason.stack ?? ""}`
    : String(event.reason ?? "unhandledrejection");
  console.error("[kim] unhandled promise rejection:", reason);
});

// Catch synchronous errors that escape React's tree.
window.onerror = (message, source, lineno, colno, error) => {
  const detail = error instanceof Error ? error.stack ?? error.message : String(message);
  console.error("[kim] window.onerror:", detail, { source, lineno, colno });
  // Return false so the browser still logs the error normally.
  return false;
};

const isCancelWindow = new URLSearchParams(window.location.search).get("window") === "cancel";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ErrorBoundary>
      {isCancelWindow ? <CancelWidget /> : <App />}
    </ErrorBoundary>
  </React.StrictMode>,
);
