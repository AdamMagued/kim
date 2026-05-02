import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { CancelWidget } from "./components/CancelWidget";

const isCancelWindow = window.location.search.includes("window=cancel");

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    {isCancelWindow ? <CancelWidget /> : <App />}
  </React.StrictMode>,
);
