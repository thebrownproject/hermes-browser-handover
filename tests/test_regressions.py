"""Regression suite for the 18/10/2026 review findings (real listener, temp Chromium, file lease).

Run:  /tmp/hbh-venv/bin/python -m unittest test_regressions -v   (from this directory)
Covers: open-socket auth expiry keeps the fence; atomic handover reservation; frame ownership on
external takeover with no phone input; fresh lease checks around awaited input; service start/stop
races; sanitized logs and tool errors; binding epoch checks; fail-closed persist/restore.
"""
import asyncio
import json
import logging
import socket
import threading
import time
import unittest
from unittest.mock import patch

from support import LOGIN, CDPProbe, Env, Viewer, free_port, run

import hbh
import hbh.config
import hbh.lease
import hbh.server
from hbh.binding import SessionBinding
from hbh.config import ViewerConfig
from hbh.handover import HandoverManager, Refused
from hbh.lease import FileLease
from hbh.service import Service

logging.getLogger("websockets").setLevel(logging.WARNING)
SENTINEL = "SENTINEL-SECRET-7f3a"
SENTINEL_ENDPOINT = f"ws://127.0.0.1:1/devtools/browser/{SENTINEL}"


class CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class ChromiumSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = CaptureLogs()
        logger = logging.getLogger("homelab_browser_handover")
        logger.addHandler(cls.logs)
        logger.setLevel(logging.DEBUG)

    def setUp(self):
        self.now = [time.time()]
        self.env = Env(clock=lambda: self.now[0])
        self.probe = CDPProbe(self.env.ws_url)
        self.logs.lines.clear()

    def tearDown(self):
        self.env.close()

    def prepared(self):
        result = self.env.create()
        self.assertTrue(result["success"])
        return result

    async def connected(self, hid):
        cookie, csrf, ticket = self.env.session(hid)
        viewer = await Viewer(self.env, hid, cookie, ticket).connect()
        viewer.cookie_csrf = (cookie, csrf)
        await viewer.wait_for("state")
        await viewer.wait_frames(1)
        return viewer

    async def controlling(self, hid):
        viewer = await self.connected(hid)
        await viewer.send({"type": "take"})
        state = await viewer.wait_for("state", predicate=lambda m: m["controller"] == "you")
        self.assertEqual(state["state"], "human")
        self.assertTrue(self.env.lease.get().human)
        return viewer

    async def drain(self, viewer, seconds):
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                await viewer.recv(remaining)
            except (TimeoutError, asyncio.TimeoutError):
                return
            except Exception:
                return

    def screencasting(self, hid):
        async def q(m):
            record = m.records[hid]
            return bool(record.page and record.page.screencasting)
        return self.env.call(q)

    # ── 1. open socket auth expiry ───────────────────────────────────────────

    def test_open_socket_expires_with_cookie_fence_kept_and_fresh_identity_recovers(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]
        with patch.object(hbh.server, "AUTH_RECHECK_S", 0.2):
            async def go():
                viewer = await self.controlling(hid)
                await viewer.wait_frames_since(0)
                old_cookie, old_csrf = viewer.cookie_csrf
                self.now[0] += 13 * 3600                       # past COOKIE_TTL while the socket is open
                closed = await viewer.wait_for("closed", timeout=5)
                self.assertEqual(closed["reason"], "session_expired")
                await viewer.ws.wait_closed()
                self.assertEqual(viewer.ws.close_code, 4401)
                with self.assertRaises(Exception):
                    await viewer.send({"type": "text", "text": "LATE"})
                return old_cookie, old_csrf
            old_cookie, old_csrf = run(go())
        self.assertTrue(self.env.lease.get().human, "auth expiry must never release the human fence")
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["controller"], status["human_connected"]),
                         ("human_disconnected", "human", False))
        self.assertFalse(self.screencasting(hid), "frames stop when the socket loses authentication")
        self.assertEqual(self.env.ticket(hid, old_cookie, old_csrf)["status"], 403, "expired cookie cannot mint tickets")
        self.assertNotIn("LATE", run(self.probe.eval("document.body.innerText", target)))

        async def recover():
            cookie, csrf, ticket = self.env.session(hid)               # fresh identity-checked page visit
            self.assertNotEqual(cookie, old_cookie)
            again = await Viewer(self.env, hid, cookie, ticket).connect()
            state = await again.wait_for("state")
            self.assertEqual((state["state"], state["controller"]), ("human", "you"))
            await again.wait_frames_since(0)
            await again.send({"type": "release"})
            self.assertEqual((await again.wait_for("closed"))["reason"], "returned")
        run(recover())
        self.assertFalse(self.env.lease.get().human)
        self.assertEqual(self.env.status(hid)["released_by"], "owner")

    # ── 2. atomic reservation ────────────────────────────────────────────────

    def test_concurrent_creates_yield_exactly_one_active_and_clear_reservation(self):
        gate = threading.Event()
        resolver_calls = []
        binding = self.env.binding

        def slow_resolver(task):
            resolver_calls.append(task)
            gate.wait(5)
            return binding

        async def set_resolver(m, fn=slow_resolver):
            m.resolver = fn
            return True
        self.env.call(set_resolver)
        threading.Timer(0.4, gate.set).start()

        async def both(m):
            return await asyncio.gather(m.create("task-1", self.env.public_origin, "first", None),
                                        m.create("task-1", self.env.public_origin, "second", None),
                                        return_exceptions=True)
        results = self.env.call(both)
        successes = [r for r in results if isinstance(r, dict)]
        refusals = [r for r in results if isinstance(r, Refused)]
        self.assertEqual((len(successes), len(refusals)), (1, 1), results)
        self.assertIn("being prepared", str(refusals[0]))
        self.assertEqual(len(resolver_calls), 1, "the loser is refused before the resolver or CDP are touched")
        self.assertEqual(self.env.call(lambda m: _count_active(m)), 1)
        self.assertFalse(self.env.call(lambda m: _preparing(m)))
        hid = successes[0]["handover_id"]
        self.assertEqual(self.env.status(hid, cancel=True)["state"], "cancelled")

        # a failing resolver (any exception) clears the reservation and the error stays generic
        def broken(task):
            raise RuntimeError(SENTINEL)
        self.env.call(lambda m: set_resolver(m, broken))
        with self.assertRaises(Refused) as ctx:
            self.env.create()
        self.assertNotIn(SENTINEL, str(ctx.exception))
        self.assertFalse(self.env.call(lambda m: _preparing(m)))
        # a CDP-stage refusal (no page on that origin) also clears it, and nothing is left behind
        self.env.call(lambda m: set_resolver(m, lambda task: binding))
        with self.assertRaises(Refused):
            self.env.call(lambda m: m.create("task-1", "https://nothing.example", "p", None))
        self.assertFalse(self.env.call(lambda m: _preparing(m)))
        self.assertEqual(self.env.call(lambda m: _count_active(m)), 0)
        self.assertTrue(self.prepared()["success"])
        self.assertNotIn(SENTINEL, "\n".join(self.logs.lines))

    # ── 3. ownership on frames and fresh checks around input ─────────────────

    def test_external_takeover_with_no_phone_input_stops_frames(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.controlling(hid)
            await viewer.wait_frames_since(0)
            self.env.lease.evict_with("desktop-viewer")          # Desktop pane takeover, phone idle
            state = await viewer.wait_for("state", timeout=4, predicate=lambda m: m["controller"] == "other")
            self.assertEqual(state["state"], "superseded")
            frames_at_state = viewer.frames
            # the new controller's screen keeps changing; none of it may reach the old viewer
            for i in range(4):
                await self.probe.eval("document.body.style.background='#%06x'" % (0x202020 * (i + 1)), target)
                await asyncio.sleep(0.25)
            await self.drain(viewer, 1.0)
            self.assertEqual(viewer.frames, frames_at_state, "frames after the takeover were forwarded")
            self.assertFalse(self.screencasting(hid))
            await viewer.close()
        run(go())
        self.assertEqual(self.env.lease.get().viewer_id, "desktop-viewer")

    def test_preview_pauses_when_someone_else_takes_the_browser(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.connected(hid)                    # agent holds; preview frames flowing
            self.env.lease.evict_with("desktop-viewer")
            state = await viewer.wait_for("state", timeout=4, predicate=lambda m: m["controller"] == "other")
            self.assertTrue(any("Someone else" in n for n in state["notices"]))
            frames_at_state = viewer.frames
            for i in range(3):
                await self.probe.eval("document.body.style.background='#%06x'" % (0x303030 * (i + 1)), target)
                await asyncio.sleep(0.25)
            await self.drain(viewer, 1.0)
            self.assertEqual(viewer.frames, frames_at_state)
            await viewer.send({"type": "take"})
            self.assertIn("Someone else", (await viewer.wait_for("error"))["message"])
            await viewer.close()
        run(go())

    def test_epoch_change_inside_awaited_input_discards_result(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]
        lease = self.env.lease

        async def wrap(m):
            page = m.records[hid].page
            original = page.dispatch

            async def dispatch(message):
                out = await original(message)
                lease.evict_with("desktop-viewer")            # control moves while the input is in flight
                return out
            page.dispatch = dispatch
            return True

        async def go():
            viewer = await self.controlling(hid)
            self.env.call(wrap)
            await viewer.send({"type": "probe", "x": 20, "y": 20})     # no sleep: cache would still be warm
            state = await viewer.wait_for("state", timeout=4, predicate=lambda m: m["controller"] == "other")
            self.assertEqual(state["state"], "superseded")
            self.assertFalse(any(m["type"] == "probe" for m in viewer.messages), "mid-flight result leaked")
            await viewer.send({"type": "text", "text": "LATE"})
            await self.drain(viewer, 0.6)
            self.assertNotIn("LATE", await self.probe.eval("document.body.innerText", target))
            await viewer.close()
        run(go())

    # ── 5. sanitized logs and errors ─────────────────────────────────────────

    def test_sensitive_exception_text_never_reaches_logs_or_tool_errors(self):
        hid = self.prepared()["handover_id"]
        text_before = len(self.logs.lines)

        # a) viewer session failure with a sentinel message: closed, fence untouched, type-only log
        async def boom(self, record, link):
            raise RuntimeError(SENTINEL + " " + SENTINEL_ENDPOINT)
        with patch.object(HandoverManager, "_viewer_session", boom):
            async def go():
                cookie, csrf, ticket = self.env.session(hid)
                viewer = await Viewer(self.env, hid, cookie, ticket).connect()
                await viewer.ws.wait_closed()
            run(go())
        self.assertFalse(self.env.lease.get().human)
        # b) housekeeping failure
        async def sweep_boom(self):
            raise RuntimeError(SENTINEL)
        async def restart_housekeeping(m, interval):
            m._housekeeper.cancel()
            m._housekeeper = None
            m.housekeeping_interval = interval
            m.start()
            return True
        with patch.object(HandoverManager, "sweep", sweep_boom):
            self.env.call(lambda m: restart_housekeeping(m, 0.05))
            time.sleep(0.3)
        self.env.call(lambda m: restart_housekeeping(m, 5.0))
        # c) CDP connect failure whose chained exception carries the endpoint
        async def set_resolver(m):
            m.resolver = lambda task: SessionBinding(session_key="k", cdp_endpoint=SENTINEL_ENDPOINT,
                                                     connection_id=SessionBinding.connection_hash(SENTINEL_ENDPOINT))
            return True
        self.env.status(hid, cancel=True)
        self.env.call(set_resolver)
        with self.assertRaises(Refused) as ctx:
            self.env.create()
        self.assertNotIn(SENTINEL, str(ctx.exception))
        # d) the tool wrapper's generic path
        registered = {}

        class Ctx:
            def register_tool(self, name, toolset, schema, handler, **kw):
                registered[name] = handler

            def on_unload(self, cb):
                pass

            def register_system_prompt_section(self, id, content, **kw):
                pass
        hbh.register(Ctx())

        class Broken:
            def call(self, factory, timeout=30.0):
                raise RuntimeError(SENTINEL + " " + SENTINEL_ENDPOINT)
        with patch.object(hbh, "_get_service", lambda: Broken()):
            answer = json.loads(registered["homelab_browser_handover"]({"page_origin": "https://x.example", "purpose": "p"}, task_id="t"))
            status = json.loads(registered["homelab_browser_handover_status"]({"handover_id": "x"}, task_id="t"))
        self.assertFalse(answer["success"] or status["success"])
        for payload in (answer, status):
            self.assertNotIn(SENTINEL, json.dumps(payload))
        new_lines = "\n".join(self.logs.lines[text_before:])
        self.assertTrue(new_lines, "the failures above must still be logged (type only)")
        self.assertNotIn(SENTINEL, new_lines)
        self.assertNotIn("Traceback", new_lines)
        self.assertNotIn("devtools", new_lines)
        self.assertIn("RuntimeError", new_lines)

    # ── 8. lease epoch checks around binding ─────────────────────────────────

    def test_lease_epoch_change_across_binding_is_refused(self):
        binding = self.env.binding
        lease = self.env.lease

        def flapping(task):
            lease.evict_with("desktop-viewer")
            lease.release("desktop-viewer")              # back to agent, but the epoch moved by two
            return binding

        async def set_resolver(m, fn):
            m.resolver = fn
            return True
        self.env.call(lambda m: set_resolver(m, flapping))
        with self.assertRaises(Refused) as ctx:
            self.env.create()
        self.assertIn("changed while binding", str(ctx.exception))
        self.assertFalse(self.env.lease.get().human)
        # a binding reporting an older epoch than the pre-check saw is refused as well
        stale = SessionBinding(session_key=binding.session_key, cdp_endpoint=binding.cdp_endpoint,
                               connection_id=binding.connection_id, lease_epoch=lease.get().epoch - 1)
        self.env.call(lambda m: set_resolver(m, lambda task: stale))
        with self.assertRaises(Refused):
            self.env.create()
        self.env.call(lambda m: set_resolver(m, lambda task: binding))
        self.assertTrue(self.prepared()["success"])

    # ── 9. fail-closed persistence ───────────────────────────────────────────

    def test_unpersistable_state_refuses_take_and_keeps_agent_control(self):
        hid = self.prepared()["handover_id"]
        blocker = self.env.tmp / "blocker"
        blocker.write_text("not a directory")

        async def misdirect(m, path):
            m.state_path = path
            return True
        self.env.call(lambda m: misdirect(m, blocker / "state.json"))

        async def go():
            viewer = await self.connected(hid)
            await viewer.send({"type": "take"})
            error = await viewer.wait_for("error")
            self.assertIn("cannot be saved", error["message"])
            self.assertFalse(self.env.lease.get().human, "a takeover that cannot be recorded must not hold the lease")
            self.assertEqual(self.env.status(hid)["state"], "viewing")
            self.env.call(lambda m: misdirect(m, self.env.tmp / "state" / "state.json"))
            await viewer.send({"type": "take"})
            state = await viewer.wait_for("state", predicate=lambda m: m["controller"] == "you")
            self.assertEqual(state["state"], "human")
            await viewer.send({"type": "release"})
            await viewer.wait_for("closed")
        run(go())
        self.assertFalse(self.env.lease.get().human)

    def test_corrupt_state_file_refuses_start_and_keeps_fence(self):
        hid = self.prepared()["handover_id"]
        run(self.controlling(hid))
        self.env.service.stop()
        self.assertTrue(self.env.lease.get().human)
        state_file = self.env.tmp / "state" / "state.json"
        good = state_file.read_text()
        state_file.write_text("{corrupt")
        with self.assertRaises(Refused) as ctx:
            self.env.start_service()
        self.assertIn("corrupt", str(ctx.exception))
        self.assertFalse(self.env.service.running)
        self.assertTrue(self.env.lease.get().human, "a refused start never touches the lease")
        state_file.write_text(json.dumps({"records": [{"id": "x"}]}))     # malformed record: also refused
        with self.assertRaises(Refused):
            self.env.start_service()
        state_file.write_text(good)
        self.env.start_service()
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["controller"]), ("human_disconnected", "human"))


