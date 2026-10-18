"""Real viewer page in a real browser: a second tab of the temp Chromium plays the phone.

The phone tab gets the identity header through CDP Network.setExtraHTTPHeaders (what
Tailscale Serve adds in production), loads /h/<id>, and the test drives the ACTUAL
viewer JavaScript: Take control, touch tap on the canvas, soft-keyboard text through
the hidden textarea, Release control. Assertions are read from the TARGET page.
"""
import asyncio
import json
import logging
import unittest

from support import LOGIN, CDPProbe, Env, run
from hbh.cdp import CDPConnection

logging.getLogger("websockets").setLevel(logging.WARNING)
PHONE = {"width": 390, "height": 844}


class PhoneTab:
    def __init__(self, env):
        self.env = env
        self.conn = CDPConnection(env.ws_url)
        self.session = None
        self.target_id = None

    async def open(self, path):
        await self.conn.connect()
        created = await self.conn.send("Target.createTarget", {"url": "about:blank"})
        self.target_id = created["targetId"]
        attach = await self.conn.send("Target.attachToTarget", {"targetId": self.target_id, "flatten": True})
        self.session = attach["sessionId"]
        send = lambda m, p=None: self.conn.send(m, p, session_id=self.session)
        await send("Page.enable")
        await send("Runtime.enable")
        await send("Network.enable")
        await send("Network.setExtraHTTPHeaders", {"headers": {"Tailscale-User-Login": LOGIN}})
        await send("Emulation.setDeviceMetricsOverride", {**PHONE, "deviceScaleFactor": 1, "mobile": True})
        await send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
        await send("Page.addScriptToEvaluateOnNewDocument", {"source": "window.__errors=[];window.addEventListener('error',e=>window.__errors.push(String(e.message)));window.addEventListener('unhandledrejection',e=>window.__errors.push('rejection'));"})
        await send("Page.navigate", {"url": f"{self.env.public_origin}{path}"})
        await asyncio.sleep(0.5)

    async def eval(self, expression):
        result = await self.conn.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                           "awaitPromise": True}, session_id=self.session)
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"].get("text"))
        return (result.get("result") or {}).get("value")

    async def wait(self, expression, timeout=15, interval=0.2):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            value = await self.eval(expression)
            if value:
                return value
            await asyncio.sleep(interval)
        raise TimeoutError(expression)

    async def touch_tap(self, x, y):
        send = lambda m, p: self.conn.send(m, p, session_id=self.session)
        await send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        await asyncio.sleep(0.05)
        await send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    async def insert_text(self, text):
        await self.conn.send("Input.insertText", {"text": text}, session_id=self.session)

    async def close(self):
        try:
            await self.conn.send("Target.closeTarget", {"targetId": self.target_id})
        finally:
            await self.conn.close()


