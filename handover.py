"""Handover records, viewer attachment and the human-control state machine.

All state lives on one asyncio loop (owned by ``service.Service``). Tool handlers
reach it through ``run_coroutine_threadsafe``. A record passes through:

    prepared -> viewing -> human -> returned
                      \\-> human_disconnected -> human (reconnect) -> returned
                      \\-> target_lost (page gone while human holds) -> returned
                      \\-> superseded (another viewer took the native lease)

Nothing in here releases the native lease except an explicit, authenticated
``release`` from the owner's connected viewer. Disconnects, expiry, plugin unload
and errors keep the human fence in place.
"""
import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cdp import CDPConnection, CDPError, PageSession, TargetLost, origin_of
from .lease import LeaseUnavailable

log = logging.getLogger("homelab_browser_handover")

LINK_TTL = 20 * 60
RETAIN_TTL = 15 * 60
TICKET_TTL = 30
LEASE_CACHE_S = 0.25
CONTROL_RECHECK_S = 1.0
HELLO_TIMEOUT_S = 5
SEND_TIMEOUT_S = 6
MAX_NOTICES = 5
MAX_PURPOSE = 160
ACTIVE_STATES = ("prepared", "viewing", "human", "human_disconnected", "target_lost", "superseded")
HELD_STATES = ("human", "human_disconnected", "target_lost", "superseded")
TERMINAL_STATES = ("returned", "cancelled", "expired", "failed")
HANDOVER_ID_LEN = 12


class Refused(RuntimeError):
    pass


class PersistError(RuntimeError):
    """The held-record state file could not be written; takeovers must not proceed."""


class RestoreError(RuntimeError):
    """The state file exists but cannot be trusted; the service refuses to start."""


def validate_origin(value: str, *, allow_loopback_http: bool) -> str:
    if not isinstance(value, str):
        raise Refused("page_origin must be a string like https://shop.example.com")
    origin = origin_of(value.strip())
    if not origin or origin.rstrip("/") != value.strip().lower().rstrip("/"):
        raise Refused("page_origin must be exactly scheme://host[:port] with no path")
    if origin.startswith("http://"):
        host = origin[len("http://"):].split(":")[0]
        if not allow_loopback_http or host not in ("127.0.0.1", "localhost"):
            raise Refused("Only HTTPS pages can be handed over")
    return origin


def validate_purpose(value) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_PURPOSE or any(ord(c) < 32 for c in value):
        raise Refused(f"purpose must be 1-{MAX_PURPOSE} plain characters")
    return value.strip()


@dataclass
class Record:
    id: str
    task: str
    purpose: str
    origin: str
    target_id: str
    session_key: str
    connection_id: str
    cdp_endpoint: str              # private; persisted 0600 only
    viewer_id: str                 # private lease capability; persisted 0600 only
    state: str = "prepared"
    created: float = field(default_factory=time.time)
    since: float = field(default_factory=time.time)
    expires_at: float | None = None
    epoch: int | None = None
    prior_viewport: dict | None = None
    viewport_restored: bool | None = None
    current_origin: str | None = None
    released_by: str | None = None
    notices: list = field(default_factory=list)
    # runtime only
    conn: CDPConnection | None = None
    page: PageSession | None = None
    viewer: "ViewerLink | None" = None
    requested_size: dict | None = None
    frame_cap: dict = field(default_factory=lambda: {"width": 900, "height": 1800})

    PERSISTED = ("id", "task", "purpose", "origin", "target_id", "session_key", "connection_id", "cdp_endpoint",
                 "viewer_id", "state", "created", "since", "epoch", "prior_viewport", "viewport_restored",
                 "current_origin", "released_by", "notices")

    def persisted(self) -> dict:
        return {name: getattr(self, name) for name in self.PERSISTED}

    def notice(self, text: str):
        if text not in self.notices:
            self.notices = (self.notices + [text])[-MAX_NOTICES:]

    def transition(self, state: str, clock=time.time):
        if state != self.state:
            log.info("handover=%s state=%s->%s", self.id, self.state, state)
            self.state = state
            self.since = clock()


