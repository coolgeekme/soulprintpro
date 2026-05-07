import React, { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, MousePointer2, Hand, X, ExternalLink, Lock, Save, RotateCcw } from "lucide-react";

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

  // Windows virtual-key codes for the special keys that CDP can't
  // interpret from `key`/`code` alone. Without these, ArrowLeft /
  // Backspace / Delete / Tab / Enter get silently dropped by Chromium.
  // Source: https://docs.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes
  const VK_CODES = {
    Backspace: 8,  Tab: 9,        Enter: 13,    Shift: 16,
    Control: 17,   Alt: 18,       Pause: 19,    CapsLock: 20,
    Escape: 27,    Space: 32,     PageUp: 33,   PageDown: 34,
    End: 35,       Home: 36,
    ArrowLeft: 37, ArrowUp: 38,   ArrowRight: 39, ArrowDown: 40,
    Insert: 45,    Delete: 46,
    Meta: 91,      ContextMenu: 93,
    F1: 112,  F2: 113,  F3: 114,  F4: 115,  F5: 116,  F6: 117,
    F7: 118,  F8: 119,  F9: 120,  F10: 121, F11: 122, F12: 123,
  };
  const vkCode = (e) => {
    if (VK_CODES[e.key] != null) return VK_CODES[e.key];
    if (e.key && e.key.length === 1) {
      const ch = e.key.toUpperCase();
      const cc = ch.charCodeAt(0);
      // ASCII letters / digits map cleanly to their Windows VK codes
      // (which deliberately mirror ASCII for these ranges).
      if ((cc >= 0x30 && cc <= 0x39) || (cc >= 0x41 && cc <= 0x5A)) return cc;
    }
    return 0;
  };

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
          setMeta({
            url: msg.url || "",
            title: msg.title || "",
            restored: Boolean(msg.restored),
            restoredUrl: msg.restored_url || "",
          });
        } else if (msg.type === "credentials_saved") {
          setSaving(false);
          if (msg.ok) setSavedHost(msg.hostname || "");
        } else if (msg.type === "reset_complete") {
          setResetting(false);
          // Backend confirms the new URL — keep the address bar in
          // sync without waiting for the next hello.
          if (msg.url) setMeta((m) => ({ ...m, url: msg.url, restored: false }));
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
  // ── Mouse handlers ───────────────────────────────────────────────
  // Throttle mouseMove forwarding to ~60 Hz (every 16ms). Without this
  // we flood the WS with hundreds of messages per second, which
  // backpressures the screencast frames and makes the whole UI feel
  // sluggish. 16ms is fine-grained enough for hover/drag UX.
  const lastMoveAt = useRef(0);
  const onMouseMove = useCallback((e) => {
    if (!drivingRef.current) return;
    const now = performance.now();
    if (now - lastMoveAt.current < 16) return;
    lastMoveAt.current = now;
    const { x, y } = translatePoint(e);
    send({ type: "mouse", action: "mouseMoved", x, y });
  }, [send, translatePoint]);

  // Visual feedback for clicks — paint an immediate fading ring at the
  // cursor location so the user gets sub-millisecond confirmation that
  // their click registered, without waiting for the screencast frame
  // to paint the focus ring back to them.
  const [clickRing, setClickRing] = useState(null);
  const showClickRing = useCallback((evt) => {
    const canv = canvasRef.current;
    if (!canv) return;
    const rect = canv.getBoundingClientRect();
    setClickRing({
      x: evt.clientX - rect.left,
      y: evt.clientY - rect.top,
      id: Math.random(),
    });
  }, []);
  useEffect(() => {
    if (!clickRing) return undefined;
    const t = setTimeout(() => setClickRing(null), 450);
    return () => clearTimeout(t);
  }, [clickRing]);

  const onMouseDown = useCallback((e) => {
    if (!drivingRef.current) return;
    e.preventDefault();
    canvasRef.current?.focus();
    showClickRing(e);
    const { x, y } = translatePoint(e);
    const button = e.button === 2 ? "right" : (e.button === 1 ? "middle" : "left");
    send({ type: "mouse", action: "mousePressed", x, y, button, clicks: 1 });
  }, [send, translatePoint, showClickRing]);

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
        // Single-character keys (letters/digits/punctuation) carry the
        // text payload. Special keys (Backspace, ArrowLeft, etc.)
        // leave text empty so Chromium dispatches a keydown without
        // synthesising a char event.
        text: e.key && e.key.length === 1 ? e.key : "",
        windowsVirtualKeyCode: vkCode(e),
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
        key: e.key, code: e.code,
        windowsVirtualKeyCode: vkCode(e),
        modifiers: modifiers(e),
      });
    };
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  // Hard-reset the live browser: wipes cookies / persisted state and
  // navigates to a fresh page. Use when a site has flagged the
  // session (suspicious-login challenge, rate limit, white page).
  // Confirms once because losing the typed-in state is annoying if
  // it's clicked by mistake.
  const [resetting, setResetting] = useState(false);
  const resetBrowser = () => {
    if (resetting) return;
    // Pre-fill with the hostname they were on — keeps them on the
    // same site without triggering an Instagram-style ?next=… deep
    // link that causes our 4xx fallback path.
    let target = "about:blank";
    try {
      if (meta.url) {
        const u = new URL(meta.url);
        target = `${u.protocol}//${u.host}/`;
      }
    } catch {  // eslint-disable-line no-empty
      // If meta.url isn't a valid URL we just fall back to about:blank.
    }
    if (!window.confirm(
      "Reset the live browser?\n\nThis clears cookies, saved sessions, " +
      "and any in-progress login. You'll start fresh — useful when a site " +
      "blocks the session or shows a checkpoint."
    )) return;
    setResetting(true);
    send({ type: "reset", url: target });
    // The handler in onmessage will flip resetting back to false on
    // `reset_complete`. As a safety net also clear after 8s.
    setTimeout(() => setResetting(false), 8000);
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
      {/* Session-restored banner — shown when the backend revived the
          sticky page from the last known URL after a deploy/restart.
          Without this banner the page change is silent and confusing. */}
      {meta.restored && meta.restoredUrl && (
        <div className="px-4 py-2 text-xs flex items-center gap-2 bg-blue-500/15 border-b border-blue-400/30 text-blue-100" data-testid="browser-restored-banner">
          <Lock className="w-3.5 h-3.5 shrink-0" />
          <span className="flex-1">
            Session restored to <b className="break-all">{meta.restoredUrl}</b>{" "}
            (the original browser was lost on a server restart).
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
        {/* Hard-reset — wipes cookies + persisted state. Lives next to
            Take/Release so users can find it when the session is stuck
            (white pages, checkpoint walls, rate-limited responses). */}
        {connected && (
          <button type="button" onClick={resetBrowser} disabled={resetting}
                  className="sp-btn sp-btn-ghost text-xs"
                  data-testid="browser-reset"
                  title="Clear cookies and start over (use when a site blocks the session)">
            {resetting
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <RotateCcw className="w-3.5 h-3.5" />}
            Reset
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
            mode === "drive" ? "cursor-text" : "cursor-default pointer-events-none"
          }`}
          data-testid="browser-stream-canvas"
        />
        {/* Instant click-ring — paints sub-millisecond local feedback so
            the user knows their click registered without waiting for
            the round-trip frame to come back. Pure CSS animation, no
            JS work after initial setState. */}
        {clickRing && (
          <div
            key={clickRing.id}
            className="absolute pointer-events-none"
            style={{
              left: clickRing.x - 14,
              top: clickRing.y - 14,
              width: 28, height: 28,
              borderRadius: "50%",
              border: "2px solid rgba(246,64,0,0.85)",
              animation: "spClickRing 450ms ease-out forwards",
            }}
          />
        )}
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
      {/* Scoped keyframes so we don't pollute index.css for one component. */}
      <style>{`
        @keyframes spClickRing {
          0%   { transform: scale(0.55); opacity: 1; }
          80%  { opacity: 0.7; }
          100% { transform: scale(1.4);  opacity: 0; }
        }
      `}</style>
    </div>
  );
}
