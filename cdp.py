"""Minimal Chrome DevTools Protocol client for one bound page target.

Allowlisted operations only: target listing, flattened attach, viewport metrics
and override, JPEG screencast with acknowledgement pacing, pointer/wheel/key/text
input and a fixed editable-at-point probe. No general evaluate, network, file or
navigation methods are reachable from a viewer message.

Screencast pacing and the probe script follow T3 Code's ServerBrowser.ts (MIT,
Copyright 2026 T3 Tools Inc.); see third_party/NOTICE.md.
"""
import asyncio
import base64
import json
import math
import time
from urllib.parse import urlsplit

import websockets
from websockets.asyncio.client import connect as ws_connect

MAX_MESSAGE = 24 * 1024 * 1024  # a 2x phone JPEG is far below this; Chrome frames are capped by maxWidth
MAX_TEXT = 2000
ACK_PACE_S = 0.03
PROBE_SCRIPT = """(function(x, y) {
  let element = document.elementFromPoint(x, y);
  while (element && element.shadowRoot) {
    const inner = element.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === element) break;
    element = inner;
  }
  if (element && element.tagName === "LABEL" && element.control) element = element.control;
  if (!element) return false;
  if (element.tagName === "IFRAME" || element.tagName === "FRAME") return true;
  if (element.isContentEditable) return true;
  if (element.tagName === "TEXTAREA") return !element.disabled && !element.readOnly;
  if (element.tagName !== "INPUT") return false;
  const nonText = ["button","checkbox","color","file","hidden","image","radio","range","reset","submit"];
  return !nonText.includes(element.type) && !element.disabled && !element.readOnly;
})(%d, %d)"""
LAYOUT_SCRIPT = "(function(){return {w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio};})()"


class CDPError(RuntimeError):
    pass


class TargetLost(CDPError):
    pass


class InvalidInput(ValueError):
    """A viewer message failed validation; it is ignored, never forwarded."""


def origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def _num(value, lo, hi, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        if default is None:
            raise InvalidInput("invalid number")
        return default
    return min(max(float(value), lo), hi)


def _int(value, lo, hi, default=0):
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, lo), hi)


def _short(value, limit=32):
    return value if isinstance(value, str) and len(value) <= limit and all(ord(c) >= 32 for c in value) else ""


async def resolve_ws_url(endpoint: str) -> str:
    if endpoint.startswith(("ws://", "wss://")):
        return endpoint
    import urllib.request

    def fetch():
        base = endpoint.rstrip("/")
        with urllib.request.urlopen(base + "/json/version", timeout=5) as resp:  # noqa: S310 loopback CDP only
            return json.loads(resp.read(65536).decode("utf-8"))
    data = await asyncio.to_thread(fetch)
    url = data.get("webSocketDebuggerUrl") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url.startswith(("ws://", "wss://")):
        raise CDPError("browser endpoint did not advertise a websocket")
    return url