class ViewerLink:
    """One authenticated websocket viewer. Frames are written one at a time."""

    def __init__(self, ws, cookie_id: str):
        self.ws = ws
        self.cookie_id = cookie_id
        self.id = secrets.token_hex(8)
        self._frame_lock = asyncio.Lock()
        self.closed = False

    async def send_json(self, payload: dict) -> bool:
        if self.closed:
            return False
        try:
            await asyncio.wait_for(self.ws.send(json.dumps(payload)), SEND_TIMEOUT_S)
            return True
        except Exception:
            await self.close(1011, "send failed")
            return False

    async def send_frame(self, jpeg: bytes) -> bool:
        if self.closed:
            return False
        async with self._frame_lock:
            try:
                await asyncio.wait_for(self.ws.send(jpeg), SEND_TIMEOUT_S)
                return True
            except Exception:
                await self.close(1011, "slow viewer")
                return False

    async def close(self, code=1000, reason=""):
        if self.closed:
            return
        self.closed = True
        try:
            await asyncio.wait_for(self.ws.close(code, reason[:120]), 3)
        except Exception:
            pass


class HandoverManager:
    def __init__(self, *, lease, resolver, public_origin: str, state_path: Path, clock=time.time,
                 link_ttl=LINK_TTL, allow_loopback_http=False, quality=70):
        self.lease = lease
        self.resolver = resolver          # callable(task_id) -> SessionBinding, run in a thread
        self.public_origin = public_origin
        self.state_path = Path(state_path)
        self.clock = clock
        self.link_ttl = link_ttl
        self.allow_loopback_http = allow_loopback_http
        self.quality = quality
        self.records: dict[str, Record] = {}
        self.tickets: dict[str, dict] = {}
        self._lease_cache = (0.0, None)
        self._lease_inflight: asyncio.Future | None = None
        self._preparing = False           # reservation: one create() may be past the active check at a time
        self.persist_failures = 0
        self._housekeeper: asyncio.Task | None = None
        self.housekeeping_interval = 5.0
        self.generation = secrets.token_hex(4)

    # ── lease helpers ────────────────────────────────────────────────────────

    async def lease_view(self, fresh=False):
        now = time.monotonic()
        cached_at, view = self._lease_cache
        if not fresh and view is not None and now - cached_at < LEASE_CACHE_S:
            return view
        # Concurrent fresh readers (frames, heartbeat, input) share one in-flight read.
        if self._lease_inflight is not None:
            return await asyncio.shield(self._lease_inflight)
        self._lease_inflight = asyncio.ensure_future(asyncio.to_thread(self.lease.get))
        try:
            view = await asyncio.shield(self._lease_inflight)
        finally:
            self._lease_inflight = None
        self._lease_cache = (time.monotonic(), view)
        return view

    def _invalidate_lease_cache(self):
        self._lease_cache = (0.0, None)

    @staticmethod
    def our_control(record: Record, view) -> bool:
        return view.human and view.viewer_id == record.viewer_id and (record.epoch is None or view.epoch == record.epoch)

    def controller(self, record: Record, view) -> str:
        if not view.human:
            return "agent"
        return "you" if view.viewer_id == record.viewer_id else "other"

    # ── persistence ──────────────────────────────────────────────────────────

    def persist(self):
        """Write held records (0600). Raises PersistError; nothing sensitive is logged."""
        held = [r.persisted() for r in self.records.values() if r.state in HELD_STATES]
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.state_path.parent, 0o700)
            tmp = self.state_path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"generation": self.generation, "records": held}, fh)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.persist_failures += 1
            raise PersistError("handover state could not be persisted") from exc

    def _persist_quiet(self):
        """For state transitions that must proceed anyway (the fence is the lease, not this file)."""
        try:
            self.persist()
        except PersistError as exc:
            log.warning("handover state not persisted (%s)", exc.__cause__.__class__.__name__ if exc.__cause__ else "OSError")

    async def restore(self):
        """Rebuild held records. A missing file is a normal first start; an unreadable or
        malformed file fails closed (RestoreError) so a held lease is never silently forgotten."""
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise RestoreError("handover state file is unreadable") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise RestoreError("handover state file is corrupt") from exc
        if not isinstance(data, dict) or not isinstance(data.get("records", []), list):
            raise RestoreError("handover state file has an unexpected shape")
        restored = 0
        view = await self.lease_view(fresh=True)
        for item in data.get("records", []):
            try:
                record = Record(**{k: item[k] for k in Record.PERSISTED if k in item})
            except (TypeError, KeyError) as exc:
                raise RestoreError("handover state record is malformed") from exc
            if not record.id or not record.viewer_id:
                raise RestoreError("handover state record is incomplete")
            if self.our_control(record, view):
                record.transition("human_disconnected", self.clock)
                record.notice("The viewer restarted. Reopen your link to continue or hand back.")
            elif view.human:
                record.transition("superseded", self.clock)
            else:
                record.released_by = record.released_by or "external"
                record.viewport_restored = False if record.viewport_restored is None else record.viewport_restored
                record.transition("returned", self.clock)
            self.records[record.id] = record
            restored += 1
        self._persist_quiet()
        return restored

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._housekeeper is None:
            self._housekeeper = asyncio.create_task(self._housekeeping())

    async def _housekeeping(self):
        while True:
            try:
                await asyncio.sleep(self.housekeeping_interval)
                await self.sweep()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - type only; never a traceback or message
                log.warning("handover housekeeping failed (%s)", exc.__class__.__name__)

    async def sweep(self):
        now = self.clock()
        self.tickets = {k: v for k, v in self.tickets.items() if v["expires"] > now}
        for record in list(self.records.values()):
            if record.state in ("prepared", "viewing") and record.expires_at and now >= record.expires_at:
                view = await self.lease_view(fresh=True)
                if view.human:
                    continue  # never expire a page a human may be using; state will catch up below
                await self._finish(record, "expired")
            elif record.state == "human_disconnected":
                view = await self.lease_view(fresh=True)
                if not view.human:
                    record.released_by = "external"
                    record.viewport_restored = False
                    await self._finish(record, "returned")
                elif view.viewer_id != record.viewer_id:
                    record.transition("superseded", self.clock)
                    self._persist_quiet()
            elif record.state in TERMINAL_STATES and now - record.since > RETAIN_TTL:
                self.records.pop(record.id, None)

    async def unload(self):
        """Stop streaming and detach. The native lease is deliberately untouched."""
        if self._housekeeper:
            self._housekeeper.cancel()
            self._housekeeper = None
        for record in list(self.records.values()):
            if record.viewer:
                await record.viewer.send_json({"type": "closed", "reason": "viewer_restarting"})
                await record.viewer.close(1001, "viewer restarting")
                record.viewer = None
            if record.state == "human":
                record.transition("human_disconnected", self.clock)
            await self._drop_cdp(record)
        self._persist_quiet()

    async def _drop_cdp(self, record: Record):
        page, conn = record.page, record.conn
        record.page = record.conn = None
        if page:
            try:
                await page.detach()
            except Exception:
                pass
        if conn:
            await conn.close()

    async def _finish(self, record: Record, state: str):
        if record.viewer:
            await record.viewer.send_json(await self._state_message(record))
            await record.viewer.send_json({"type": "closed", "reason": state})
            await record.viewer.close(1000, state)
            record.viewer = None
        await self._drop_cdp(record)
        record.transition(state, self.clock)
        record.expires_at = None
        self._persist_quiet()

    # ── agent tools ──────────────────────────────────────────────────────────

    def _active(self) -> Record | None:
        return next((r for r in self.records.values() if r.state in ACTIVE_STATES), None)

    async def create(self, task_id: str, page_origin: str, purpose: str, target_id: str | None = None) -> dict:
        origin = validate_origin(page_origin, allow_loopback_http=self.allow_loopback_http)
        purpose = validate_purpose(purpose)
        if target_id is not None and (not isinstance(target_id, str) or not 1 <= len(target_id) <= 128
                                      or not target_id.replace("-", "").replace("_", "").isalnum()):
            raise Refused("target_id is malformed")
        task = task_id or "default"
        # The active check and the reservation happen without an await in between, so two
        # concurrent tool calls cannot both pass; the loser is refused immediately.
        active = self._active()
        if active:
            raise Refused(f"Handover {active.id} is still {active.state}. Finish or cancel it first.")
        if self._preparing:
            raise Refused("Another handover is being prepared. Wait for it to finish or fail.")
        self._preparing = True
        try:
            return await self._create_reserved(task, origin, purpose, target_id)
        finally:
            self._preparing = False

    async def _create_reserved(self, task: str, origin: str, purpose: str, target_id: str | None) -> dict:
        before = await self.lease_view(fresh=True)
        if before.human:
            raise Refused("A human already holds control of the browser. Ask them to hand back first.")
        try:
            binding = await asyncio.to_thread(self.resolver, task)
        except Refused:
            raise
        except Exception as exc:  # noqa: BLE001 - message/type never reach the model or the log
            raise Refused(str(exc) if exc.__class__.__name__ == "BindingRefused" else
                          "The prepared browser could not be bound.") from None
        bound_epoch = getattr(binding, "lease_epoch", None)
        if bound_epoch is not None and bound_epoch != before.epoch:
            raise Refused("Control of the browser changed while binding. Retry on the current page.")
        conn = CDPConnection(binding.cdp_endpoint)
        try:
            await conn.connect()
            pages = await conn.list_pages()
            candidates = [p for p in pages if origin_of(p["url"]) == origin]
            if target_id is not None:
                candidates = [p for p in candidates if p["targetId"] == target_id]
            if len(candidates) != 1:
                raise Refused("Exactly one open page on that origin is required. Missing or ambiguous pages are refused.")
            page = PageSession(conn, candidates[0]["targetId"])
            await page.attach()
            if await page.current_origin() != origin:
                raise Refused("The page changed while binding. Retry on the current page.")
            after = await self.lease_view(fresh=True)
            if after.human:
                raise Refused("A human took control while binding. Wait for hand back.")
            if after.epoch != before.epoch:
                raise Refused("Control of the browser changed while binding. Retry on the current page.")
        except Refused:
            await conn.close()
            raise
        except (CDPError, TargetLost):
            await conn.close()
            raise Refused("The prepared browser page could not be attached.") from None
        except BaseException:
            await conn.close()
            raise
        record = Record(id=secrets.token_hex(HANDOVER_ID_LEN // 2), task=task, purpose=purpose, origin=origin,
                        target_id=page.target_id, session_key=binding.session_key, connection_id=binding.connection_id,
                        cdp_endpoint=binding.cdp_endpoint, viewer_id="hbh:" + secrets.token_urlsafe(18),
                        created=self.clock(), since=self.clock(), expires_at=self.clock() + self.link_ttl,
                        current_origin=origin, conn=conn, page=page)
        self._wire_page(record)
        self.records[record.id] = record
        log.info("handover=%s prepared target=%s connection=%s", record.id, record.target_id, record.connection_id)
        return self.public(record) | {"link": f"{self.public_origin}/h/{record.id}"}

    def _wire_page(self, record: Record):
        page = record.page

        def navigated(origin):
            record.current_origin = origin
            if origin != record.origin:
                record.notice("The page moved to a different website.")
            if record.viewer:
                asyncio.create_task(record.viewer.send_json({"type": "origin", "origin": origin,
                                                             "notices": list(record.notices)}))

        def popup():
            record.notice("The page opened a new tab. This viewer stays on the original page.")
            if record.viewer:
                asyncio.create_task(record.viewer.send_json({"type": "notices", "notices": list(record.notices)}))
        page.on_navigated = navigated
        page.on_popup = popup

    def public(self, record: Record) -> dict:
        data = {"success": True, "handover_id": record.id, "state": record.state, "origin": record.origin,
                "current_origin": record.current_origin, "target_id": record.target_id,
                "connection_id": record.connection_id, "purpose": record.purpose,
                "human_connected": record.viewer is not None, "since": record.since,
                "viewport_restored": record.viewport_restored, "released_by": record.released_by,
                "notices": list(record.notices), "page_alive": bool(record.page and not record.page.lost.is_set())}
        if record.expires_at and record.state in ("prepared", "viewing"):
            data["expires_in_seconds"] = max(0, int(record.expires_at - self.clock()))
        return data

    async def status(self, handover_id: str, task_id: str, cancel=False) -> dict:
        record = self.records.get(handover_id) if isinstance(handover_id, str) else None
        if not record or record.task != (task_id or "default"):
            raise Refused("Handover unavailable for this task.")
        view = await self.lease_view(fresh=True)
        if record.state in HELD_STATES and not view.human:
            record.released_by = record.released_by or "external"
            record.viewport_restored = False if record.viewport_restored is None else record.viewport_restored
            await self._finish(record, "returned")
        elif record.state in HELD_STATES and view.viewer_id != record.viewer_id and record.state != "superseded":
            record.transition("superseded", self.clock)
            self._persist_quiet()
        if cancel:
            if view.human:
                raise Refused("A human holds control. Cancellation is refused until they hand back from the page.")
            if record.state in ("prepared", "viewing"):
                await self._finish(record, "cancelled")
        data = self.public(record)
        data["controller"] = "agent" if not view.human else ("human" if view.viewer_id == record.viewer_id else "human_other")
        data["agent_may_continue"] = record.state in TERMINAL_STATES and not view.human
        return data

    # ── viewer HTTP support ──────────────────────────────────────────────────

    def page_info(self, handover_id: str) -> dict | None:
        record = self.records.get(handover_id)
        if not record:
            return None
        return {"id": record.id, "state": record.state, "purpose": record.purpose, "origin": record.origin,
                "active": record.state in ACTIVE_STATES}

    def mint_ticket(self, handover_id: str, cookie_id: str) -> str:
        record = self.records.get(handover_id)
        if not record or record.state not in ACTIVE_STATES:
            raise Refused("This handover is no longer active.")
        self.tickets = {k: v for k, v in self.tickets.items() if v["expires"] > self.clock() and v["cookie"] != cookie_id}
        ticket = secrets.token_urlsafe(32)
        self.tickets[ticket] = {"handover": handover_id, "cookie": cookie_id, "expires": self.clock() + TICKET_TTL}
        return ticket

    def consume_ticket(self, ticket: str, handover_id: str, cookie_id: str) -> bool:
        entry = self.tickets.pop(ticket, None) if isinstance(ticket, str) else None
        return bool(entry and entry["handover"] == handover_id and entry["cookie"] == cookie_id
                    and entry["expires"] > self.clock())

    # ── viewer websocket ─────────────────────────────────────────────────────

    async def _state_message(self, record: Record, view=None) -> dict:
        view = view or await self.lease_view()
        viewport = None
        if record.page and not record.page.lost.is_set():
            try:
                layout = await record.page.layout()
                viewport = {"width": layout["width"], "height": layout["height"]}
            except (CDPError, TargetLost):
                viewport = None
        return {"type": "state", "state": record.state, "controller": self.controller(record, view),
                "origin": record.current_origin or record.origin, "purpose": record.purpose,
                "viewport": viewport, "notices": list(record.notices),
                "page_alive": bool(record.page and not record.page.lost.is_set())}

    async def _ensure_page(self, record: Record) -> bool:
        if record.page and not record.page.lost.is_set() and record.conn and record.conn.alive:
            return True
        await self._drop_cdp(record)
        conn = CDPConnection(record.cdp_endpoint)
        try:
            await conn.connect()
            pages = await conn.list_pages()
            if not any(p["targetId"] == record.target_id for p in pages):
                raise TargetLost("target missing")
            page = PageSession(conn, record.target_id)
            await page.attach()
            record.conn, record.page = conn, page
            self._wire_page(record)
            return True
        except (CDPError, TargetLost, OSError):
            await conn.close()
            return False

    async def attach_viewer(self, handover_id: str, ticket: str, cookie_id: str, ws):
        if not self.consume_ticket(ticket, handover_id, cookie_id):
            await ws.close(4401, "ticket rejected")
            return
        record = self.records.get(handover_id)
        link = ViewerLink(ws, cookie_id)
        if not record or record.state not in ACTIVE_STATES:
            await link.send_json({"type": "closed", "reason": "finished"})
            await link.close(1000, "finished")
            return
        if record.viewer:
            previous, record.viewer = record.viewer, None
            await previous.send_json({"type": "closed", "reason": "replaced"})
            await previous.close(1000, "replaced")
        record.viewer = link
        watcher = asyncio.create_task(self._watch_control(record, link))
        try:
            await self._viewer_session(record, link)
        except Exception as exc:  # noqa: BLE001 - closed sockets are routine; never let them touch the lease
            if exc.__class__.__name__ not in ("ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError"):
                # Type only: a message or traceback could carry an endpoint or viewer text.
                log.warning("handover=%s viewer session failed (%s)", record.id, exc.__class__.__name__)
        finally:
            watcher.cancel()
            if record.viewer is link:
                record.viewer = None
                await self._on_viewer_gone(record)

    async def expire_viewer(self, handover_id: str, ws, reason: str = "session_expired"):
        """The listener's auth re-check failed for this socket: stop frames and input now.
        The lease is untouched; the owner recovers by reopening the link (identity re-checked)."""
        record = self.records.get(handover_id) if isinstance(handover_id, str) else None
        link = record.viewer if record else None
        if not link or link.ws is not ws:
            return
        record.viewer = None
        # Complete the held-state transition and input cleanup before telling the
        # client it is closed. A status read after close must see the stable state.
        await self._on_viewer_gone(record)
        log.info("handover=%s viewer=%s auth expired; socket closed, fence unchanged", record.id, link.id[:8])
        await link.send_json({"type": "closed", "reason": reason})
        await link.close(4401, reason)

    async def _watch_control(self, record: Record, link: ViewerLink):
        """Heartbeat: a Desktop-pane takeover or external hand back with no phone input must still
        stop frames and update the viewer. Fresh lease read every CONTROL_RECHECK_S."""
        try:
            while record.viewer is link and not link.closed:
                await asyncio.sleep(CONTROL_RECHECK_S)
                if record.viewer is not link or link.closed or record.state in TERMINAL_STATES:
                    return
                await self._control_check(record, link)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("handover=%s control watch failed (%s)", record.id, exc.__class__.__name__)

    async def _control_check(self, record: Record, link: ViewerLink, view=None) -> bool:
        """Fresh ownership check shared by frames, heartbeat and input. Returns True when frames may
        flow to this link: agent holds (preview) or this handover holds with the recorded epoch."""
        view = view or await self.lease_view(fresh=True)
        if record.viewer is not link or link.closed:
            return False
        if record.state == "human":
            if not self.our_control(record, view):
                await self._lost_control(record, link, view)
                return False
            return True
        if record.state in ("prepared", "viewing"):
            if view.human:
                if record.page and record.page.screencasting:
                    await record.page.stop_screencast()
                    record.notice("Someone else is controlling the browser screen; the preview is paused.")
                    await link.send_json(await self._state_message(record, view))
                return False
            if record.page and not record.page.lost.is_set() and not record.page.screencasting:
                await self._refresh_stream(record, link, view)
            return True
        if record.state == "superseded" and not view.human:
            record.released_by = "external"
            record.viewport_restored = False if record.viewport_restored is None else record.viewport_restored
            await self._finish(record, "returned")
        return False

    async def _on_viewer_gone(self, record: Record):
        if record.page:
            await record.page.stop_screencast()
        view = await self.lease_view(fresh=True)
        if record.state == "human":
            if self.our_control(record, view) and record.page and not record.page.lost.is_set():
                try:
                    await record.page.release_input()
                except (CDPError, TargetLost):
                    pass
            record.transition("human_disconnected", self.clock)
            self._persist_quiet()
        elif record.state == "viewing":
            record.transition("prepared", self.clock)

    async def _viewer_session(self, record: Record, link: ViewerLink):
        view = await self.lease_view(fresh=True)
        if record.state in HELD_STATES and not view.human:
            record.released_by = record.released_by or "external"
            record.viewport_restored = False if record.viewport_restored is None else record.viewport_restored
            await self._finish(record, "returned")
            return
        page_ok = await self._ensure_page(record)
        if not page_ok:
            if self.our_control(record, view) or record.state in HELD_STATES:
                record.transition("target_lost", self.clock)
                record.notice("The prepared page is no longer reachable. Hand back when you are done.")
                self._persist_quiet()
            else:
                await self._finish(record, "failed")
                return
        if record.state == "prepared":
            record.transition("viewing", self.clock)
        elif record.state == "human_disconnected" and self.our_control(record, view) and page_ok:
            record.transition("human", self.clock)
            self._persist_quiet()
        elif record.state in ("human", "human_disconnected") and view.human and view.viewer_id != record.viewer_id:
            record.transition("superseded", self.clock)
            self._persist_quiet()

        hello = None
        try:
            raw = await asyncio.wait_for(link.ws.recv(), HELLO_TIMEOUT_S)
            hello = self._parse(raw)
        except Exception:
            hello = None
        if hello and hello.get("type") == "hello":
            self._apply_hello(record, hello)
        if not await link.send_json(await self._state_message(record, view)):
            return
        await self._refresh_stream(record, link, view)
        if record.state == "human" and record.requested_size and page_ok:
            await self._apply_size(record)
        async for raw in link.ws:
            if link.closed or record.viewer is not link:
                return
            message = self._parse(raw)
            if not message:
                continue
            try:
                await self._handle(record, link, message)
            except (CDPError, TargetLost):
                await self._page_lost(record, link)
            except LeaseUnavailable:
                log.warning("handover=%s lease operation failed; keeping current fence", record.id)
                await link.send_json({"type": "error", "message": "Control change failed. Nothing was changed."})
            if record.state in TERMINAL_STATES:
                return

    @staticmethod
    def _parse(raw):
        if not isinstance(raw, str) or len(raw) > 4096:
            return None
        try:
            message = json.loads(raw)
        except ValueError:
            return None
        return message if isinstance(message, dict) and isinstance(message.get("type"), str) else None

    def _apply_hello(self, record: Record, hello: dict):
        cap_w, cap_h = hello.get("maxWidth"), hello.get("maxHeight")
        if isinstance(cap_w, (int, float)) and isinstance(cap_h, (int, float)) and cap_w >= 100 and cap_h >= 100:
            record.frame_cap = {"width": int(min(cap_w, 1800)), "height": int(min(cap_h, 2800))}
        self._remember_size(record, hello)

    @staticmethod
    def _remember_size(record: Record, message: dict):
        w, h = message.get("width"), message.get("height")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)) and 100 <= w <= 3840 and 100 <= h <= 2160:
            record.requested_size = {"width": int(round(w)), "height": int(round(h))}

    async def _apply_size(self, record: Record):
        if record.page and record.requested_size and not record.page.lost.is_set():
            await record.page.set_viewport(record.requested_size["width"], record.requested_size["height"])
            if record.viewer:
                await record.viewer.send_json({"type": "viewport", **record.requested_size})
            # The relayout may not repaint; a still shows the new layout immediately.
            if record.page.screencasting:
                await record.page.push_still()

    async def _refresh_stream(self, record: Record, link: ViewerLink, view):
        """Frames flow only to the owner while the agent holds or while this handover holds."""
        page = record.page
        if not page or page.lost.is_set():
            return
        allowed = (not view.human) or self.our_control(record, view)
        if allowed and not page.screencasting:
            async def on_frame(jpeg, ack):
                # Every frame re-checks ownership with a fresh (coalesced) lease read, so a takeover
                # from the Desktop pane while the phone is idle never leaks the new controller's screen.
                try:
                    if record.viewer is link and not link.closed and await self._control_check(record, link):
                        await link.send_frame(jpeg)
                finally:
                    await ack()
            try:
                await page.start_screencast(record.frame_cap["width"], record.frame_cap["height"], self.quality, on_frame)
            except (CDPError, TargetLost):
                await self._page_lost(record, link)
        elif not allowed and page.screencasting:
            await page.stop_screencast()

    async def _page_lost(self, record: Record, link: ViewerLink):
        view = await self.lease_view(fresh=True)
        await self._drop_cdp(record)
        if self.our_control(record, view) or record.state in HELD_STATES:
            record.transition("target_lost", self.clock)
            record.notice("The prepared page is no longer reachable. Hand back when you are done.")
            self._persist_quiet()
            await link.send_json(await self._state_message(record, view))
        else:
            await self._finish(record, "failed")

    async def _handle(self, record: Record, link: ViewerLink, message: dict):
        kind = message["type"]
        if kind == "ping":
            await link.send_json({"type": "pong"})
            return
        if kind == "hello":
            self._apply_hello(record, message)
            return
        if kind == "take":
            await self._take(record, link)
            return
        if kind == "release":
            await self._release(record, link)
            return
        if kind == "resize":
            self._remember_size(record, message)
            if record.state == "human" and self.our_control(record, await self.lease_view(fresh=True)):
                await self._apply_size(record)
            return
        if kind in ("mouse", "wheel", "key", "text", "probe"):
            if record.state != "human" or record.viewer is not link or not record.page:
                return
            # Fresh reads before and after: the cached view could be stale on both sides of the await.
            view = await self.lease_view(fresh=True)
            if not self.our_control(record, view):
                await self._lost_control(record, link, view)
                return
            epoch = view.epoch
            result = await record.page.dispatch(message)
            view = await self.lease_view(fresh=True)
            if not self.our_control(record, view) or view.epoch != epoch:
                # Control moved mid-flight: the result belongs to whoever holds now, so it is discarded.
                await self._lost_control(record, link, view)
                return
            if result:
                await link.send_json(result)

    async def _lost_control(self, record: Record, link: ViewerLink, view):
        view = await self.lease_view(fresh=True)
        if self.our_control(record, view):
            return
        if not view.human:
            record.released_by = "external"
            record.viewport_restored = False
            await self._finish(record, "returned")
            return
        record.transition("superseded", self.clock)
        record.notice("Another viewer took control of the browser screen.")
        self._persist_quiet()
        if record.page:
            await record.page.stop_screencast()
        await link.send_json(await self._state_message(record, view))

    async def _take(self, record: Record, link: ViewerLink):
        if record.viewer is not link:
            return
        view = await self.lease_view(fresh=True)
        if record.state != "viewing" or not record.page or record.page.lost.is_set():
            await link.send_json({"type": "error", "message": "This page cannot be controlled right now."})
            return
        if view.human:
            await link.send_json({"type": "error", "message": "Someone else already has control."})
            return
        # Fail closed: a takeover whose record cannot be persisted would not survive a reload, so the
        # owner could lose the hand-back path. Probe the state file before touching the lease.
        try:
            self.persist()
        except PersistError:
            await link.send_json({"type": "error", "message": "Control could not be taken: viewer state cannot be saved."})
            return
        self._invalidate_lease_cache()
        acquired = await asyncio.to_thread(self.lease.acquire, record.viewer_id, f"Phone handover: {record.purpose}"[:200])
        self._lease_cache = (time.monotonic(), acquired)
        if not (acquired.human and acquired.viewer_id == record.viewer_id):
            await link.send_json({"type": "error", "message": "Control could not be taken."})
            return
        record.epoch = acquired.epoch
        try:
            record.prior_viewport = await record.page.layout()
        except (CDPError, TargetLost):
            record.prior_viewport = None
        record.viewport_restored = None
        record.released_by = None
        record.transition("human", self.clock)
        try:
            self.persist()
        except PersistError:
            # Our own fresh lease: give it straight back rather than hold an unrecorded fence.
            record.transition("viewing", self.clock)
            record.epoch = None
            self._invalidate_lease_cache()
            released = await asyncio.to_thread(self.lease.release, record.viewer_id)
            self._lease_cache = (time.monotonic(), released)
            if released.human:
                record.transition("human", self.clock)   # release failed: keep the truthful held state
                record.epoch = acquired.epoch
            await link.send_json({"type": "error", "message": "Control could not be taken: viewer state cannot be saved."})
            return
        try:
            await self._apply_size(record)
        except (CDPError, TargetLost):
            await self._page_lost(record, link)
            return
        await link.send_json(await self._state_message(record, acquired))
        await self._refresh_stream(record, link, acquired)

    async def _release(self, record: Record, link: ViewerLink):
        if record.viewer is not link or record.state not in ("human", "target_lost", "human_disconnected", "superseded"):
            await link.send_json({"type": "error", "message": "You do not hold control."})
            return
        view = await self.lease_view(fresh=True)
        if not view.human:
            record.released_by = "external"
            record.viewport_restored = False if record.viewport_restored is None else record.viewport_restored
            await self._finish(record, "returned")
            return
        if view.viewer_id != record.viewer_id:
            await link.send_json({"type": "error", "message": "Another viewer holds control; they must hand back."})
            return
        restored = False
        if record.page and not record.page.lost.is_set():
            try:
                await record.page.stop_screencast()
                await record.page.release_input()
                restored = await record.page.restore_viewport(record.prior_viewport)
            except (CDPError, TargetLost):
                restored = False
        record.viewport_restored = restored
        self._invalidate_lease_cache()
        released = await asyncio.to_thread(self.lease.release, record.viewer_id)
        self._lease_cache = (time.monotonic(), released)
        if released.human:
            # Still human: either the lease moved to someone else or the write failed. Keep the fence.
            record.viewport_restored = None
            await link.send_json({"type": "error", "message": "Hand back did not complete. Control is unchanged."})
            return
        record.released_by = "owner"
        await self._finish(record, "returned")
