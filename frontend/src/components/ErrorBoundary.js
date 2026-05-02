import React from "react";
import { AlertTriangle, RotateCcw, ArrowLeft } from "lucide-react";

/**
 * ErrorBoundary — last-line-of-defense React error boundary.
 *
 * Why it exists:
 *   Any uncaught render error would otherwise unmount the whole React
 *   tree and leave the user staring at a blank page. We'd rather catch
 *   the error, show what happened, and give them a way to recover
 *   without reloading the entire app. Common triggers historically:
 *     • toast.error() receiving a FastAPI validation array → Sonner
 *       tries to render an object as a React child and throws.
 *     • A server response missing a field that JSX deref'd unsafely
 *       (e.g. `mission.foo.bar` when foo became null).
 *
 * Usage:
 *   <ErrorBoundary>
 *     <Routes>…</Routes>
 *   </ErrorBoundary>
 *
 * The boundary resets its internal error state when the user clicks
 * "Try again", which re-mounts children. "Back" uses history.back().
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Log to the console so devs can still find the stack in prod.
    // eslint-disable-next-line no-console
    console.error("[ErrorBoundary]", error, info?.componentStack);
  }

  reset = () => {
    this.setState({ error: null });
  };

  goBack = () => {
    this.reset();
    if (window.history.length > 1) window.history.back();
    else window.location.href = "/";
  };

  render() {
    if (!this.state.error) return this.props.children;
    const msg = this.state.error?.message || String(this.state.error);
    return (
      <div
        className="min-h-screen flex items-center justify-center px-6"
        style={{ background: "var(--bg)", color: "var(--fg)" }}
        data-testid="app-error-boundary"
      >
        <div className="max-w-md w-full sp-glass-strong rounded-2xl p-6 space-y-4">
          <div className="flex items-center gap-3 text-amber-300">
            <AlertTriangle className="w-5 h-5" />
            <h2 className="text-lg font-medium tracking-tight" style={{ color: "var(--fg)" }}>
              Something broke on this screen
            </h2>
          </div>
          <p className="text-sm sp-subtle leading-relaxed">
            We caught the error before it crashed the rest of the app. You
            can try again — if it keeps happening, please share this
            message with support:
          </p>
          <pre
            className="text-[11px] rounded-lg p-3 overflow-auto max-h-40"
            style={{ background: "var(--chip-bg)", border: "1px solid var(--border-soft)" }}
          >
            {msg}
          </pre>
          <div className="flex gap-2 justify-end">
            <button
              type="button"
              onClick={this.goBack}
              className="sp-btn sp-btn-ghost text-xs"
              data-testid="error-boundary-back"
            >
              <ArrowLeft className="w-3.5 h-3.5" /> Back
            </button>
            <button
              type="button"
              onClick={this.reset}
              className="sp-btn sp-btn-primary text-xs"
              data-testid="error-boundary-retry"
            >
              <RotateCcw className="w-3.5 h-3.5" /> Try again
            </button>
          </div>
        </div>
      </div>
    );
  }
}
