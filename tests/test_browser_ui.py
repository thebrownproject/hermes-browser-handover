"""Real viewer page in a separate temporary Chromium playing the phone.

The phone tab gets the identity header through CDP Network.setExtraHTTPHeaders (what
Tailscale Serve adds in production), loads /h/<id>, and the test drives the ACTUAL
viewer JavaScript: Take control, touch tap on the canvas, soft-keyboard text through
the hidden textarea, Release control. Assertions are read from the TARGET page.
"""
import asyncio
import json
import logging
import unittest

from support import LOGIN, CDPProbe, DEFAULT_CHROME, Env, run
from hbh.demo import launch_chromium, stop_chromium
from hbh.cdp import CDPConnection

logging.getLogger("websockets").setLevel(logging.WARNING)
PHONE = {"width": 390, "height": 844}


class PhoneTab:
    def __init__(self, env):
        self.env = env
        # Phone and agent are separate browsers in production. Full Chromium
        # suppresses scrolling in a background tab if both share one browser.
        self.proc, phone_endpoint = launch_chromium(DEFAULT_CHROME, env.tmp / "phone-profile", "about:blank")
        self.conn = CDPConnection(phone_endpoint)
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
            stop_chromium(self.proc)


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
                self.assertEqual(await phone.eval("document.getElementById('sub').textContent"), "Hermes")
                self.assertEqual(await phone.eval("document.getElementById('ctl').textContent"), "Take control")
                self.assertFalse(await phone.eval("document.getElementById('ctl').disabled"))
                self.assertEqual(await phone.eval("document.getElementById('fin').textContent"), "Finish session")
                self.assertFalse(await phone.eval("document.getElementById('fin').hidden || document.getElementById('fin').disabled"))
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
                # Release from the real button: handed back, link still open, button offers a repeat take
                await phone.eval("document.getElementById('ctl').click()")
                await phone.wait("window.__hbhView.controller === 'agent' && window.__hbhView.cycles === 1")
                self.assertFalse(self.env.lease.get().human)
                self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
                await phone.wait("document.getElementById('ctl').textContent === 'Take control again' && !document.getElementById('ctl').disabled")
                self.assertEqual((await phone.eval("window.__hbhView"))["cycles"], 1)
                status = self.env.status(hid)
                self.assertEqual((status["state"], status["agent_may_continue"], status["human_may_retake"]), ("viewing", True, True))
                self.assertEqual(await phone.eval("window.__errors"), [])
                # Take control again from the same page: fresh lease epoch, phone layout again
                await phone.eval("document.getElementById('ctl').click()")
                await phone.wait("document.body.classList.contains('border-you')")
                self.assertEqual((self.env.lease.get().human, self.env.lease.get().epoch), (True, 3))
                self.assertFalse(self.env.status(hid)["agent_may_continue"])
                deadline = asyncio.get_running_loop().time() + 5
                while (await self.probe.eval("innerWidth", target)) != PHONE["width"]:
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail("second take did not re-apply the phone viewport")
                    await asyncio.sleep(0.05)
                # Finish session while holding: first tap arms, second tap hands back and ends the link
                await phone.eval("document.getElementById('fin').click()")
                self.assertEqual(await phone.eval("document.getElementById('fin').textContent"), "Confirm finish")
                self.assertTrue(await phone.eval("(()=>{const b=document.getElementById('bar').getBoundingClientRect();return ['ctl','fin'].every(id=>{const r=document.getElementById(id).getBoundingClientRect();return r.left>=b.left&&r.right<=b.right&&r.top>=b.top&&r.bottom<=b.bottom})})()"), "control buttons fit the phone header")
                self.assertTrue(self.env.lease.get().human, "arming Finish must not change control")
                await phone.eval("document.getElementById('fin').click()")
                await phone.wait("document.getElementById('msg').textContent.indexOf('Session finished') >= 0")
                self.assertFalse(self.env.lease.get().human)
                self.assertEqual(self.env.lease.get().epoch, 4)
                self.assertEqual(await self.probe.eval("innerWidth", target), 1280)
                self.assertTrue(await phone.eval("document.getElementById('ctl').disabled && document.getElementById('fin').disabled"))
                self.assertEqual(await phone.eval("window.__errors"), [])
                # the page served to the phone carries no endpoint or identity material
                html = await phone.eval("document.documentElement.outerHTML")
                self.assertNotIn(self.env.ws_url, html)
                self.assertNotIn(LOGIN, html)
            finally:
                await phone.close()
        run(go())
        status = self.env.status(hid)
        self.assertEqual((status["state"], status["cycles"], status["viewport_restored"], status["released_by"]), ("finished", 2, True, "owner"))
        self.assertTrue(status["agent_may_continue"])

    def test_phone_swipe_scrolls_page_and_nested_panel(self):
        result = self.env.create()
        target = result['target_id']
        async def go():
            phone = PhoneTab(self.env)
            await phone.open('/h/' + result['handover_id'])
            try:
                await phone.wait("window.__hbhView && window.__hbhView.state === 'viewing'")
                await phone.eval("document.getElementById('ctl').click()")
                await phone.wait("window.__hbhView.controller === 'you'")
                await phone.wait("document.getElementById('cv').width === 390")
                await asyncio.sleep(.4)
                async def swipe(x, y, distance):
                    send = lambda m,p: phone.conn.send(m,p,session_id=phone.session)
                    await send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
                    for i in range(1,9):
                        await send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y-distance*i/8}]})
                        await asyncio.sleep(.03)
                    await send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
                    await asyncio.sleep(.4)
                await swipe(190,650,280)
                self.assertGreater(await self.probe.eval('scrollY',target),150,'A real phone swipe must scroll the remote page')
                # An isolated overflow panel reproduces modal checkout scrolling.
                await self.probe.eval("document.body.innerHTML='<div id=panel><div id=inside>Scrollable modal</div></div>'; document.styleSheets[0].insertRule('#panel{position:fixed;inset:0;overflow:auto;background:white}');document.styleSheets[0].insertRule('#inside{height:2400px}')",target)
                self.assertGreater(await self.probe.eval("document.getElementById('panel').scrollHeight-document.getElementById('panel').clientHeight",target),1000)
                await asyncio.sleep(.3)
                await swipe(190,650,280)
                self.assertGreater(await self.probe.eval("document.getElementById('panel').scrollTop",target),150,'Swipe must scroll the inner modal')
            finally:
                await phone.close()
        run(go())

    def test_native_touch_events_scroll_without_clicking(self):
        result=self.env.create()
        target=result['target_id']
        async def go():
            phone=PhoneTab(self.env)
            await phone.open('/h/'+result['handover_id'])
            try:
                await phone.wait("window.__hbhView && window.__hbhView.state === 'viewing'")
                await phone.eval("document.getElementById('ctl').click()")
                await phone.wait("window.__hbhView.controller === 'you' && document.getElementById('cv').width === 390")
                await phone.eval("""(async()=>{
                  const c=document.getElementById('cv'),r=c.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height*.8;
                  function fire(type,yy){const t={identifier:1,target:c,clientX:x,clientY:yy};
                    const e=new Event(type,{bubbles:true,cancelable:true});
                    Object.defineProperties(e,{touches:{value:type==='touchend'?[]:[t]},changedTouches:{value:[t]}});c.dispatchEvent(e);}
                  fire('touchstart',y);
                  for(let i=1;i<=8;i++){fire('touchmove',y-i*30);await new Promise(r=>setTimeout(r,30));}
                  fire('touchend',y-240);
                })()""")
                deadline=asyncio.get_running_loop().time()+4
                while (await self.probe.eval('scrollY',target))<150:
                    if asyncio.get_running_loop().time()>deadline:self.fail('Native touch gesture did not scroll')
                    await asyncio.sleep(.05)
                self.assertLess(await self.probe.eval('scrollY',target),360,'One gesture must not be duplicated')
                self.assertEqual(await self.probe.eval("document.getElementById('out').textContent",target),'')
            finally:await phone.close()
        run(go())

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
