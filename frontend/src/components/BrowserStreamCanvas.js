import React, { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, MousePointer2, Hand, X, ExternalLink, Lock, Save } from "lucide-react";

/**
 * BrowserStreamCanvas — ChatGPT Agent Mode-style live remote browser.
 *
 * Connects to /api/missions/:id/browser-stream over a WebSocket, paints
 * the JPEG screencast frames onto a <canvas>, and forwards the user's
 * mouse + keyboard events back so they can authenticate / interact.
 *
 * Two modes:
 *   - "watch"  (default): pointer-events disabled, agent-driven only.
 *                          Useful for "look over the agent's shoulder".
 *   - "drive": user has taken control. Agent loop is server-side paused
 *              (see browser_stream._append_control_event). User clicks
 *              and types are forwarded via Input.dispatchMouseEvent /
 *              dispatchKeyEvent through CDP.
 *
 * Sizing: the JPEG payload comes with explicit `w/h`. We render it at
 * display CSS pixels and translate the user's local click coords back
 * to remote pixels via a simple scale factor.
 */
export default function BrowserStreamCanvas({
  missionId,
  token,
  onClose,
  initiallyDrive = false,
  needsLoginFor = "",
}) {
  const canvasRef = useRef(null);
  const wsRef = useRef(null);
  const [mode, setMode] = useState(initiallyDrive ? "drive" : "watch");
  const [connected, setConnected] = useState(false);
  const [closeInfo, setCloseInfo] = useState("");  // surfaced if WS dies
  const [meta, setMeta] = useState({ url: "", title: "" });
  const [savedHost, setSavedHost] = useState("");  // shows "✓ saved" toast inline
  const [saving, setSaving] = useState(false);
  const remoteSize = useRef({ w: 1280, h: 800 });
  const retryRef = useRef({ attempt: 0, timer: null });

  // Track whether we've sent the "take" control message — survives
  // remounts so a quick toggle doesn't double-fire events.
  const drivingRef = useRef(initiallyDrive);
  useEffect(() => { drivingRef.current = mode === "drive"; }, [mode]);

  // ── Connect WS ────────────────────────────────────────────────────
  // Pulled into a stable ref so onclose can reschedule itself.
  useEffect(() => {
    if (!missionId) return undefined;
    if (!token) {
      // Surface the auth gap clearly instead of just sitting on
      // "Connecting…" forever — usually means the user signed in via
      // Google and the parent component hasn't resolved the Firebase
      // ID token yet (or is reading the wrong localStorage key).
      setCloseInfo("Waiting for auth token…");
      return undefined;
    }
    setCloseInfo("");

    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const apiBase = (process.env.REACT_APP_BACKEND_URL || "").replace(/^https?:/, `${proto}:`);
      const url = `${apiBase}/api/missions/${missionId}/browser-stream?token=${encodeURIComponent(token)}`;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        retryRef.current.attempt = 0;
        setCloseInfo("");
        setConnected(true);
        // If the parent wanted to start in drive mode, send the take
        // control packet immediately so the agent loop pauses on
        // reconnect.
        if (drivingRef.current) {
          ws.send(JSON.stringify({ type: "control", action: "take" }));
        }
      };

      ws.onclose = (ev) => {
        setConnected(false);
        // Surface the close code so the user knows whether it was an
        // auth issue (4403), a server crash (1006), or a clean close.
        const reason = ev?.reason || "";
        const code = ev?.code || 0;
        if (cancelled) return;
        // Don't auto-retry on auth failures — the token's bad and
        // hammering won't fix it.
        if (code === 4403) {
          setCloseInfo("Forbidden — please refresh and sign in again.");
          return;
        }
        // Otherwise retry up to 3 times with backoff.
        const attempt = retryRef.current.attempt + 1;
        retryRef.current.attempt = attempt;
        if (attempt > 3) {
          setCloseInfo(
            `Disconnected (${code}${reason ? ` · ${reason}` : ""}). ` +
            `Tap close, then re-open the live browser to retry.`
          );
          return;
        }
        const delay = Math.min(8000, 800 * 2 ** (attempt - 1));
        setCloseInfo(`Reconnecting (try ${attempt}/3 in ${Math.round(delay / 1000)}s)…`);
        retryRef.current.timer = setTimeout(connect, delay);
      };

      ws.onerror = () => { /* close handler will fire next */ };

      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "frame") {
          paintFrame(msg.data, msg.w, msg.h);
        } else if (msg.type === "hello") {
          setMeta({ url: msg.url || "", title: msg.title || "" });
        } else if (msg.type === "credentials_saved") {
          setSaving(false);
          if (msg.ok) setSavedHost(msg.hostname || "");
        }
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (retryRef.current.timer) {
        clearTimeout(retryRef.current.timer);
        retryRef.current.timer = null;
      }
      try { wsRef.current?.close(); } catch { /* ignore */ }
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missionId, token]);

  // ── Frame painter ────────────────────────────────────────────────
  const paintFrame = useCallback((b64, w, h) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (w && h) remoteSize.current = { w, h };
    const img = new Image();
    img.onload = () => {
      const canv = canvasRef.current;
      if (!canv) return;
      if (canv.width !== w || canv.height !== h) {
        canv.width = w;
        canv.height = h;
      }
      const ctx = canv.getContext("2d");
      ctx.drawImage(img, 0, 0, w, h);
    };
    img.src = `data:image/jpeg;base64,${b64}`;
  }, []);

  // ── Coordinate translation ───────────────────────────────────────
  const translatePoint = useCallback((evt) => {
    const canv = canvasRef.current;
    if (!canv) return { x: 0, y: 0 };
    const rect = canv.getBoundingClientRect();
    const sx = remoteSize.current.w / rect.width;
    const sy = remoteSize.current.h / rect.height;
    return {
      x: (evt.clientX - rect.left) * sx,
      y: (evt.clientY - rect.top) * sy,
    };
  }, []);

  const send = useCallback((payload) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(payload));
  }, []);

  // ── Mouse handlers ───────────────────────────────────────────────
  const onMouseMove = useCallback((e) => {
    if (!drivingRef.current) return;
    const { x, y } = translatePoint(e);
    send({ type: "mouse", action: "mouseMoved", x, y });
  }, [send, translatePoint]);

  const onMouseDown = useCallback((e) => {
    if (!drivingRef.current) return;
    e.preventDefault();
    const { x, y } = translatePoint(e);
    const button = e.button === 2 ? "right" : (e.button === 1 ? "middle" : "left");
    send({ type: "mouse", action: "mousePressed", x, y, button, clicks: 1 });
  }, [send, translatePoint]);

  const onMouseUp = useCallback((e) => {
    if (!drivingRef.current) return;
    e.preventDefault();
    const { x, y } = translatePoint(e);
    const button = e.button === 2 ? "right" : (e.button === 1 ? "middle" : "left");
    send({ type: "mouse", action: "mouseReleased", x, y, button, clicks: 1 });
  }, [send, translatePoint]);

  const onWheel = useCallback((e) => {
    if (!drivingRef.current) return;
    e.preventDefault();
    const { x, y } = translatePoint(e);
    send({ type: "mouse", action: "mouseWheel", x, y, dx: e.deltaX, dy: e.deltaY });
  }, [send, translatePoint]);

  const onContextMenu = useCallback((e) => {
    if (drivingRef.current) e.preventDefault();
  }, []);

  // ── Keyboard handlers — only attached while driving ───────────────
  useEffect(() => {
    if (mode !== "drive") return undefined;
    const modifiers = (e) => (
      (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0)
    );
    const onKeyDown = (e) => {
      // Don't hijack the user's tab-out / DevTools shortcuts at the
      // browser level — only forward when the canvas (or its parent
      // panel) has focus.
      const canv = canvasRef.current;
      if (!canv || !document.activeElement) return;
      // The canvas is `tabIndex=0` so it receives focus on click; if
      // focus is in some other input we DON'T intercept.
      if (!canv.contains(document.activeElement) && document.activeElement !== canv) return;
      e.preventDefault();
      send({
        type: "key", action: "keyDown",
        key: e.key, code: e.code,
        text: e.key.length === 1 ? e.key : "",
        modifiers: modifiers(e),
      });
    };
    const onKeyUp = (e) => {
      const canv = canvasRef.current;
      if (!canv || !document.activeElement) return;
      if (!canv.contains(document.activeElement) && document.activeElement !== canv) return;
      e.preventDefault();
      send({
        type: "key", action: "keyUp",
        key: e.key, code: e.code, modifiers: modifiers(e),
      });
    };
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
    };
  }, [mode, send]);

  // ── Take / release control ───────────────────────────────────────
  const takeControl = () => {
    setMode("drive");
    send({ type: "control", action: "take" });
    canvasRef.current?.focus();
  };
  const releaseControl = () => {
    setMode("watch");
    send({ type: "control", action: "release" });
  };

  const saveCredentials = () => {
    setSaving(true);
    setSavedHost("");
    send({ type: "save_credentials", hostname: needsLoginFor || "" });
  };

  return (
    <div className="sp-glass-strong rounded-2xl overflow-hidden flex flex-col" data-testid="browser-stream">
      {/* Login-required banner — only visible when the agent has paused
          on a login wall. Tells the user exactly what to do. */}
      {needsLoginFor && (
        <div className="px-4 py-2 text-xs flex items-center gap-2 bg-orange-500/15 border-b border-orange-400/30 text-orange-100" data-testid="browser-needs-login-banner">
          <Lock className="w-3.5 h-3.5 shrink-0" />
          <span className="flex-1">
            <b>Login required for {needsLoginFor}.</b>{" "}
            Click <i>Take control</i>, sign in, then <i>Save login</i> so the agent can use this account on future missions.
          </span>
        </div>
      )}
      {/* Address bar / status */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-white/5 text-xs">
        <div className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-400" : "bg-amber-400 animate-pulse"}`} />
        <div className="flex-1 truncate sp-subtle">
          {connected ? (meta.url || "loading…") : "connecting…"}
        </div>
        {meta.url && (
          <a href={meta.url} target="_blank" rel="noopener noreferrer"
             className="sp-subtle hover:text-orange-300"
             title="Open in a real new tab">
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        )}
        {/* Save-login button — only meaningful while the user is driving
            AND on a real (non-blank) page. Hidden in watch mode. */}
        {mode === "drive" && meta.url && (
          <button type="button" onClick={saveCredentials} disabled={saving}
                  className="sp-btn sp-btn-ghost text-xs"
                  data-testid="browser-save-login"
                  title="Encrypt and save this login so the agent can re-use it on future missions">
            {saving
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <Save className="w-3.5 h-3.5" />}
            {savedHost ? `Saved · ${savedHost}` : "Save login"}
          </button>
        )}
        {mode === "watch" ? (
          <button type="button" onClick={takeControl}
                  className="sp-btn sp-btn-primary text-xs"
                  data-testid="browser-take-control">
            <Hand className="w-3.5 h-3.5" /> Take control
          </button>
        ) : (
          <button type="button" onClick={releaseControl}
                  className="sp-btn sp-btn-ghost text-xs"
                  data-testid="browser-release-control">
            <MousePointer2 className="w-3.5 h-3.5" /> Hand back to agent
          </button>
        )}
        {onClose && (
          <button type="button" onClick={onClose}
                  className="sp-btn sp-btn-ghost text-xs"
                  data-testid="browser-stream-close">
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      {/* Stream canvas */}
      <div className="relative bg-black/40">
        <canvas
          ref={canvasRef}
          tabIndex={0}
          onMouseMove={onMouseMove}
          onMouseDown={onMouseDown}
          onMouseUp={onMouseUp}
          onWheel={onWheel}
          onContextMenu={onContextMenu}
          className={`block w-full h-auto outline-none ${
            mode === "drive" ? "cursor-crosshair" : "cursor-default pointer-events-none"
          }`}
          data-testid="browser-stream-canvas"
        />
        {!connected && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/50 text-xs sp-subtle px-4 text-center" data-testid="browser-stream-overlay">
            <div className="flex flex-col items-center gap-2">
              <div className="flex items-center">
                <Loader2 className="w-4 h-4 animate-spin mr-2" />
                {closeInfo || "Connecting to remote browser…"}
              </div>
              {closeInfo && (
                <div className="text-[10px] sp-subtle opacity-70 max-w-xs">
                  If this keeps happening, close and re-open the live browser, or refresh the page.
                </div>
              )}
            </div>
          </div>
        )}
        {mode === "drive" && (
          <div className="absolute top-2 left-2 inline-flex items-center gap-1.5 px-2 py-1 rounded-full bg-orange-500/20 border border-orange-400/40 text-[10px] text-orange-200" data-testid="browser-driving-pill">
            <Lock className="w-3 h-3" /> You have control · agent paused
          </div>
        )}
      </div>
    </div>
  );
}
