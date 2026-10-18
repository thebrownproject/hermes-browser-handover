"""Shared helpers: package import under a stable name, temp Chromium, identity-aware clients.

Nothing here touches a Hermes profile. Chromium runs headless on a temporary
profile, the lease is a temporary file, and the listener binds a free loopback port.
"""
import asyncio
import http.client
import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if "hbh" not in sys.modules:
    spec = importlib.util.spec_from_file_location("hbh", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["hbh"] = module
    spec.loader.exec_module(module)

from hbh.binding import SessionBinding  # noqa: E402
from hbh.config import ViewerConfig  # noqa: E402
from hbh.demo import launch_chromium, stop_chromium  # noqa: E402
from hbh.lease import FileLease  # noqa: E402
from hbh.service import Service  # noqa: E402

LOGIN = "owner@example.test"
WRONG_LOGIN = "intruder@example.test"

DEFAULT_CHROME = os.environ.get("HBH_CHROME") or str(
    Path.home() / ".t3/tools/chrome-headless-shell/linux64/154.0.8037.92/chrome-headless-shell")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Env:
    """A running Service on a free port with a temp lease, optionally with a temp Chromium."""

    def __init__(self, *, chrome=True, public_scheme="http", clock=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="hbh-test-"))
        os.chmod(self.tmp, 0o700)
        self.clock = clock or time.time
        self.port = free_port()
        self.public_origin = f"{public_scheme}://127.0.0.1:{self.port}"
        self.config = ViewerConfig(public_origin=self.public_origin, tailscale_login=LOGIN, listen_port=self.port)
        self.lease = FileLease(self.tmp / "lease.json")
        self.binding = None
        self.proc = None
        self.ws_url = None
        self.service = None
        self.start_service()
        if chrome:
            self.proc, self.ws_url = launch_chromium(DEFAULT_CHROME, self.tmp / "profile", self.local_demo_url())
            self.binding = SessionBinding(session_key="test-session", cdp_endpoint=self.ws_url,
                                          connection_id=SessionBinding.connection_hash(self.ws_url))

    def local_demo_url(self):
        return f"http://127.0.0.1:{self.port}/local-demo"

    def start_service(self):
        self.service = Service(config=self.config, lease=self.lease, resolver=lambda task: self.binding,
                               state_dir=self.tmp / "state", allow_loopback_http=True, local_demo=True, name="hbh-test",
                               clock=self.clock)
        self.service.start()

    def restart_service(self):
        self.service.stop()
        self.start_service()

    def call(self, factory, timeout=30):
        return self.service.call(factory, timeout)

    def create(self, task="task-1", purpose="Test handover", target_id=None):
        return self.call(lambda m: m.create(task, self.public_origin, purpose, target_id))

    def status(self, handover_id, task="task-1", cancel=False):
        return self.call(lambda m: m.status(handover_id, task, cancel))

    def close(self):
        try:
            if self.service:
                self.service.stop()
        finally:
            if self.proc:
                stop_chromium(self.proc)
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def http(self, path, *, login=LOGIN, host=None, cookie=None, headers=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{self.port}")
        if login is not None:
            conn.putheader("Tailscale-User-Login", login)
        if cookie:
            conn.putheader("Cookie", f"hbh={cookie}")
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        result = {"status": resp.status, "headers": {k.lower(): v for k, v in resp.getheaders()}, "body": body}
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie:
            result["cookie"] = set_cookie.split(";")[0].split("=", 1)[1]
        conn.close()
        return result

    def open_page(self, handover_id, login=LOGIN):
        page = self.http(f"/h/{handover_id}", login=login)
        text = page["body"].decode()
        csrf = None
        marker = '"csrf": "'
        if marker in text:
            csrf = text.split(marker, 1)[1].split('"', 1)[0]
        return page, csrf

    def ticket(self, handover_id, cookie, csrf, *, origin=None, login=LOGIN, host=None):
        headers = {"Origin": origin if origin is not None else self.public_origin, "Sec-Fetch-Site": "same-origin"}
        if csrf is not None:
            headers["X-HBH-CSRF"] = csrf
        resp = self.http(f"/api/ticket/{handover_id}", login=login, cookie=cookie, headers=headers, host=host)
        if resp["status"] == 200:
            return json.loads(resp["body"])["ticket"]
        return resp

    def session(self, handover_id, login=LOGIN):
        """Open page, keep cookie, mint ticket."""
        page, csrf = self.open_page(handover_id, login)
        assert page["status"] == 200, page
        cookie = page["cookie"]
        ticket = self.ticket(handover_id, cookie, csrf)
        return cookie, csrf, ticket


class Viewer:
    """Async websocket viewer client with identity headers."""

    def __init__(self, env: Env, handover_id: str, cookie: str, ticket: str, *, origin=None, login=LOGIN,
                 hello=None):
        self.env = env
        self.handover_id = handover_id
        self.cookie = cookie
        self.ticket = ticket
        self.origin = origin if origin is not None else env.public_origin
        self.login = login
        self.hello = hello if hello is not None else {"type": "hello", "maxWidth": 780, "maxHeight": 1400, "width": 390, "height": 700}
        self.ws = None
        self.frames = 0
        self.messages = []

    async def connect(self):
        from websockets.asyncio.client import connect
        headers = {"Tailscale-User-Login": self.login,
                   "Cookie": f"hbh={self.cookie}", "Origin": self.origin}
        self.ws = await connect(f"ws://127.0.0.1:{self.env.port}/ws/{self.handover_id}?t={self.ticket}",
                                additional_headers=headers, max_size=32 * 1024 * 1024, open_timeout=10)
        if self.hello:
            try:
                await self.ws.send(json.dumps(self.hello))
            except Exception:
                pass  # a rejected ticket closes before hello; tests assert on close_code
        return self

    async def send(self, message):
        await self.ws.send(json.dumps(message) if not isinstance(message, (str, bytes)) else message)

    async def recv(self, timeout=10):
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        if isinstance(raw, bytes):
            self.frames += 1
            return {"type": "frame", "size": len(raw)}
        message = json.loads(raw)
        self.messages.append(message)
        return message

    async def wait_for(self, kind, timeout=15, predicate=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"no {kind} message")
            message = await self.recv(remaining)
            if message["type"] == kind and (predicate is None or predicate(message)):
                return message

    async def wait_frames(self, count=1, timeout=15):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        seen = 0
        while seen < count:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("no frames")
            message = await self.recv(remaining)
            if message["type"] == "frame":
                seen += 1
        return seen

    async def wait_frames_since(self, seen, timeout=15):
        """Frames already consumed by wait_for count; wait until the total exceeds ``seen``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.frames <= seen:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("no new frames")
            await self.recv(remaining)
        return self.frames

    async def close(self):
        if self.ws:
            await self.ws.close()


class CDPProbe:
    """Direct CDP access for assertions about the temp browser (test-only)."""

    def __init__(self, ws_url):
        self.ws_url = ws_url

    async def eval(self, expression, target_id=None):
        from hbh.cdp import CDPConnection
        conn = CDPConnection(self.ws_url)
        await conn.connect()
        try:
            if target_id is None:
                pages = await conn.list_pages()
                target_id = pages[0]["targetId"]
            attach = await conn.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            result = await conn.send("Runtime.evaluate", {"expression": expression, "returnByValue": True},
                                     session_id=attach["sessionId"])
            return (result.get("result") or {}).get("value")
        finally:
            await conn.close()

    async def pages(self):
        from hbh.cdp import CDPConnection
        conn = CDPConnection(self.ws_url)
        await conn.connect()
        try:
            return await conn.list_pages()
        finally:
            await conn.close()

    async def browser(self, method, params=None):
        from hbh.cdp import CDPConnection
        conn = CDPConnection(self.ws_url)
        await conn.connect()
        try:
            return await conn.send(method, params or {})
        finally:
            await conn.close()


def run(coro):
    return asyncio.run(coro)
