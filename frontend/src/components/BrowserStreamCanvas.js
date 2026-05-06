import React, { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, MousePointer2, Hand, X, ExternalLink, Lock } from "lucide-react";

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
}) {
  const canvasRef = useRef(null);
  const wsRef = useRef(null);
  const [mode, setMode] = useState(initiallyDrive ? "drive" : "watch");
  const [connected, setConnected] = useState(false);
  const [meta, setMeta] = useState({ url: "", title: "" });
  const remoteSize = useRef({ w: 1280, h: 800 });

  // Track whether we've sent the "take" control message — survives
  // remounts so a quick toggle doesn't double-fire events.
  const drivingRef = useRef(initiallyDrive);
  useEffect(() => { drivingRef.current = mode === "drive"; }, [mode]);

  // ── Connect WS ────────────────────────────────────────────────────
  useEffect(() => {
    if (!missionId || !token) return undefined;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const apiBase = (process.env.REACT_APP_BACKEND_URL || "").replace(/^https?:/, `${proto}:`);
    const url = `${apiBase}/api/missions/${missionId}/browser-stream?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      // If the parent wanted to start in drive mode, send the take
      // control packet immediately so the agent loop pauses on
      // reconnect.
      if (drivingRef.current) {
        ws.send(JSON.stringify({ type: "control", action: "take" }));
      }
    };
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "frame") {
        paintFrame(msg.data, msg.w, msg.h);
      } else if (msg.type === "hello") {
        setMeta({ url: msg.url || "", title: msg.title || "" });
      }
    };

    return () => {
      try { ws.close(); } catch { /* ignore */ }
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

  return (
    <div className="sp-glass-strong rounded-2xl overflow-hidden flex flex-col" data-testid="browser-stream">
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
          <div className="absolute inset-0 flex items-center justify-center bg-black/50 text-xs sp-subtle">
            <Loader2 className="w-4 h-4 animate-spin mr-2" /> Connecting to remote browser…
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
