import React from "react";

interface Props {
  children: React.ReactNode;
  /**
   * V-audit #7: optional scoped fallback. When provided, it replaces the
   * default app-wide "Something went wrong" / Reload screen with a narrower
   * one (e.g. "this conversation failed to render") that doesn't blank the
   * rest of the app (sidebar, session navigation, etc). Receives the caught
   * error's message and a `reset` callback that clears the boundary's error
   * state in place, without a full `window.location.reload()`.
   */
  fallback?: (message: string, reset: () => void) => React.ReactNode;
  /**
   * When this value changes while the boundary is showing an error, the
   * boundary automatically resets — e.g. pass the active session id so
   * navigating to a different conversation clears a stale crash instead of
   * leaving the fallback stuck on screen for content that's no longer shown.
   */
  resetKey?: string | number | null;
}

interface State {
  hasError: boolean;
  message: string;
}

export class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, message: "" };
  }

  static getDerivedStateFromError(error: unknown): State {
    const message =
      error instanceof Error ? error.message : String(error ?? "Unknown render error");
    return { hasError: true, message };
  }

  override componentDidCatch(error: unknown, info: React.ErrorInfo) {
    const msg = error instanceof Error ? error.message : String(error ?? "");
    console.error("[ErrorBoundary] render error:", msg, info.componentStack);
    // Route to optional sink if DSN is configured (deferred wiring).
    // import.meta.env is typed via vite/client (src/vite-env.d.ts); its string
    // index signature makes an undeclared VITE_* key resolve without an `any`.
    const dsn = import.meta.env.VITE_ERROR_DSN as string | undefined;
    if (dsn) {
      void fetch(dsn, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message: msg, stack: info.componentStack }),
      }).catch(() => undefined);
    }
  }

  override componentDidUpdate(prevProps: Props) {
    // V-audit #7: auto-clear a stale crash when the caller signals the
    // underlying content changed (e.g. the user navigated to a different
    // session) rather than leaving the scoped fallback stuck on screen.
    if (this.state.hasError && prevProps.resetKey !== this.props.resetKey) {
      this.reset();
    }
  }

  private reset = () => {
    this.setState({ hasError: false, message: "" });
  };

  private handleReload = () => {
    window.location.reload();
  };

  override render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback(this.state.message, this.reset);
      }
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            height: "100vh",
            gap: "12px",
            fontFamily: "var(--font-sans, system-ui, sans-serif)",
            color: "var(--color-text-primary, #111)",
            background: "var(--color-bg, #fff)",
            padding: "24px",
            textAlign: "center",
          }}
        >
          <svg
            width="40"
            height="40"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" />
          </svg>
          <h2 style={{ margin: 0, fontSize: "1.125rem", fontWeight: 600 }}>
            Something went wrong
          </h2>
          {this.state.message && (
            <p
              style={{
                margin: 0,
                fontSize: "0.8125rem",
                opacity: 0.6,
                maxWidth: "360px",
                wordBreak: "break-word",
              }}
            >
              {this.state.message}
            </p>
          )}
          <button
            onClick={this.handleReload}
            style={{
              marginTop: "8px",
              padding: "8px 20px",
              borderRadius: "6px",
              border: "1px solid var(--color-border, #d1d5db)",
              background: "var(--color-surface, #f9fafb)",
              color: "inherit",
              fontSize: "0.875rem",
              cursor: "pointer",
            }}
          >
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