class CDPConnection:
    """Browser-level websocket with request/response correlation and event fan-out."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._ws = None
        self._next = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list = []  # (method, session_id|None, callback)
        self._reader: asyncio.Task | None = None
        self.closed = asyncio.Event()

    async def connect(self, timeout=8.0):
        url = await resolve_ws_url(self.endpoint)
        try:
            self._ws = await asyncio.wait_for(ws_connect(url, max_size=MAX_MESSAGE, ping_interval=None,
                                                         open_timeout=timeout), timeout + 1)
        except Exception as exc:
            raise CDPError("could not connect to the browser") from exc
        self._reader = asyncio.create_task(self._read())

    @property
    def alive(self) -> bool:
        return self._ws is not None and not self.closed.is_set()

    async def _read(self):
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if "id" in message:
                    fut = self._pending.pop(message["id"], None)
                    if fut and not fut.done():
                        fut.set_result(message)
                    continue
                method = message.get("method")
                session = message.get("sessionId")
                params = message.get("params") or {}
                for want_method, want_session, callback in list(self._listeners):
                    if want_method == method and (want_session is None or want_session == session):
                        try:
                            result = callback(params, session)
                            if asyncio.iscoroutine(result):
                                asyncio.create_task(result)
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(TargetLost("browser connection closed"))
            self._pending.clear()

    def on(self, method: str, callback, session_id: str | None = None):
        entry = (method, session_id, callback)
        self._listeners.append(entry)
        return lambda: entry in self._listeners and self._listeners.remove(entry)

    async def send(self, method: str, params: dict | None = None, session_id: str | None = None, timeout=10.0):
        if not self.alive:
            raise TargetLost("browser connection closed")
        call_id = self._next
        self._next += 1
        message = {"id": call_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            await self._ws.send(json.dumps(message))
            response = await asyncio.wait_for(fut, timeout)
        except TargetLost:
            raise
        except asyncio.TimeoutError as exc:
            self._pending.pop(call_id, None)
            raise CDPError(f"{method} timed out") from exc
        except Exception as exc:
            self._pending.pop(call_id, None)
            raise TargetLost("browser connection failed") from exc
        if "error" in response:
            code = (response["error"] or {}).get("code")
            raise CDPError(f"{method} failed ({code})")
        return response.get("result") or {}

    async def list_pages(self) -> list[dict]:
        result = await self.send("Target.getTargets")
        pages = []
        for info in result.get("targetInfos", []):
            if info.get("type") == "page" and isinstance(info.get("targetId"), str):
                pages.append({"targetId": info["targetId"], "url": str(info.get("url") or ""),
                              "attached": bool(info.get("attached")), "openerId": info.get("openerId")})
        return pages

    async def close(self):
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._reader:
            self._reader.cancel()
        self.closed.set()


class PageSession:
    """One flattened session on one target. Lost targets raise TargetLost thereafter."""

    def __init__(self, conn: CDPConnection, target_id: str):
        self.conn = conn
        self.target_id = target_id
        self.session_id: str | None = None
        self.lost = asyncio.Event()
        self.on_navigated = None   # callback(origin: str|None)
        self.on_popup = None       # callback()
        self._frame_cb = None
        self._unsubscribe = []
        self.screencasting = False
        self.pressed_keys: dict[str, tuple[str, str]] = {}
        self.pressed_buttons: dict[str, tuple[float, float]] = {}
        self.main_frame_id = None
        self._frames_seen = 0
        self._still_quality = 70
        self._tasks: set = set()

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def attach(self):
        result = await self.conn.send("Target.attachToTarget", {"targetId": self.target_id, "flatten": True})
        self.session_id = result.get("sessionId")
        if not self.session_id:
            raise CDPError("attach returned no session")
        await self.conn.send("Page.enable", session_id=self.session_id)
        tree = await self.conn.send("Page.getFrameTree", session_id=self.session_id)
        self.main_frame_id = ((tree.get("frameTree") or {}).get("frame") or {}).get("id")
        await self.conn.send("Target.setDiscoverTargets", {"discover": True})
        self._unsubscribe.append(self.conn.on("Target.detachedFromTarget", self._detached))
        self._unsubscribe.append(self.conn.on("Target.targetDestroyed", self._destroyed))
        self._unsubscribe.append(self.conn.on("Target.targetCreated", self._created))
        self._unsubscribe.append(self.conn.on("Page.frameNavigated", self._navigated, self.session_id))
        self._unsubscribe.append(self.conn.on("Page.screencastFrame", self._frame, self.session_id))

    def _mark_lost(self):
        if not self.lost.is_set():
            self.lost.set()
            self.screencasting = False

    def _detached(self, params, _session):
        if params.get("sessionId") == self.session_id or params.get("targetId") == self.target_id:
            self._mark_lost()

    def _destroyed(self, params, _session):
        if params.get("targetId") == self.target_id:
            self._mark_lost()

    def _created(self, params, _session):
        info = params.get("targetInfo") or {}
        if info.get("openerId") == self.target_id and info.get("type") == "page" and self.on_popup:
            self.on_popup()

    def _navigated(self, params, _session):
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return
        self.main_frame_id = frame.get("id", self.main_frame_id)
        if self.on_navigated:
            self.on_navigated(origin_of(str(frame.get("url") or "")))

    def _check(self):
        if self.lost.is_set() or not self.conn.alive:
            raise TargetLost("the prepared page is gone")

    async def send(self, method, params=None, timeout=10.0):
        self._check()
        try:
            return await self.conn.send(method, params, session_id=self.session_id, timeout=timeout)
        except TargetLost:
            self._mark_lost()
            raise

    async def current_origin(self) -> str | None:
        result = await self.conn.send("Target.getTargetInfo", {"targetId": self.target_id})
        return origin_of(str((result.get("targetInfo") or {}).get("url") or ""))

    async def layout(self) -> dict:
        result = await self.send("Runtime.evaluate", {"expression": LAYOUT_SCRIPT, "returnByValue": True})
        value = (result.get("result") or {}).get("value")
        if not isinstance(value, dict):
            raise CDPError("layout unavailable")
        return {"width": int(_num(value.get("w"), 1, 10000)), "height": int(_num(value.get("h"), 1, 10000)),
                "dpr": float(_num(value.get("dpr"), 0.1, 8))}

    async def set_viewport(self, width: int, height: int):
        width = int(_num(width, 100, 3840))
        height = int(_num(height, 100, 2160))
        await self.send("Emulation.setDeviceMetricsOverride",
                        {"width": width, "height": height, "deviceScaleFactor": 0, "mobile": False})

    async def restore_viewport(self, prior: dict | None) -> bool:
        """Clear this session's override; re-apply the recorded size when the page differs."""
        await self.send("Emulation.clearDeviceMetricsOverride")
        if not prior:
            return True
        now = await self.layout()
        if now["width"] == prior["width"] and now["height"] == prior["height"]:
            return True
        await self.send("Emulation.setDeviceMetricsOverride",
                        {"width": prior["width"], "height": prior["height"], "deviceScaleFactor": 0, "mobile": False})
        now = await self.layout()
        return now["width"] == prior["width"] and now["height"] == prior["height"]

    # ── screencast ────────────────────────────────────────────────────────────

    async def start_screencast(self, max_width: int, max_height: int, quality: int, on_frame):
        """``on_frame(jpeg_bytes, ack)``: call ``await ack()`` once the frame left the socket."""
        self._frame_cb = on_frame
        self._frames_seen = 0
        self._still_quality = int(_num(quality, 20, 90))
        self.screencasting = True
        await self.send("Page.startScreencast", {
            "format": "jpeg", "quality": self._still_quality,
            "maxWidth": int(_num(max_width, 100, 1800)), "maxHeight": int(_num(max_height, 100, 2800)),
            "everyNthFrame": 1})
        self._spawn(self._still_if_idle())

    async def _still_if_idle(self, delay=0.7):
        """An idle page never repaints for a new screencast; start from a still (T3 does the same)."""
        await asyncio.sleep(delay)
        if self._frames_seen or not self.screencasting or self._frame_cb is None:
            return
        await self.push_still()

    async def push_still(self):
        callback = self._frame_cb
        if callback is None or self.lost.is_set():
            return
        try:
            result = await self.send("Page.captureScreenshot", {"format": "jpeg", "quality": self._still_quality}, timeout=8)
            jpeg = base64.b64decode(result.get("data", ""), validate=True)
        except (CDPError, ValueError, TypeError):
            return
        if jpeg and self._frame_cb is callback:
            async def no_ack():
                return None
            await callback(jpeg, no_ack)

    async def stop_screencast(self):
        self._frame_cb = None
        if self.screencasting and not self.lost.is_set() and self.conn.alive:
            self.screencasting = False
            try:
                await self.send("Page.stopScreencast", timeout=3)
            except CDPError:
                pass

    def _frame(self, params, _session):
        self._frames_seen = getattr(self, "_frames_seen", 0) + 1
        callback = self._frame_cb
        frame_session = params.get("sessionId")
        data = params.get("data")
        arrived = time.monotonic()

        async def ack():
            delay = ACK_PACE_S - (time.monotonic() - arrived)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self.send("Page.screencastFrameAck", {"sessionId": frame_session}, timeout=5)
            except CDPError:
                pass
        if callback is None or not isinstance(data, str):
            self._spawn(ack())
            return
        try:
            jpeg = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            self._spawn(ack())
            return
        self._spawn(callback(jpeg, ack))

    # ── input ─────────────────────────────────────────────────────────────────

    async def dispatch(self, message: dict):
        """Validated viewer input. Unknown or malformed messages are ignored, never forwarded."""
        try:
            return await self._dispatch(message)
        except InvalidInput:
            return None

    async def _dispatch(self, message: dict):
        kind = message.get("type")
        modifiers = _int(message.get("modifiers"), 0, 15)
        if kind == "mouse":
            action = message.get("action")
            cdp_type = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}.get(action)
            if not cdp_type:
                return
            button = message.get("button") if message.get("button") in ("none", "left", "middle", "right") else "none"
            x, y = _num(message.get("x"), 0, 10000), _num(message.get("y"), 0, 10000)
            await self.send("Input.dispatchMouseEvent", {
                "type": cdp_type, "x": x, "y": y, "button": button,
                "buttons": _int(message.get("buttons"), 0, 7),
                "clickCount": _int(message.get("clickCount"), 0, 3, 0 if cdp_type == "mouseMoved" else 1),
                "modifiers": modifiers})
            if button != "none":
                if action == "down":
                    self.pressed_buttons[button] = (x, y)
                elif action == "up":
                    self.pressed_buttons.pop(button, None)
        elif kind == "wheel":
            await self.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": _num(message.get("x"), 0, 10000), "y": _num(message.get("y"), 0, 10000),
                "deltaX": _num(message.get("deltaX"), -5000, 5000, 0.0),
                "deltaY": _num(message.get("deltaY"), -5000, 5000, 0.0), "modifiers": modifiers})
        elif kind == "key":
            key, code = _short(message.get("key")), _short(message.get("code"))
            if not key and not code:
                return
            if message.get("action") == "up":
                await self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code, "modifiers": modifiers})
                self.pressed_keys.pop(code or key, None)
                return
            text = message.get("text") if isinstance(message.get("text"), str) and 0 < len(message["text"]) <= 4 else None
            params = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": code, "modifiers": modifiers,
                      "windowsVirtualKeyCode": _int(message.get("keyCode"), 0, 255,
                                                    ord(key.upper()) if len(key) == 1 else 0)}
            if text:
                params["text"] = text
                params["unmodifiedText"] = text
            await self.send("Input.dispatchKeyEvent", params)
            self.pressed_keys[code or key] = (key, code)
        elif kind == "text":
            text = message.get("text")
            if isinstance(text, str) and text:
                await self.send("Input.insertText", {"text": text[:MAX_TEXT]})
        elif kind == "probe":
            x, y = _num(message.get("x"), 0, 10000), _num(message.get("y"), 0, 10000)
            result = await self.send("Runtime.evaluate", {"expression": PROBE_SCRIPT % (int(x), int(y)), "returnByValue": True})
            # Echo the viewer's own coordinates so it can match the answer to its tap.
            return {"type": "probe", "x": x, "y": y, "editable": (result.get("result") or {}).get("value") is True}
        return None

    async def release_input(self):
        """Lift anything the viewer still holds so the page is not stuck mid-gesture."""
        for key, code in list(self.pressed_keys.values()):
            try:
                await self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code}, timeout=3)
            except CDPError:
                pass
        self.pressed_keys.clear()
        for button, (x, y) in list(self.pressed_buttons.items()):
            try:
                await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": button, "x": x, "y": y,
                                                             "buttons": 0, "clickCount": 1}, timeout=3)
            except CDPError:
                pass
        self.pressed_buttons.clear()

    async def detach(self):
        await self.stop_screencast()
        for task in list(self._tasks):
            task.cancel()
        for unsubscribe in self._unsubscribe:
            try:
                unsubscribe()
            except Exception:
                pass
        if self.session_id and self.conn.alive and not self.lost.is_set():
            try:
                await self.conn.send("Target.detachFromTarget", {"sessionId": self.session_id}, timeout=3)
            except CDPError:
                pass
        self._mark_lost()


__all__ = ["CDPConnection", "PageSession", "CDPError", "TargetLost", "origin_of", "websockets"]