class LifecycleSuite(unittest.TestCase):
    """Service start/stop races and the module-level lazy start (no Chromium needed)."""

    def setUp(self):
        self.env = Env(chrome=False)
        self.env.service.stop()

    def tearDown(self):
        self.env.close()

    def loop_threads(self, name):
        return [t for t in threading.enumerate() if t.name == f"{name}-loop" and t.is_alive()]

    def port_free(self, port):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def test_concurrent_first_tool_calls_start_one_service(self):
        cfg = ViewerConfig(public_origin=self.env.public_origin, tailscale_login=LOGIN, listen_port=self.env.port,
                           state_dir=self.env.tmp / "state")
        lease = self.env.lease
        results, errors = [], []

        def call():
            try:
                results.append(hbh._get_service())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        hbh._service = None
        try:
            with patch.object(hbh.config, "load", lambda path=None: cfg), patch.object(hbh.lease, "NativeLease", lambda: lease):
                threads = [threading.Thread(target=call) for _ in range(8)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(20)
                self.assertEqual(errors, [])
                self.assertEqual(len(results), 8)
                self.assertTrue(all(r is results[0] for r in results), "every caller must get the same service")
                self.assertEqual(len(self.loop_threads("homelab-browser-handover")), 1)
                self.assertTrue(results[0].running)
                # unload through the registered hook: thread and listener gone, lease untouched
                lease.acquire("someone-else", "held during unload")
                registered = {}

                class Ctx:
                    def register_tool(self, name, toolset, schema, handler, **kw):
                        pass

                    def on_unload(self, cb):
                        registered["unload"] = cb

                    def register_system_prompt_section(self, id, content, **kw):
                        pass
                hbh.register(Ctx())
                registered["unload"]()
                self.assertIsNone(hbh._service)
                self.assertEqual(self.loop_threads("homelab-browser-handover"), [])
                self.assertTrue(self.port_free(self.env.port))
                self.assertEqual(lease.get().viewer_id, "someone-else", "unload must never release the lease")
                lease.release("someone-else")
                # a later call starts cleanly again
                again = hbh._get_service()
                self.assertTrue(again.running)
                self.assertIsNot(again, results[0])
                registered["unload"]()
        finally:
            if hbh._service is not None:
                hbh._service.stop()
                hbh._service = None

    def test_slow_start_times_out_without_leaking_listener_or_thread(self):
        service = self.env.service
        original = hbh.server.ViewerServer.start

        async def slow(self_, *a, **k):
            await asyncio.sleep(1.5)
            return await original(self_, *a, **k)
        with patch.object(hbh.server.ViewerServer, "start", slow):
            with self.assertRaises(Refused) as ctx:
                service.start(timeout=0.3)
        self.assertIn("did not start in time", str(ctx.exception))
        self.assertFalse(service.running)
        self.assertEqual(self.loop_threads("hbh-test"), [])
        self.assertTrue(self.port_free(self.env.port), "a timed-out start must not leave the listener bound")
        with self.assertRaises(Refused):
            service.call(lambda m: m.status("x", "t"))
        service.start()
        self.assertTrue(service.running)
        self.assertEqual(len(self.loop_threads("hbh-test")), 1)
        service.stop()
        self.assertEqual(self.loop_threads("hbh-test"), [])

    def test_failed_bind_reports_type_only_and_leaves_no_thread(self):
        with socket.socket() as blocker:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", self.env.port))
            blocker.listen(1)
            with self.assertRaises(Refused) as ctx:
                self.env.service.start()
        message = str(ctx.exception)
        self.assertIn("OSError", message)
        self.assertNotIn("127.0.0.1", message)
        self.assertNotIn(str(self.env.port), message)
        self.assertFalse(self.env.service.running)
        self.assertEqual(self.loop_threads("hbh-test"), [])
        self.env.service.start()                           # the port is free again
        self.assertTrue(self.env.service.running)

    def test_restart_cycles_keep_lease_and_single_thread(self):
        self.env.lease.acquire("someone-else", "held")
        for _ in range(3):
            self.env.service.start()
            self.assertEqual(len(self.loop_threads("hbh-test")), 1)
            self.env.service.start()                       # idempotent
            self.assertEqual(len(self.loop_threads("hbh-test")), 1)
            self.env.service.stop()
            self.env.service.stop()                        # idempotent
            self.assertEqual(self.loop_threads("hbh-test"), [])
        self.assertEqual(self.env.lease.get().viewer_id, "someone-else")


async def _count_active(m):
    return sum(1 for r in m.records.values() if r.state in hbh.handover.ACTIVE_STATES)


async def _preparing(m):
    return m._preparing


if __name__ == "__main__":
    unittest.main()