class BrowserUITests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.probe = CDPProbe(self.env.ws_url)

    def tearDown(self):
        self.env.close()

    def test_synthetic_confirm_runs_under_csp(self):
        async def go():
            phone = PhoneTab(self.env)
            await phone.open("/demo")
            try:
                await phone.eval("document.getElementById('go').click()")
                self.assertIn("Confirmed locally: Demo Person", await phone.eval("document.getElementById('out').textContent"))
            finally:
                await phone.close()
        run(go())

    def test_phone_page_take_tap_type_release(self):
        result = self.env.create(purpose="Finish the demo form")
        hid, target = result["handover_id"], result["target_id"]

        async def go():
            phone = PhoneTab(self.env)
            await phone.open(f"/h/{hid}")
            try:
                # Page loaded, script ran, ticket fetched, websocket connected, first frame painted.
                await phone.wait("window.__hbhView && window.__hbhView.state === 'viewing'")
                await phone.wait("document.getElementById('cv').width === %d" % PHONE["width"])   # first frame painted
                self.assertEqual(await phone.eval("window.__errors"), [])
                self.assertEqual(await phone.eval("document.getElementById('sub').textContent"), "Finish the demo form")
                self.assertEqual(await phone.eval("document.getElementById('ctl').textContent"), "Take control")
                self.assertFalse(await phone.eval("document.getElementById('ctl').disabled"))
                self.assertFalse(self.env.lease.get().human)
                # Take control from the real button
                await phone.eval("document.getElementById('ctl').click()")
                await phone.wait("document.body.classList.contains('border-you')")
                self.assertTrue(self.env.lease.get().human)
                self.assertEqual(await phone.eval("document.getElementById('ctl').textContent"), "Release control")
                # The viewer measured its stage and the target page now has the phone layout
                await asyncio.sleep(0.8)
                layout = await self.probe.eval("({w:innerWidth,h:innerHeight})", target)
                self.assertEqual(layout["w"], PHONE["width"])
                self.assertLess(layout["h"], PHONE["height"])
                self.assertGreater(layout["h"], 600)
                await phone.wait("document.getElementById('cv').width === %d" % PHONE["width"])
                # Touch-tap the plate field through the canvas (1:1 mapping at dpr 1)
                point = await self.probe.eval(
                    "(()=>{const e=document.getElementById('plate');e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();"
                    "return {x:r.x+r.width/2,y:r.y+r.height/2}})()", target)
                # Account for letterboxing rather than assuming a 1:1 canvas.
                screen = await phone.eval("(()=>{const c=document.getElementById('cv'),r=c.getBoundingClientRect(),fit=Math.min(r.width/c.width,r.height/c.height);return {x:r.x+(r.width-c.width*fit)/2,y:r.y+(r.height-c.height*fit)/2,w:c.width*fit,h:c.height*fit}})()")
                await phone.touch_tap(screen["x"] + point["x"] * screen["w"] / layout["w"],
                                      screen["y"] + point["y"] * screen["h"] / layout["h"])
                await phone.wait("document.activeElement && document.activeElement.id === 'kb'")
                deadline = asyncio.get_running_loop().time() + 5
                while await self.probe.eval("document.activeElement.id", target) != "plate":
                    if asyncio.get_running_loop().time() >= deadline:
                        actual = await self.probe.eval("({active:document.activeElement.id,w:innerWidth,h:innerHeight,scroll:scrollY})", target)
                        raise AssertionError(f"Target field did not focus: {actual}; target point={point}; canvas={screen}")
                    await asyncio.sleep(0.05)
                # Soft keyboard: text lands in the hidden textarea and is forwarded
                await phone.insert_text("ZZ")
                deadline = asyncio.get_running_loop().time() + 5
                while await self.probe.eval("document.getElementById('plate').value", target) != "DEMO123ZZ":
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail("Forwarded text did not reach the target field")
                    await asyncio.sleep(0.05)
                self.assertEqual(await phone.eval("document.getElementById('kb').value"), "​", "sentinel restored")
                # Release from the real button
                await phone.eval("document.getElementById('ctl').click()")
                await phone.wait("document.getElementById('msg').textContent.indexOf('Handed back') >= 0")
                self.assertFalse(self.env.lease.get().human)
                self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
                self.assertEqual(await phone.eval("window.__errors"), [])
                # the page served to the phone carries no endpoint or identity material
                html = await phone.eval("document.documentElement.outerHTML")
                self.assertNotIn(self.env.ws_url, html)
                self.assertNotIn(LOGIN, html)
            finally:
                await phone.close()
        run(go())
        self.assertEqual(self.env.status(hid)["state"], "returned")

    def test_phone_page_without_identity_is_refused(self):
        result = self.env.create()
        hid = result["handover_id"]

        async def go():
            phone = PhoneTab(self.env)
            await phone.conn.connect()
            created = await phone.conn.send("Target.createTarget", {"url": f"{self.env.public_origin}/h/{hid}"})
            await asyncio.sleep(1.0)
            attach = await phone.conn.send("Target.attachToTarget", {"targetId": created["targetId"], "flatten": True})
            phone.session = attach["sessionId"]
            phone.target_id = created["targetId"]
            text = await phone.eval("document.body.innerText")
            self.assertIn("Private authenticated access required", text)
            await phone.close()
        run(go())
        self.assertFalse(self.env.lease.get().human)


if __name__ == "__main__":
    unittest.main()
