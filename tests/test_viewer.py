"""Real listener + real headless Chromium integration suite (no Hermes, no real profile).

Run:  /tmp/hbh-venv/bin/python -m unittest tests.test_viewer -v
Env:  HBH_CHROME=/path/to/chrome (default: T3's chrome-headless-shell 154 on VM102)
"""
import asyncio
import json
import logging
import os
import time
import unittest
from pathlib import Path

from support import LOGIN, WRONG_LOGIN, CDPProbe, Env, Viewer, run

logging.getLogger("websockets").setLevel(logging.WARNING)


class CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class ViewerSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = CaptureLogs()
        logging.getLogger("homelab_browser_handover").addHandler(cls.logs)
        logging.getLogger("homelab_browser_handover").setLevel(logging.DEBUG)

    def setUp(self):
        self.env = Env()
        self.probe = CDPProbe(self.env.ws_url)

    def tearDown(self):
        self.env.close()

    # ── helpers ──────────────────────────────────────────────────────────────

    def prepared(self):
        result = self.env.create()
        self.assertTrue(result["success"])
        self.assertNotIn("cdp_endpoint", result)
        self.assertNotIn("viewer_id", result)
        return result

    async def controlling(self, hid, viewer=None):
        if viewer is None:
            cookie, csrf, ticket = self.env.session(hid)
            viewer = await Viewer(self.env, hid, cookie, ticket).connect()
        state = await viewer.wait_for("state")
        await viewer.wait_frames(1)
        await viewer.send({"type": "take"})
        state = await viewer.wait_for("state", predicate=lambda m: m["controller"] == "you")
        self.assertEqual(state["state"], "human")
        self.assertTrue(self.env.lease.get().human)
        return viewer

    async def element_center(self, target_id, element_id):
        return await self.probe.eval(
            "(()=>{const e=document.getElementById(%s);e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();"
            "return {x:r.x+r.width/2,y:r.y+r.height/2}})()" % json.dumps(element_id), target_id)

    async def tap(self, viewer, point):
        await viewer.send({"type": "probe", "x": point["x"], "y": point["y"]})
        probe = await viewer.wait_for("probe")
        for action, buttons in (("move", 0), ("down", 1), ("up", 0)):
            await viewer.send({"type": "mouse", "action": action, "x": point["x"], "y": point["y"],
                               "button": "none" if action == "move" else "left", "buttons": buttons,
                               "clickCount": 0 if action == "move" else 1, "modifiers": 0})
        return probe

    async def key(self, viewer, key, code=None, key_code=0, text=None):
        for action in ("down", "up"):
            message = {"type": "key", "action": action, "key": key, "code": code or key, "keyCode": key_code, "modifiers": 0}
            if action == "down" and text:
                message["text"] = text
            await viewer.send(message)

    # ── authentication and transport ─────────────────────────────────────────

    def test_identity_host_and_demo_guards(self):
        hid = self.prepared()["handover_id"]
        for kwargs in ({"login": None}, {"login": WRONG_LOGIN}, {"login": LOGIN + "x"}, {"host": "evil.example:1"},
                       {"host": ""}, {"login": LOGIN.upper()}):
            resp = self.env.http(f"/h/{hid}", **kwargs)
            self.assertEqual(resp["status"], 403, kwargs)
            self.assertNotIn("cookie", resp)
        self.assertEqual(self.env.http("/demo", login=None)["status"], 403)
        self.assertEqual(self.env.http("/demo")["status"], 200)
        page = self.env.http(f"/h/{hid}")
        self.assertEqual(page["status"], 200)
        self.assertIn("HttpOnly", page["headers"]["set-cookie"])
        self.assertIn("SameSite=Strict", page["headers"]["set-cookie"])
        self.assertIn("frame-ancestors 'none'", page["headers"]["content-security-policy"])
        self.assertEqual(page["headers"]["cache-control"], "no-store")
        self.assertEqual(self.env.http("/h/" + "0" * 12)["status"], 200)   # unknown id: generic finished page
        self.assertIn(b"not active", self.env.http("/h/" + "0" * 12)["body"])
        self.assertEqual(self.env.http("/other")["status"], 404)
        self.assertEqual(self.env.http("/h/../../etc")["status"], 404)

    def test_ticket_requires_cookie_csrf_origin_and_is_single_use(self):
        hid = self.prepared()["handover_id"]
        page, csrf = self.env.open_page(hid)
        cookie = page["cookie"]
        self.assertEqual(self.env.ticket(hid, None, csrf)["status"], 403)             # no cookie
        self.assertEqual(self.env.ticket(hid, "A" * 43, csrf)["status"], 403)         # forged cookie
        self.assertEqual(self.env.ticket(hid, cookie, None)["status"], 403)           # no csrf header
        self.assertEqual(self.env.ticket(hid, cookie, "x" * 43)["status"], 403)       # wrong csrf
        self.assertEqual(self.env.ticket(hid, cookie, csrf, origin="https://evil.example")["status"], 403)
        self.assertEqual(self.env.http(f"/api/ticket/{hid}", cookie=cookie, headers={"X-HBH-CSRF": csrf, "Sec-Fetch-Site": "cross-site"})["status"], 403)
        self.assertEqual(self.env.http(f"/api/ticket/{hid}", cookie=cookie, headers={"X-HBH-CSRF": csrf})["status"], 200)  # same-origin fetch sends no Origin
        self.assertEqual(self.env.ticket(hid, cookie, csrf, login=WRONG_LOGIN)["status"], 403)
        self.assertEqual(self.env.ticket(hid, cookie, csrf, host="evil:9")["status"], 403)
        # a cookie issued to another identity cannot be reused by this one
        other_page = self.env.http(f"/h/{hid}", login=WRONG_LOGIN)
        self.assertEqual(other_page["status"], 403)
        ticket = self.env.ticket(hid, cookie, csrf)
        self.assertIsInstance(ticket, str)

        async def go():
            first = await Viewer(self.env, hid, cookie, ticket).connect()
            await first.wait_for("state")
            # replay the same ticket
            with self.assertRaises(Exception):
                second = await Viewer(self.env, hid, cookie, ticket).connect()
                await second.wait_for("state", timeout=3)
            await first.close()
        run(go())

    def test_websocket_rejects_cross_origin_wrong_identity_missing_cookie_and_wrong_handover(self):
        hid = self.prepared()["handover_id"]
        cookie, csrf, ticket = self.env.session(hid)
        from websockets.exceptions import InvalidStatus

        async def go():
            for kwargs in ({"origin": "https://evil.example"}, {"login": WRONG_LOGIN}, {"cookie": "B" * 43}):
                use_cookie = kwargs.pop("cookie", cookie)
                with self.assertRaises(InvalidStatus, msg=kwargs):
                    await Viewer(self.env, hid, use_cookie, ticket, **kwargs).connect()
            # ticket minted for this handover must not open another id
            other = Viewer(self.env, "0" * 12, cookie, ticket)
            try:
                await other.connect()
                with self.assertRaises(Exception):
                    await other.wait_for("state", timeout=3)
            except InvalidStatus:
                pass
            # presenting the ticket to the wrong handover burned it; a fresh one works
            viewer = await Viewer(self.env, hid, cookie, self.env.ticket(hid, cookie, csrf)).connect()
            state = await viewer.wait_for("state")
            self.assertEqual(state["controller"], "agent")
            await viewer.close()
        run(go())

    def test_expired_ticket_is_refused(self):
        hid = self.prepared()["handover_id"]
        cookie, csrf, ticket = self.env.session(hid)
        self.env.call(lambda m: _expire_tickets(m))

        async def go():
            viewer = await Viewer(self.env, hid, cookie, ticket).connect()
            with self.assertRaises(Exception):
                await viewer.wait_for("state", timeout=3)
            self.assertEqual(viewer.ws.close_code, 4401)
        run(go())

    # ── control flow with the real page ──────────────────────────────────────

    def test_take_type_scroll_release_restores_viewport_and_lease(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            before = await self.probe.eval("({w:innerWidth,h:innerHeight})", target)
            viewer = await self.controlling(hid)
            await asyncio.sleep(0.3)
            self.assertEqual(await self.probe.eval("({w:innerWidth,h:innerHeight})", target), {"w": 390, "h": 700})
            lease = self.env.lease.get()
            self.assertTrue(lease.viewer_id.startswith("hbh:"))
            # prefilled values survive the handover untouched
            self.assertEqual(await self.probe.eval("document.getElementById('name').value", target), "Demo Person")
            self.assertEqual(await self.probe.eval("document.getElementById('pick').value", target), "Covered")
            # scroll by pan-to-wheel
            await viewer.send({"type": "wheel", "x": 100, "y": 300, "deltaX": 0, "deltaY": 300, "modifiers": 0})
            await asyncio.sleep(0.5)
            self.assertGreater(await self.probe.eval("scrollY", target), 0)
            # tap the plate field: probe says editable, typing appends, backspace deletes
            point = await self.element_center(target, "plate")
            probe = await self.tap(viewer, point)
            self.assertTrue(probe["editable"])
            await asyncio.sleep(0.2)
            self.assertEqual(await self.probe.eval("document.activeElement.id", target), "plate")
            await self.key(viewer, "End", key_code=35)
            await viewer.send({"type": "text", "text": "XY"})
            await self.key(viewer, "Backspace", key_code=8)
            await viewer.send({"type": "text", "text": "é"})          # composition-style text
            await asyncio.sleep(0.3)
            self.assertEqual(await self.probe.eval("document.getElementById('plate').value", target), "DEMO123Xé")
            # a non-editable tap reports false
            point = await self.element_center(target, "out")
            probe = await self.tap(viewer, point)
            self.assertFalse(probe["editable"])
            # Enter via keyDown with text \r submits nothing here but must not error
            await self.key(viewer, "Enter", key_code=13, text="\r")
            # a client resize while controlling changes the page viewport
            seen = viewer.frames
            await viewer.send({"type": "resize", "width": 360, "height": 640})
            await viewer.wait_for("viewport", predicate=lambda m: m["width"] == 360)
            await asyncio.sleep(0.2)
            self.assertEqual(await self.probe.eval("innerWidth", target), 360)
            await viewer.wait_frames_since(seen)
            # Ordinary release: the agent gets the browser back but the link stays usable (0.2.0).
            state = await viewer.release()
            self.assertEqual((state["state"], state["cycles"], state["released_by"]), ("viewing", 1, "owner"))
            self.assertEqual(await self.probe.eval("({w:innerWidth,h:innerHeight})", target), before)
            self.assertFalse(self.env.lease.get().human)
            # edits made by the human are still on the page for the agent
            self.assertEqual(await self.probe.eval("document.getElementById('plate').value", target), "DEMO123Xé")
            status = self.env.status(hid)
            self.assertEqual((status["state"], status["controller"], status["viewport_restored"], status["released_by"]),
                             ("viewing", "agent", True, "owner"))
            self.assertTrue(status["agent_may_continue"])
            self.assertTrue(status["human_may_retake"])
            self.assertEqual(self.env.lease.get().epoch, 2)
            # Explicit Finish ends the handover for good.
            await viewer.finish()
        run(go())
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["controller"], status["viewport_restored"], status["released_by"]),
                         ("finished", "agent", True, "owner"))
        self.assertTrue(status["agent_may_continue"])
        self.assertFalse(status["human_may_retake"])
        self.assertEqual(self.env.lease.get().epoch, 2)
        self.assertIn(b"finished from the phone", self.env.http(f"/h/{hid}")["body"])
        cookie = self.env.http(f"/h/{hid}")["cookie"]
        self.assertEqual(self.env.http(f"/api/ticket/{hid}", cookie=cookie, headers={"X-HBH-CSRF": "x" * 43})["status"], 403)

    def test_viewer_input_is_ignored_before_take_and_in_view_only(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            cookie, csrf, ticket = self.env.session(hid)
            viewer = await Viewer(self.env, hid, cookie, ticket).connect()
            await viewer.wait_for("state")
            point = await self.element_center(target, "plate")
            await viewer.send({"type": "mouse", "action": "down", "x": point["x"], "y": point["y"], "button": "left",
                               "buttons": 1, "clickCount": 1, "modifiers": 0})
            await viewer.send({"type": "text", "text": "HACK"})
            await viewer.send({"type": "resize", "width": 300, "height": 500})
            await viewer.send({"type": "probe", "x": 1, "y": 1})
            await asyncio.sleep(0.5)
            self.assertEqual(await self.probe.eval("document.getElementById('plate').value", target), "DEMO123")
            self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
            self.assertFalse(any(m["type"] == "probe" for m in viewer.messages))
            self.assertFalse(self.env.lease.get().human)
            await viewer.close()
        run(go())

    def test_disconnect_keeps_human_fence_until_explicit_release_after_reconnect(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.controlling(hid)
            await viewer.ws.close()
            await asyncio.sleep(0.6)
            lease = self.env.lease.get()
            self.assertTrue(lease.human, "disconnect must not hand control back")
            status = self.env.status(hid)
            self.assertEqual((status["state"], status["controller"], status["human_connected"]), ("human_disconnected", "human", False))
            # the agent cannot cancel while the human holds
            with self.assertRaises(Exception):
                self.env.status(hid, cancel=True)
            self.assertTrue(self.env.lease.get().human)
            # the page is still 390 wide (not restored while the human holds)
            self.assertEqual(await self.probe.eval("innerWidth", target), 390)
            # reconnect with fresh identity-authenticated ticket: control resumes without re-taking
            cookie, csrf, ticket = self.env.session(hid)
            again = await Viewer(self.env, hid, cookie, ticket).connect()
            state = await again.wait_for("state")
            self.assertEqual((state["state"], state["controller"]), ("human", "you"))
            self.assertEqual(self.env.lease.get().epoch, 1)      # no extra transition
            await again.wait_frames_since(0)
            state = await again.release()
            self.assertEqual(state["state"], "viewing")
            self.assertFalse(self.env.lease.get().human)
            self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
            await again.finish()
        run(go())
        self.assertEqual(self.env.status(hid)["state"], "finished")

    def test_service_restart_keeps_fence_and_restores_record(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def take():
            viewer = await self.controlling(hid)
            return viewer
        viewer = run(take())
        self.env.restart_service()   # unload + reload with the same state dir
        self.assertTrue(self.env.lease.get().human, "plugin reload must not release the human lease")
        state_file = self.env.tmp / "state" / "state.json"
        self.assertEqual(oct(state_file.stat().st_mode & 0o777), "0o600")
        persisted = json.loads(state_file.read_text())
        self.assertEqual(persisted["records"][0]["state"], "human_disconnected")
        for record in persisted["records"]:
            self.assertFalse(any(isinstance(v, str) and len(v) > 2000 for v in record.values()), "no frame data persisted")
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["controller"]), ("human_disconnected", "human"))
        self.assertNotIn("cdp_endpoint", status)
        self.assertNotIn("link", status)

        async def finish():
            cookie, csrf, ticket = self.env.session(hid)
            again = await Viewer(self.env, hid, cookie, ticket).connect()
            state = await again.wait_for("state")
            self.assertEqual(state["controller"], "you")
            await again.wait_frames_since(0)
            self.assertEqual((await again.release())["state"], "viewing")
            self.assertFalse(self.env.lease.get().human)
            self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
            self.assertEqual(self.env.status(hid)["viewport_restored"], True)
            await again.finish()
        run(finish())
        self.assertFalse(self.env.lease.get().human)
        self.assertEqual(self.env.status(hid)["state"], "finished")

    def test_second_viewer_replaces_first_and_stale_input_is_dropped(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            first = await self.controlling(hid)
            cookie, csrf, ticket = self.env.session(hid)
            second = await Viewer(self.env, hid, cookie, ticket).connect()
            closed = await first.wait_for("closed")
            self.assertEqual(closed["reason"], "replaced")
            state = await second.wait_for("state")
            self.assertEqual(state["controller"], "you")
            # the evicted socket cannot inject input any more
            with self.assertRaises(Exception):
                await first.send({"type": "text", "text": "STALE"})
                await asyncio.sleep(0.3)
                await first.send({"type": "text", "text": "STALE"})
            await asyncio.sleep(0.3)
            self.assertNotIn("STALE", await self.probe.eval("document.body.innerText", target))
            await second.release()
            await second.finish()
        run(go())

    def test_external_lease_takeover_supersedes_viewer_mid_action(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.controlling(hid)
            # Someone else (Desktop pane) takes the native lease: epoch moves, viewer id changes
            self.env.lease.evict_with("desktop-viewer")
            await asyncio.sleep(0.3)
            point = await self.element_center(target, "plate")
            await viewer.send({"type": "text", "text": "LATE"})
            state = await viewer.wait_for("state", predicate=lambda m: m["controller"] == "other")
            self.assertEqual(state["state"], "superseded")
            await asyncio.sleep(0.3)
            self.assertEqual(await self.probe.eval("document.getElementById('plate').value", target), "DEMO123")
            await viewer.send({"type": "release"})
            error = await viewer.wait_for("error")
            self.assertIn("Another viewer", error["message"])
            self.assertEqual(self.env.lease.get().viewer_id, "desktop-viewer")
            status = self.env.status(hid)
            self.assertEqual((status["state"], status["controller"]), ("superseded", "human_other"))
            self.assertFalse(status["agent_may_continue"])
            await viewer.close()
        run(go())

    def test_target_loss_while_human_keeps_fence_until_release(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.controlling(hid)
            await self.probe.browser("Target.closeTarget", {"targetId": target})
            state = None
            for _ in range(3):
                await viewer.send({"type": "text", "text": "x"})
                try:
                    state = await viewer.wait_for("state", timeout=5, predicate=lambda m: m["state"] == "target_lost")
                    break
                except TimeoutError:
                    continue
            self.assertIsNotNone(state)
            self.assertEqual(state["controller"], "you")
            self.assertFalse(state["page_alive"])
            self.assertTrue(self.env.lease.get().human)
            await viewer.send({"type": "release"})
            self.assertEqual((await viewer.wait_for("closed"))["reason"], "returned")
            self.assertFalse(self.env.lease.get().human)
        run(go())
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["viewport_restored"]), ("returned", False))

    def test_malformed_oversize_and_bogus_messages(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.controlling(hid)
            for bogus in ("not json", json.dumps([1, 2]), json.dumps({"type": 5}), json.dumps({"type": "navigate", "url": "https://evil"}),
                          json.dumps({"type": "mouse", "action": "down", "x": "NaN", "y": None}),
                          json.dumps({"type": "key", "action": "down", "key": "A" * 500}),
                          json.dumps({"type": "wheel", "x": 1e308, "y": -1e308, "deltaY": float("inf") if False else 1e300}),
                          json.dumps({"type": "text", "text": 12}), json.dumps({"type": "evaluate", "expression": "1+1"}),
                          json.dumps({"type": "resize", "width": 1, "height": 1})):
                await viewer.send(bogus)
            await viewer.send({"type": "ping"})
            await viewer.wait_for("pong")
            self.assertEqual(await self.probe.eval("innerWidth", target), 390)
            self.assertEqual(await self.probe.eval("location.pathname", target), "/local-demo")
            await viewer.send("x" * 5000)                      # over max_size: connection closes
            await asyncio.sleep(0.5)
            self.assertTrue(self.env.lease.get().human, "oversize message must not hand control back")
            cookie, csrf, ticket = self.env.session(hid)
            again = await Viewer(self.env, hid, cookie, ticket).connect()
            self.assertEqual((await again.wait_for("state"))["controller"], "you")
            await again.release()
            await again.finish()
        run(go())

    def test_create_refusals(self):
        from hbh.handover import Refused
        with self.assertRaises(Refused):
            self.env.call(lambda m: m.create("task-1", "https://127.0.0.1:1/path", "p", None))
        with self.assertRaises(Refused):
            self.env.call(lambda m: m.create("task-1", self.env.public_origin, "", None))
        with self.assertRaises(Refused):
            self.env.call(lambda m: m.create("task-1", "https://nothing.example", "p", None))
        # ambiguous: open a second tab on the same origin
        run(self.probe.browser("Target.createTarget", {"url": self.env.local_demo_url()}))
        time.sleep(0.5)
        with self.assertRaises(Refused):
            self.env.create()
        pages = run(self.probe.pages())
        # explicit target id resolves the ambiguity
        result = self.env.create(target_id=pages[0]["targetId"])
        self.assertEqual(result["target_id"], pages[0]["targetId"])
        with self.assertRaises(Refused):      # one active handover at a time
            self.env.create(target_id=pages[1]["targetId"])
        # status for another task is refused
        with self.assertRaises(Refused):
            self.env.status(result["handover_id"], task="someone-else")
        cancelled = self.env.status(result["handover_id"], cancel=True)
        self.assertEqual(cancelled["state"], "cancelled")
        # lease held by a human (other viewer) refuses creation
        self.env.lease.evict_with("desktop-viewer")
        with self.assertRaises(Refused):
            self.env.create(target_id=pages[0]["targetId"])

    def test_link_expiry_only_while_agent_holds(self):
        result = self.prepared()
        hid = result["handover_id"]
        self.env.call(lambda m: _expire_link(m, hid))
        self.assertEqual(self.env.status(hid)["state"], "expired")
        self.assertFalse(self.env.lease.get().human)
        # a held handover never expires
        result = self.prepared()
        hid = result["handover_id"]

        async def go():
            viewer = await self.controlling(hid)
            self.env.call(lambda m: _expire_link(m, hid))
            self.assertEqual(self.env.status(hid)["state"], "human")
            self.assertTrue(self.env.lease.get().human)
            await viewer.release()
            await viewer.finish()
        run(go())

    def test_logs_and_status_never_carry_frames_endpoints_or_identity(self):
        result = self.prepared()
        hid = result["handover_id"]

        async def go():
            viewer = await self.controlling(hid)
            await viewer.wait_frames_since(0)
            await viewer.release()
            await viewer.finish()
        run(go())
        text = "\n".join(self.logs.lines)
        self.assertNotIn(self.env.ws_url, text)
        self.assertNotIn(LOGIN, text)
        self.assertNotIn("/9j/", text)                 # JPEG base64 prefix
        self.assertNotIn("hbh:", text)                 # lease capability
        status = json.dumps(self.env.status(hid))
        self.assertNotIn(self.env.ws_url, status)
        self.assertNotIn("hbh:", status)
        self.assertNotIn("local-demo", status)         # no page path/URL, origin only


class RepeatControlSuite(unittest.TestCase):
    """0.2.0 lifecycle: Release control keeps the link open for a repeat take; Finish session ends it.
    Every cycle acquires a fresh native lease epoch and is ownership-checked; the fence is never
    released by expiry, disconnect, restart or a failed hand back."""

    def setUp(self):
        self.now = [time.time()]
        self.env = Env(clock=lambda: self.now[0])
        self.probe = CDPProbe(self.env.ws_url)

    def tearDown(self):
        self.env.close()

    def prepared(self):
        result = self.env.create()
        self.assertTrue(result["success"])
        return result

    async def connected(self, hid):
        cookie, csrf, ticket = self.env.session(hid)
        viewer = await Viewer(self.env, hid, cookie, ticket).connect()
        await viewer.wait_for("state")
        await viewer.wait_frames(1)
        return viewer

    async def take(self, viewer, expect_epoch):
        seen = viewer.frames
        await viewer.send({"type": "take"})
        state = await viewer.wait_for("state", predicate=lambda m: m["controller"] == "you")
        self.assertEqual(state["state"], "human")
        await viewer.wait_frames_since(seen)             # the phone layout repaint reaches the viewer
        lease = self.env.lease.get()
        self.assertEqual((lease.human, lease.epoch), (True, expect_epoch))
        self.assertTrue(lease.viewer_id.startswith("hbh:"))
        return state

    async def type_into_plate(self, viewer, target, text):
        point = await self.probe.eval(
            "(()=>{const e=document.getElementById('plate');e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();"
            "return {x:r.x+r.width/2,y:r.y+r.height/2}})()", target)
        await viewer.send({"type": "probe", "x": point["x"], "y": point["y"]})
        await viewer.wait_for("probe")
        for action, buttons in (("move", 0), ("down", 1), ("up", 0)):
            await viewer.send({"type": "mouse", "action": action, "x": point["x"], "y": point["y"],
                               "button": "none" if action == "move" else "left", "buttons": buttons,
                               "clickCount": 0 if action == "move" else 1, "modifiers": 0})
        for action in ("down", "up"):
            await viewer.send({"type": "key", "action": action, "key": "End", "code": "End", "keyCode": 35, "modifiers": 0})
        await viewer.send({"type": "text", "text": text})
        deadline = asyncio.get_running_loop().time() + 5
        while text not in await self.probe.eval("document.getElementById('plate').value", target):
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(f"text {text!r} did not reach the target")
            await asyncio.sleep(0.05)

    def record_epoch(self, hid):
        return self.env.call(lambda m: _record_fields(m, hid))

    # ── repeat cycles on one open viewer ─────────────────────────────────────

    def test_repeat_take_release_on_same_viewer_uses_fresh_epochs_and_restores_each_time(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]
        status = self.env.status(hid)
        self.assertEqual((status["agent_may_continue"], status["cycles"], status["human_may_retake"]), (False, 0, True))

        async def go():
            viewer = await self.connected(hid)
            before = await self.probe.eval("({w:innerWidth,h:innerHeight})", target)
            for cycle in (1, 2, 3):
                await self.take(viewer, expect_epoch=2 * cycle - 1)
                fields = self.record_epoch(hid)
                self.assertEqual((fields["state"], fields["epoch"], fields["cycles"]), ("human", 2 * cycle - 1, cycle - 1))
                status = self.env.status(hid)
                self.assertEqual((status["controller"], status["agent_may_continue"], status["human_may_retake"]),
                                 ("human", False, False))
                self.assertEqual(await self.probe.eval("innerWidth", target), 390)
                await self.type_into_plate(viewer, target, f"C{cycle}")
                seen = viewer.frames
                state = await viewer.release()
                self.assertEqual((state["state"], state["controller"], state["cycles"], state["released_by"]),
                                 ("viewing", "agent", cycle, "owner"))
                lease = self.env.lease.get()
                self.assertEqual((lease.human, lease.epoch), (False, 2 * cycle))
                self.assertEqual(await self.probe.eval("({w:innerWidth,h:innerHeight})", target), before)
                fields = self.record_epoch(hid)
                self.assertEqual((fields["state"], fields["epoch"], fields["cycles"], fields["viewport_restored"]),
                                 ("viewing", None, cycle, True))
                status = self.env.status(hid)
                self.assertEqual((status["state"], status["controller"], status["agent_may_continue"],
                                  status["human_may_retake"], status["cycles"]), ("viewing", "agent", True, True, cycle))
                self.assertGreater(status["expires_in_seconds"], 0)
                # the preview keeps streaming to the owner between cycles (the restored layout repaints)
                await viewer.wait_frames_since(seen)
                # input is ignored again until the next take
                await viewer.send({"type": "text", "text": "IGNORED"})
                await asyncio.sleep(0.3)
                self.assertNotIn("IGNORED", await self.probe.eval("document.getElementById('plate').value", target))
            self.assertEqual(await self.probe.eval("document.getElementById('plate').value", target), "DEMO123C1C2C3")
            await viewer.finish()
            self.assertEqual(self.env.lease.get().epoch, 6)
        run(go())
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["cycles"], status["agent_may_continue"], status["human_may_retake"],
                          status["human_connected"]), ("finished", 3, True, False, False))
        self.assertFalse(self.env.lease.get().human)
        # a finished handover is terminal: a new one can be prepared, the old link is dead
        self.assertEqual(self.env.call(lambda m: _page_info(m, hid))["active"], False)
        self.assertTrue(self.prepared()["success"])

    def test_release_then_disconnect_reconnect_and_take_again(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.connected(hid)
            await self.take(viewer, 1)
            await viewer.release()
            await viewer.ws.close()
            await asyncio.sleep(0.4)
            status = self.env.status(hid)
            self.assertEqual((status["state"], status["human_connected"], status["agent_may_continue"], status["human_may_retake"]),
                             ("prepared", False, True, True))
            self.assertFalse(self.env.lease.get().human)
            # the same link works again after a fresh identity-checked page visit
            again = await self.connected(hid)
            state = again.messages[0]
            self.assertEqual((state["state"], state["controller"], state["cycles"]), ("viewing", "agent", 1))
            await self.take(again, 3)
            self.assertFalse(self.env.status(hid)["agent_may_continue"])
            await self.type_into_plate(again, target, "R2")
            # disconnect while holding: fence kept, then recovered on reconnect, then released and finished
            await again.ws.close()
            await asyncio.sleep(0.4)
            self.assertTrue(self.env.lease.get().human)
            self.assertEqual(self.env.status(hid)["state"], "human_disconnected")
            third = await self.connected(hid)
            self.assertEqual((third.messages[0]["state"], third.messages[0]["controller"]), ("human", "you"))
            self.assertEqual(self.env.lease.get().epoch, 3, "reconnect never re-acquires")
            state = await third.release()
            self.assertEqual((state["state"], state["cycles"]), ("viewing", 2))
            self.assertEqual(self.env.lease.get().epoch, 4)
            await third.finish()
        run(go())
        self.assertEqual(self.env.status(hid)["state"], "finished")
        self.assertEqual(run(self.probe.eval("document.getElementById('plate').value", target)), "DEMO123R2")

    # ── finish paths ─────────────────────────────────────────────────────────

    def test_finish_while_holding_hands_back_restores_viewport_and_ends(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.connected(hid)
            await self.take(viewer, 1)
            self.assertEqual(await self.probe.eval("innerWidth", target), 390)
            closed = await viewer.finish()
            self.assertEqual(closed["reason"], "finished")
            self.assertFalse(self.env.lease.get().human)
            self.assertEqual(self.env.lease.get().epoch, 2)
            self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
            await viewer.ws.wait_closed()
        run(go())
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["controller"], status["viewport_restored"], status["released_by"],
                          status["cycles"], status["agent_may_continue"]), ("finished", "agent", True, "owner", 1, True))
        self.assertFalse((self.env.tmp / "state" / "state.json").exists() and
                         json.loads((self.env.tmp / "state" / "state.json").read_text())["records"], "nothing held")

    def test_finish_while_agent_holds_and_before_any_take(self):
        hid = self.prepared()["handover_id"]

        async def go():
            viewer = await self.connected(hid)
            self.assertFalse(self.env.lease.get().human)
            await viewer.finish()
        run(go())
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["cycles"], status["released_by"], status["agent_may_continue"]),
                         ("finished", 0, None, True))
        self.assertEqual(self.env.lease.get().epoch, 0, "finishing without a hold never touches the lease")

    def test_finish_and_release_refuse_to_touch_another_controllers_lease(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.connected(hid)
            await self.take(viewer, 1)
            self.env.lease.evict_with("desktop-viewer")                 # someone else now holds (epoch 2)
            await viewer.send({"type": "finish"})
            error = await viewer.wait_for("error")
            self.assertIn("Another viewer", error["message"])
            await viewer.send({"type": "release"})
            error = await viewer.wait_for("error")
            self.assertIn("Another viewer", error["message"])
            lease = self.env.lease.get()
            self.assertEqual((lease.human, lease.viewer_id, lease.epoch), (True, "desktop-viewer", 2))
            status = self.env.status(hid)
            self.assertEqual((status["state"], status["controller"], status["agent_may_continue"]), ("superseded", "human_other", False))
            # the other controller hands back from the Desktop pane: this handover is over (external)
            self.env.lease.release("desktop-viewer")
            status = self.env.status(hid)
            self.assertEqual((status["state"], status["released_by"], status["agent_may_continue"]), ("returned", "external", True))
            closed = await viewer.wait_for("closed")
            self.assertEqual(closed["reason"], "returned")
        run(go())
        # a stale record epoch from the first cycle never counts as ours even with our viewer id
        from hbh.handover import HandoverManager
        from hbh.lease import LeaseView
        record = type("R", (), {"viewer_id": "hbh:x", "epoch": 1})()
        self.assertFalse(HandoverManager.our_control(record, LeaseView("human", "hbh:x", 3)))
        self.assertTrue(HandoverManager.our_control(record, LeaseView("human", "hbh:x", 1)))
        record.epoch = None
        self.assertFalse(HandoverManager.our_control(record, LeaseView("human", "hbh:x", 1)))

    def test_stale_epoch_cannot_finish_or_release_a_new_hold_with_same_viewer_id(self):
        hid = self.prepared()["handover_id"]

        async def go():
            viewer = await self.connected(hid)
            await self.take(viewer, 1)
            current = self.env.lease.get()
            newer = self.env.lease.evict_with(current.viewer_id)
            for action in ("finish", "release"):
                await viewer.send({"type": action})
                error = await viewer.wait_for("error")
                self.assertIn("control changed", error["message"].lower())
                lease = self.env.lease.get()
                self.assertEqual((lease.human, lease.viewer_id, lease.epoch),
                                 (True, newer.viewer_id, newer.epoch))
                self.assertFalse(self.env.status(hid)["agent_may_continue"])
            await viewer.close()
        run(go())

    def test_other_viewer_between_cycles_blocks_retake_until_they_hand_back(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            viewer = await self.connected(hid)
            await self.take(viewer, 1)
            await viewer.release()                                      # epoch 2, viewing
            self.env.lease.evict_with("desktop-viewer")                 # epoch 3: someone else holds while we preview
            state = await viewer.wait_for("state", timeout=4, predicate=lambda m: m["controller"] == "other")
            self.assertEqual(state["state"], "viewing")
            await viewer.send({"type": "take"})
            self.assertIn("Someone else", (await viewer.wait_for("error"))["message"])
            status = self.env.status(hid)
            self.assertEqual((status["state"], status["controller"], status["agent_may_continue"]), ("viewing", "human_other", False))
            self.env.lease.release("desktop-viewer")                    # epoch 4
            await asyncio.sleep(1.3)                                    # heartbeat restarts the preview
            self.assertTrue(self.env.status(hid)["agent_may_continue"])
            await self.take(viewer, 5)
            await self.type_into_plate(viewer, target, "OK")
            await viewer.release()
            self.assertEqual(self.env.lease.get().epoch, 6)
            await viewer.finish()
        run(go())
        self.assertEqual(self.env.status(hid)["cycles"], 2)

    def test_failed_hand_back_keeps_hold_for_release_and_finish(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]
        lease = self.env.lease
        real_release = lease.release

        def failing_release(viewer_id):
            return lease.get()                                           # write failed: still human

        async def go():
            viewer = await self.connected(hid)
            await self.take(viewer, 1)
            lease.release = failing_release
            try:
                for kind in ("release", "finish"):
                    await viewer.send({"type": kind})
                    error = await viewer.wait_for("error")
                    self.assertIn("did not complete", error["message"])
                    held = lease.get()
                    self.assertEqual((held.human, held.epoch), (True, 1))
                    status = self.env.status(hid)
                    self.assertEqual((status["state"], status["controller"], status["agent_may_continue"], status["viewport_restored"]),
                                     ("human", "human", False, None))
                    self.assertIsNone(self.record_epoch(hid)["released_by"])
                # input still works: the hold is real and the viewer is still attached
                await self.type_into_plate(viewer, target, "HELD")
            finally:
                lease.release = real_release
            state = await viewer.release()
            self.assertEqual((state["state"], state["cycles"]), ("viewing", 1))
            self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
            await viewer.finish()
        run(go())
        self.assertEqual(self.env.status(hid)["state"], "finished")

    # ── bounded link lifetime across cycles ──────────────────────────────────

    def test_link_window_renews_on_release_and_never_expires_a_hold(self):
        from hbh.handover import LINK_TTL
        result = self.prepared()
        hid = result["handover_id"]
        self.assertAlmostEqual(self.env.status(hid)["expires_in_seconds"], LINK_TTL, delta=2)

        async def go():
            viewer = await self.connected(hid)
            await self.take(viewer, 1)
            self.now[0] += LINK_TTL + 60                                 # well past the original window
            self.env.call(lambda m: m.sweep())
            self.assertEqual(self.env.status(hid)["state"], "human")
            self.assertTrue(self.env.lease.get().human, "a held handover never expires")
            self.assertNotIn("expires_in_seconds", self.env.status(hid))
            state = await viewer.release()
            self.assertEqual(state["state"], "viewing")
            status = self.env.status(hid)
            self.assertAlmostEqual(status["expires_in_seconds"], LINK_TTL, delta=2, msg="release restarts the window")
            self.assertTrue(status["agent_may_continue"])
            # second cycle, then let the renewed window lapse while the agent holds
            await self.take(viewer, 3)
            await viewer.release()
            self.now[0] += LINK_TTL / 2
            self.env.call(lambda m: m.sweep())
            self.assertEqual(self.env.status(hid)["state"], "viewing")
            self.now[0] += LINK_TTL / 2 + 5
            self.env.call(lambda m: m.sweep())
            closed = await viewer.wait_for("closed")
            self.assertEqual(closed["reason"], "expired")
        run(go())
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["cycles"], status["agent_may_continue"], status["released_by"]),
                         ("expired", 2, True, "owner"))
        self.assertFalse(self.env.lease.get().human)
        self.assertEqual(self.env.lease.get().epoch, 4)
        self.assertIn(b"expired", self.env.http(f"/h/{hid}")["body"])

    def test_restart_during_second_cycle_keeps_fence_and_link_recovers(self):
        result = self.prepared()
        hid, target = result["handover_id"], result["target_id"]

        async def first():
            viewer = await self.connected(hid)
            await self.take(viewer, 1)
            await viewer.release()
            await self.take(viewer, 3)
            return viewer
        run(first())
        self.env.restart_service()
        self.assertTrue(self.env.lease.get().human)
        persisted = json.loads((self.env.tmp / "state" / "state.json").read_text())["records"][0]
        self.assertEqual((persisted["state"], persisted["epoch"], persisted["cycles"]), ("human_disconnected", 3, 1))
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["controller"], status["cycles"], status["agent_may_continue"]),
                         ("human_disconnected", "human", 1, False))

        async def second():
            again = await self.connected(hid)
            self.assertEqual((again.messages[0]["state"], again.messages[0]["controller"], again.messages[0]["cycles"]),
                             ("human", "you", 1))
            self.assertEqual(self.env.lease.get().epoch, 3)
            await self.type_into_plate(again, target, "AFTER")
            state = await again.release()
            self.assertEqual((state["state"], state["cycles"]), ("viewing", 2))
            self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
            # the link is still usable after the restart and the release
            await self.take(again, 5)
            await again.release()
            await again.finish()
        run(second())
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["cycles"], status["agent_may_continue"]), ("finished", 3, True))
        self.assertEqual(self.env.lease.get().epoch, 6)

    def test_restart_while_agent_holds_between_cycles_ends_the_link_without_touching_the_lease(self):
        hid = self.prepared()["handover_id"]

        async def go():
            viewer = await self.connected(hid)
            await self.take(viewer, 1)
            await viewer.release()
        run(go())
        self.env.restart_service()
        self.assertFalse(self.env.lease.get().human)
        self.assertEqual(self.env.lease.get().epoch, 2)
        from hbh.handover import Refused
        with self.assertRaises(Refused):           # not held, so not persisted: the agent must prepare a new link
            self.env.status(hid)
        self.assertIn(b"not active", self.env.http(f"/h/{hid}")["body"])
        self.assertTrue(self.prepared()["success"])


async def _page_info(manager, hid):
    return manager.page_info(hid)


async def _record_fields(manager, hid):
    record = manager.records[hid]
    return {"state": record.state, "epoch": record.epoch, "cycles": record.cycles,
            "viewport_restored": record.viewport_restored, "released_by": record.released_by}


async def _expire_tickets(manager):
    for entry in manager.tickets.values():
        entry["expires"] = 0
    return True


async def _expire_link(manager, hid):
    record = manager.records[hid]
    if record.expires_at:
        record.expires_at = 1.0
    await manager.sweep()
    return True


if __name__ == "__main__":
    unittest.main()
