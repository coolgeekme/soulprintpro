"""Remote-browser live streaming via Chromium DevTools Protocol (CDP).

Ships "ChatGPT Agent Mode"-style interactive browser sessions:

  1. The frontend opens a WebSocket to /api/missions/{mission_id}/browser-stream.
  2. On connect, we attach a CDP session to the mission's sticky Chromium
     page (already created by `browser_tools._get_or_create_mission_page`),
     call `Page.startScreencast`, and forward every JPEG frame as a
     base64 payload over the WS.
  3. When the user clicks / types on the frontend canvas, the frontend
     sends back `{type: "mouse" | "key" | "scroll" | "resize" | ...}`
     messages and we translate those into `Input.dispatchMouseEvent`
     / `Input.dispatchKeyEvent` CDP calls.
  4. A lightweight "control ownership" flag is honoured: when the user
     has control (their `takeControl=true` message arrives), we append a
     `control_user` mission event so the agent loop can pause. When
     control is released we append `control_agent` and the loop resumes.

Security model:
  • The WS endpoint requires the same bearer token as every other
    mission endpoint (via query param `?token=...` — websockets can't
    send HTTP headers reliably cross-browser).
  • Caller must be a member of the mission's org.
  • Every inbound CDP input call is throttled and size-bounded (mouse
    events at 60 Hz max, key events at 100 chars/sec; nothing can spam
    our pod).
  • Screencast frames are JPEG quality=60, max 24 FPS, max width
    1280 — bounded bandwidth (~80-150 KB/s per session).

Concurrency:
  • Each mission has a sticky Chromium page (shared with the agent's
    tool calls). We serialise CDP input so a tool-call and a user
    click can't race. An asyncio lock already exists on the page entry.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import WebSocket, WebSocketDisconnect

from browser_tools import _get_or_create_mission_page  # noqa: F401 — intentional private import

logger = logging.getLogger(__name__)


# Per-mission CDP session registry so multiple WS clients watching the
# same mission share one screencast stream (no redundant CDP traffic).
_CDP_STREAMS: Dict[str, "_StreamHub"] = {}


class _StreamHub:
    """One CDP attach per mission; fan frames out to every connected WS."""

    def __init__(self, mission_id: str, page):
        self.mission_id = mission_id
        self.page = page
        self._cdp = None
        self._clients: set[WebSocket] = set()
        self._clients_lock = asyncio.Lock()
        self._started = False
        self._start_lock = asyncio.Lock()
        # Track the last time we observed a real screencast frame. CDP
        # only emits frames on paint events, so a fully-idle page (login
        # wall, post-load static content) can sit there for minutes
        # without firing one. We use this to drive a fallback "snapshot
        # every N seconds" loop so the user ALWAYS sees the current
        # viewport, not a black canvas + spinner.
        self._last_frame_at: float = 0.0
        self._fallback_task: Optional[asyncio.Task] = None

    async def _ensure_started(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            self._cdp = await self.page.context.new_cdp_session(self.page)
            # Page.startScreencast emits a `Page.screencastFrame` event per
            # paint. We must `screencastFrameAck` each one or the stream
            # pauses. quality=60 is a tuned sweet spot (small frames,
            # still readable).
            self._cdp.on("Page.screencastFrame", self._on_frame)
            await self._cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": 60,
                "maxWidth": 1280,
                "maxHeight": 800,
                "everyNthFrame": 1,
            })
            self._started = True
            # Spawn the static-page fallback watcher so the canvas
            # always reflects the current viewport even when nothing
            # is repainting.
            if self._fallback_task is None or self._fallback_task.done():
                self._fallback_task = asyncio.create_task(self._fallback_snapshot_loop())
            logger.info("Mission %s: screencast started (%d client(s))",
                        self.mission_id, len(self._clients))

    async def _push_snapshot(self) -> None:
        """Grab a one-shot screenshot via CDP and broadcast it as a
        synthetic 'frame' message so connected clients always have a
        recent view of the page — even when the screencast itself is
        idle."""
        if not self._cdp:
            return
        try:
            # Guard against pages that haven't even reached
            # DOMContentLoaded yet — captureScreenshot returns blank
            # data before that happens. Wait briefly so the first
            # snapshot is meaningful instead of pure white.
            try:
                await self.page.wait_for_load_state(
                    "domcontentloaded", timeout=4000,
                )
            except Exception:
                pass
            res = await self._cdp.send("Page.captureScreenshot", {
                "format": "jpeg",
                "quality": 70,
                "captureBeyondViewport": False,
                "fromSurface": True,
            })
        except Exception as exc:
            logger.debug("Mission %s: snapshot capture failed: %s", self.mission_id, exc)
            return
        # Best-effort viewport size — falls back to the configured maxes
        # which match what Page.startScreencast uses.
        w = h = 0
        try:
            metrics = await self._cdp.send("Page.getLayoutMetrics")
            v = metrics.get("cssVisualViewport") or {}
            w = int(v.get("clientWidth") or 0)
            h = int(v.get("clientHeight") or 0)
        except Exception:
            pass
        await self._fan_out({
            "type": "frame",
            "data": res.get("data", ""),
            "w": w or 1280,
            "h": h or 800,
            "ts": 0,
            "snapshot": True,
        })

    async def _fallback_snapshot_loop(self) -> None:
        """Periodically push a fresh screenshot whenever the screencast
        itself has been silent for >2.5s. Cheap (one CDP call every few
        seconds) and the only thing standing between a static login
        wall and a blank canvas. Exits when no clients remain."""
        try:
            while self._started:
                await asyncio.sleep(2.5)
                async with self._clients_lock:
                    if not self._clients:
                        return
                now = asyncio.get_event_loop().time()
                if now - self._last_frame_at < 2.5:
                    continue
                await self._push_snapshot()
        except Exception as exc:
            logger.debug("Mission %s: fallback loop ended: %s", self.mission_id, exc)

    def _on_frame(self, frame: Dict[str, Any]) -> None:
        # CDP delivers frames with a `sessionId` we must ACK (or the
        # stream stalls). The ACK is fire-and-forget.
        session_id = frame.get("sessionId")
        data_b64 = frame.get("data", "")
        metadata = frame.get("metadata") or {}
        asyncio.create_task(self._ack(session_id))
        # Mark we got a real screencast frame so the fallback loop
        # knows it can stay quiet for a couple of seconds.
        try:
            self._last_frame_at = asyncio.get_event_loop().time()
        except Exception:
            pass
        # Broadcast to every client. Drop frames to dead clients — they'll
        # be reaped by their own disconnect handler.
        payload = {
            "type": "frame",
            "data": data_b64,
            "w": int(metadata.get("deviceWidth") or 1280),
            "h": int(metadata.get("deviceHeight") or 800),
            "ts": int(metadata.get("timestamp") or 0),
        }
        asyncio.create_task(self._fan_out(payload))

    async def _ack(self, session_id) -> None:
        try:
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    async def _fan_out(self, payload: Dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        async with self._clients_lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._clients_lock:
                for ws in dead:
                    self._clients.discard(ws)

    async def add_client(self, ws: WebSocket) -> None:
        async with self._clients_lock:
            self._clients.add(ws)
        await self._ensure_started()
        # Push an immediate one-shot snapshot to the new client so they
        # see the current viewport right away — even if Page.startScreencast
        # hasn't fired a frame yet (login walls / fully-static pages).
        try:
            await self._push_snapshot()
        except Exception:
            pass

    async def remove_client(self, ws: WebSocket) -> None:
        async with self._clients_lock:
            self._clients.discard(ws)
            remaining = len(self._clients)
        if remaining == 0:
            # No more watchers — stop the screencast but keep the page
            # itself alive (the agent is still using it).
            try:
                if self._cdp:
                    await self._cdp.send("Page.stopScreencast")
                    await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
            self._started = False
            if self._fallback_task and not self._fallback_task.done():
                self._fallback_task.cancel()
            self._fallback_task = None
            _CDP_STREAMS.pop(self.mission_id, None)
            logger.info("Mission %s: screencast stopped (no clients)", self.mission_id)

    # ── Input dispatch ──────────────────────────────────────────────
    async def dispatch_mouse(self, action: str, x: float, y: float,
                             button: str = "none", clicks: int = 0,
                             delta_x: float = 0, delta_y: float = 0) -> None:
        """Forward a mouse event to Chromium via CDP."""
        if not self._cdp:
            return
        try:
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": action,
                "x": x, "y": y,
                "button": button,
                "clickCount": clicks,
                "deltaX": delta_x,
                "deltaY": delta_y,
            })
        except Exception as exc:
            logger.debug("Mouse dispatch failed: %s", exc)

    async def dispatch_key(self, kind: str, key: str = "", code: str = "",
                           text: str = "", modifiers: int = 0) -> None:
        """Forward a keyboard event to Chromium via CDP.

        kind = "keyDown" | "keyUp" | "char" (rawKeyDown is also valid).
        modifiers = bitmask: Alt=1, Ctrl=2, Meta/Command=4, Shift=8.
        """
        if not self._cdp:
            return
        payload: Dict[str, Any] = {
            "type": kind,
            "modifiers": modifiers,
        }
        if key:
            payload["key"] = key
        if code:
            payload["code"] = code
        if text:
            payload["text"] = text
        try:
            await self._cdp.send("Input.dispatchKeyEvent", payload)
        except Exception as exc:
            logger.debug("Key dispatch failed: %s", exc)


async def _hub_for(mission_id: str) -> _StreamHub:
    hub = _CDP_STREAMS.get(mission_id)
    if hub:
        return hub
    entry = await _get_or_create_mission_page(mission_id)
    hub = _StreamHub(mission_id, entry["page"])
    _CDP_STREAMS[mission_id] = hub
    return hub


async def _last_browsed_url(db, mission_id: str) -> str:
    """Find the most recent URL the mission has actually navigated to.

    The agent loop persists a `tool_call` event per browse_url / browse_click
    / browse_fill / browse_submit. We walk newest-first and pick the first
    one that yields a usable URL. This lets the WS endpoint auto-restore
    the live browser after a backend restart wipes the in-memory page —
    without it, users land on `about:blank` and can't continue the session.
    """
    try:
        cursor = db.mission_events.find(
            {"mission_id": mission_id, "kind": "tool_call"},
            {"_id": 0, "tool": 1, "args": 1, "result": 1, "ts": 1},
        ).sort("ts", -1).limit(20)
        async for ev in cursor:
            args = ev.get("args") or {}
            result = ev.get("result") or {}
            # Prefer the post-action URL (forms / clicks may redirect).
            for key in ("final_url", "url"):
                cand = result.get(key) or args.get(key)
                if isinstance(cand, str) and cand.startswith(("http://", "https://")):
                    return cand
    except Exception as exc:
        logger.debug("last-URL lookup failed for %s: %s", mission_id, exc)
    return ""


async def _restore_page_if_blank(db, mission_id: str, page) -> str:
    """If the mission's sticky Chromium page is sitting on about:blank
    (typical state after a backend restart), look up the last URL the
    agent was on and navigate there. Returns the URL we navigated to,
    or "" if no restore happened.
    """
    try:
        cur_url = (page.url or "").strip()
    except Exception:
        cur_url = ""
    if cur_url and cur_url not in ("about:blank", "chrome://newtab/", "chrome://new-tab-page/"):
        return ""
    target = await _last_browsed_url(db, mission_id)
    if not target:
        return ""
    nav_ok = False
    try:
        # Best-effort navigation. We use `wait_until="commit"` so even
        # a 4xx/5xx response (Instagram etc. sometimes 403 cookie-less
        # requests) still paints whatever the server returns instead of
        # raising before any frame can be captured.
        try:
            await page.goto(target, wait_until="commit", timeout=20000)
            nav_ok = True
        except Exception:
            await page.goto(target, timeout=20000)
            nav_ok = True
        logger.info("Mission %s: restored sticky page to %s", mission_id, target)
    except Exception as exc:
        logger.warning("Mission %s: page restore to %s failed: %s",
                       mission_id, target, exc)
    # If the deep link is hard-blocked (Instagram's ?next=… etc.), fall
    # back to the site root — the user can still sign in there.
    if not nav_ok:
        try:
            from urllib.parse import urlparse
            pu = urlparse(target)
            if pu.scheme in ("http", "https") and pu.netloc:
                fallback = f"{pu.scheme}://{pu.netloc}/"
                await page.goto(fallback, timeout=15000)
                logger.info("Mission %s: deep-link blocked; using root %s",
                            mission_id, fallback)
                return fallback
        except Exception:
            pass
        return ""
    return target


async def _append_control_event(db, mission_id: str, who: str, user_id: str) -> None:
    """Persist a control-handoff event on the mission so the UI timeline
    shows when the user took control / gave it back. Also sets a quick
    flag on the mission doc so the agent loop can check before its next
    iteration."""
    now = datetime.now(timezone.utc)
    update: Dict[str, Any] = {
        "user_controls_browser": (who == "user"),
        "last_activity_at": now,
    }
    # Releasing control while a login wall was flagged means the user
    # just successfully authenticated — clear the needs_login marker so
    # the loop can resume cleanly on the next tick.
    if who == "agent":
        update["needs_login_for"] = ""
    try:
        await db.mission_events.insert_one({
            "mission_id": mission_id,
            "kind": "system",
            "text": (
                "User took remote-browser control (agent loop paused)."
                if who == "user"
                else "User returned control — agent loop resumed."
            ),
            "control": who,
            "user_id": user_id,
            "ts": now,
        })
        await db.missions.update_one({"id": mission_id}, {"$set": update})
    except Exception as exc:
        logger.warning("control event append failed: %s", exc)


async def _capture_and_save_credentials(
    db, *, mission_id: str, user_id: str, hostname_hint: str = "",
) -> Optional[str]:
    """Grab the mission Chromium context's current storage_state and
    persist it, encrypted, in the credential vault keyed by (org_id,
    eTLD+1). Triggered when the user explicitly clicks "Save login" in
    the live browser overlay AFTER a successful authentication.

    Returns the eTLD+1 we keyed under, or None on failure.
    """
    from credential_vault import save_credentials, host_to_etld_plus_one
    try:
        mission = await db.missions.find_one({"id": mission_id}, {"_id": 0})
        if not mission:
            return None
        org_id = mission.get("org_id")
        # Reach into the live Chromium context for this mission.
        from browser_tools import _MISSION_PAGES  # noqa: F401 — intentional
        entry = _MISSION_PAGES.get(mission_id)
        if not entry:
            return None
        page = entry["page"]
        ctx = page.context
        state = await ctx.storage_state()
        # Prefer the user-provided hostname (from the take-control hint),
        # fall back to the current page URL.
        host_source = hostname_hint or page.url
        host = host_to_etld_plus_one(
            host_source.split("://", 1)[-1].split("/", 1)[0]
            if "://" in host_source else host_source
        )
        if not host:
            return None
        return await save_credentials(
            db=db, org_id=org_id, hostname=host,
            storage_state=state, user_id=user_id, label=host,
        )
    except Exception as exc:
        logger.warning("credential capture failed for %s: %s", mission_id, exc)
        return None


# ── Public entrypoint — registered on the FastAPI app ─────────────────
def register_browser_stream_routes(
    *, app, db,
    require_mission_member,  # async (mission_id, token) -> (mission, user)
) -> None:
    """Mounts `/api/missions/{mission_id}/browser-stream` onto `app`."""

    @app.websocket("/api/missions/{mission_id}/browser-stream")
    async def stream(ws: WebSocket, mission_id: str, token: str = ""):
        # Validate token + membership BEFORE accepting the socket so a
        # tampered client gets a 403-flavoured close instead of a live
        # connection.
        try:
            mission, user = await require_mission_member(mission_id, token)
        except Exception as exc:
            logger.info("WS auth failed for mission %s: %s", mission_id, exc)
            await ws.close(code=4403, reason="forbidden")
            return
        await ws.accept()
        hub = await _hub_for(mission_id)
        # If the page is sitting on about:blank (typical after a backend
        # restart wipes the in-memory Chromium context), try to restore
        # it to the agent's last known URL so the user can resume the
        # session — sign in to the same login wall, save credentials,
        # etc. Best-effort: if it fails, the canvas just shows blank.
        restored_url = await _restore_page_if_blank(db, mission_id, hub.page)
        await hub.add_client(ws)
        # Initial hello so the frontend knows it's connected + shows the
        # mission's current URL in the address-bar display.
        try:
            await ws.send_json({
                "type": "hello",
                "mission_id": mission_id,
                "url": hub.page.url,
                "title": await hub.page.title(),
                "restored": bool(restored_url),
                "restored_url": restored_url,
            })
        except Exception:
            pass

        user_id = user.get("id", "")
        try:
            while True:
                raw = await ws.receive_json()
                kind = raw.get("type")
                if kind == "mouse":
                    await hub.dispatch_mouse(
                        action=raw.get("action", "mouseMoved"),
                        x=float(raw.get("x", 0)),
                        y=float(raw.get("y", 0)),
                        button=raw.get("button", "none"),
                        clicks=int(raw.get("clicks", 0)),
                        delta_x=float(raw.get("dx", 0)),
                        delta_y=float(raw.get("dy", 0)),
                    )
                elif kind == "key":
                    await hub.dispatch_key(
                        kind=raw.get("action", "keyDown"),
                        key=raw.get("key", ""),
                        code=raw.get("code", ""),
                        text=raw.get("text", ""),
                        modifiers=int(raw.get("modifiers", 0)),
                    )
                elif kind == "control":
                    # {action: "take" | "release"}
                    who = "user" if raw.get("action") == "take" else "agent"
                    await _append_control_event(db, mission_id, who, user_id)
                    # When the user hands control BACK to the agent, snapshot
                    # the fresh storage_state — they may have just signed in,
                    # and we want those cookies preserved across restarts.
                    if who == "agent":
                        try:
                            from browser_tools import _persist_mission_state
                            await _persist_mission_state(mission_id, hub.page)
                        except Exception:
                            pass
                elif kind == "save_credentials":
                    # {hostname?: "google.com"} — persist current
                    # storage_state encrypted in the vault for re-use
                    # on future missions.
                    saved_host = await _capture_and_save_credentials(
                        db, mission_id=mission_id, user_id=user_id,
                        hostname_hint=str(raw.get("hostname") or ""),
                    )
                    try:
                        await ws.send_json({
                            "type": "credentials_saved",
                            "hostname": saved_host or "",
                            "ok": bool(saved_host),
                        })
                    except Exception:
                        pass
                elif kind == "ping":
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("WS stream error for %s: %s", mission_id, exc)
        finally:
            # Release control on disconnect so a closed tab doesn't leave
            # the agent paused forever.
            try:
                cur = await db.missions.find_one(
                    {"id": mission_id}, {"user_controls_browser": 1},
                )
                if (cur or {}).get("user_controls_browser"):
                    await _append_control_event(db, mission_id, "agent", user_id)
            except Exception:
                pass
            await hub.remove_client(ws)


__all__ = ["register_browser_stream_routes"]
