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
            await viewer.send({"type": "release"})
            closed = await viewer.wait_for("closed")
            self.assertEqual(closed["reason"], "returned")
            self.assertEqual(await self.probe.eval("({w:innerWidth,h:innerHeight})", target), before)
            self.assertFalse(self.env.lease.get().human)
            # edits made by the human are still on the page for the agent
            self.assertEqual(await self.probe.eval("document.getElementById('plate').value", target), "DEMO123Xé")
        run(go())
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["controller"], status["viewport_restored"], status["released_by"]),
                         ("returned", "agent", True, "owner"))
        self.assertTrue(status["agent_may_continue"])
        self.assertEqual(self.env.lease.get().epoch, 2)

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
            await again.send({"type": "release"})
            self.assertEqual((await again.wait_for("closed"))["reason"], "returned")
            self.assertFalse(self.env.lease.get().human)
            self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
        run(go())

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
            await again.send({"type": "release"})
            self.assertEqual((await again.wait_for("closed"))["reason"], "returned")
        run(finish())
        self.assertFalse(self.env.lease.get().human)
        self.assertEqual(run(self.probe.eval("innerWidth", target)), 1280)
        self.assertEqual(self.env.status(hid)["viewport_restored"], True)

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
            await second.send({"type": "release"})
            await second.wait_for("closed")
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
            await again.send({"type": "release"})
            await again.wait_for("closed")
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
            await viewer.send({"type": "release"})
            await viewer.wait_for("closed")
        run(go())

    def test_logs_and_status_never_carry_frames_endpoints_or_identity(self):
        result = self.prepared()
        hid = result["handover_id"]

        async def go():
            viewer = await self.controlling(hid)
            await viewer.wait_frames_since(0)
            await viewer.send({"type": "release"})
            await viewer.wait_for("closed")
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
