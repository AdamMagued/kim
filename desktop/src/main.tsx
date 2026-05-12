import React from "react";
import ReactDOM from "react-dom/client";
import "./design-mocks/tokens.css";
import "./design-mocks/styles.css";
import App from "./App";
import { CancelWidget } from "./components/CancelWidget";

const isCancelWindow = new URLSearchParams(window.location.search).get("window") === "cancel";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    {isCancelWindow ? <CancelWidget /> : <App />}
  </React.StrictMode>,
);
