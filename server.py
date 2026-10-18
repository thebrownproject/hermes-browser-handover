"""Loopback HTTP + WebSocket listener for the owner's phone viewer.

Trust boundary: the socket binds 127.0.0.1 only and is reached through an SSH
loopback forward and Tailscale Serve, which terminates TLS and adds the
``Tailscale-User-Login`` identity header. Every page, API and websocket request
must carry the exact configured login AND the exact public Host; POST and
websocket requests must also carry the exact public Origin, a session cookie this
listener set, and (ticket GET) the page's CSRF header or (WS) a single-use ticket.

Anything on the Hermes host that can reach loopback could forge the header. That
is the documented same-host boundary, identical to the code-entry listener.
"""
import asyncio
import hmac
import html
import json
import re
import secrets
import time
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers

from .handover import Refused
from .viewer import demo_page, finished_page, viewer_page

COOKIE_NAME = "hbh"
COOKIE_TTL = 12 * 3600
AUTH_RECHECK_S = 5.0      # an open websocket re-validates its session cookie this often
MAX_COOKIES = 64
_ID_RE = re.compile(r"[0-9a-f]{12}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")


class Guard(RuntimeError):
    pass


class CookieStore:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.entries: dict[str, dict] = {}

    def issue(self, login: str) -> tuple[str, str]:
        now = self.clock()
        self.entries = {k: v for k, v in self.entries.items() if now - v["created"] < COOKIE_TTL}
        while len(self.entries) >= MAX_COOKIES:
            self.entries.pop(next(iter(self.entries)))
        cookie = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self.entries[cookie] = {"login": login, "created": now, "csrf": csrf}
        return cookie, csrf

    def lookup(self, cookie: str | None, login: str) -> dict | None:
        if not isinstance(cookie, str) or not _TOKEN_RE.fullmatch(cookie):
            return None
        entry = self.entries.get(cookie)
        if not entry or self.clock() - entry["created"] >= COOKIE_TTL or not hmac.compare_digest(entry["login"], login):
            return None
        return entry


def _cookie_value(headers: Headers, name: str) -> str | None:
    for raw in headers.get_all("Cookie"):
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
    return None


class ViewerServer:
    def __init__(self, *, config, manager, clock=time.time, local_demo=False):
        self.config = config
        self.manager = manager
        # Demo/tests only: the synthetic form for a loopback Chromium that cannot add identity headers.
        self.local_demo = local_demo
        self.cookies = CookieStore(clock)
        self.public = urlsplit(config.public_origin)
        self._server = None

    # ── guards ───────────────────────────────────────────────────────────────

    def identity(self, headers: Headers) -> str:
        hosts = headers.get_all("Host")
        logins = headers.get_all("Tailscale-User-Login")
        if len(hosts) != 1 or len(logins) != 1:
            raise Guard("duplicate or missing headers")
        if hosts[0].lower() != self.public.netloc.lower():
            raise Guard("host")
        if not hmac.compare_digest(logins[0], self.config.tailscale_login):
            raise Guard("identity")
        return logins[0]

    def same_origin(self, headers: Headers, *, origin_required: bool):
        """Browsers always send Origin on websocket handshakes but not on same-origin GET fetches.
        The ticket GET is protected by the per-session CSRF header plus the SameSite=Strict cookie;
        Origin and Sec-Fetch-Site are still rejected whenever they are present and foreign."""
        origins = headers.get_all("Origin")
        if origin_required and len(origins) != 1:
            raise Guard("origin")
        if any(origin.lower() != self.config.public_origin for origin in origins):
            raise Guard("origin")
        fetch_site = headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site not in ("same-origin",):
            raise Guard("fetch site")

    def session(self, headers: Headers, login: str) -> tuple[str, dict]:
        cookie = _cookie_value(headers, COOKIE_NAME)
        entry = self.cookies.lookup(cookie, login)
        if not entry:
            raise Guard("session")
        return cookie, entry

    # ── responses ────────────────────────────────────────────────────────────

    def _respond(self, connection, status: HTTPStatus, body: str | bytes, content_type: str,
                 extra: dict | None = None, nonce: str | None = None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        response = connection.respond(status, "")
        response.body = data
        headers = response.headers
        for key in list(headers.keys()):
            del headers[key]
        ws_scheme = "wss" if self.public.scheme == "https" else "ws"
        csp = ("default-src 'none'; img-src blob: data:; "
               f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; " if nonce else
               "default-src 'none'; style-src 'unsafe-inline'; ")
        csp += (f"connect-src 'self' {ws_scheme}://{self.public.netloc} {self.public.scheme}://{self.public.netloc}; "
                "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in {
            "Content-Type": content_type, "Content-Length": str(len(data)), "Cache-Control": "no-store",
            "Pragma": "no-cache", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Content-Security-Policy": csp, "Connection": "close",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            **({"Strict-Transport-Security": "max-age=31536000"} if self.public.scheme == "https" else {}),
            **(extra or {})}.items():
            headers[key] = value
        return response

    def _demo(self, connection):
        nonce = secrets.token_urlsafe(16)
        return self._respond(connection, HTTPStatus.OK, demo_page(nonce),
                             "text/html; charset=utf-8", nonce=nonce)

    def _plain(self, connection, status: HTTPStatus, text: str):
        body = ('<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">'
                f"<p style=\"font:17px system-ui;margin:24px\">{html.escape(text)}</p>")
        return self._respond(connection, status, body, "text/html; charset=utf-8")

    def _set_cookie(self, cookie: str) -> str:
        return (f"{COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age={COOKIE_TTL}"
                + ("; Secure" if self.config.secure_cookies else ""))

    # ── routing ──────────────────────────────────────────────────────────────

    async def process_request(self, connection, request):
        headers = request.headers
        path, _, query = request.path.partition("?")
        if self.local_demo and path == "/local-demo":
            remote = getattr(connection, "remote_address", None)
            if remote and remote[0] in ("127.0.0.1", "::1"):
                return self._demo(connection)
        try:
            login = self.identity(headers)
        except Guard:
            return self._plain(connection, HTTPStatus.FORBIDDEN, "Private authenticated access required.")
        try:
            # websockets only parses GET; the ticket endpoint is a same-origin GET that
            # requires the page's CSRF header, which no cross-site request can attach.
            if headers.get("Transfer-Encoding") or headers.get("Content-Length", "0") not in ("0",):
                return self._plain(connection, HTTPStatus.BAD_REQUEST, "Request bodies are not accepted.")
            if path.startswith("/ws/"):
                return await self._ws_guard(connection, headers, login, path[4:], query)
            match = re.fullmatch(r"/h/([0-9a-f]{12})", path)
            if match:
                return self._page(connection, headers, login, match.group(1))
            match = re.fullmatch(r"/api/ticket/([0-9a-f]{12})", path)
            if match:
                return self._ticket(connection, headers, login, match.group(1))
            if path == "/demo":
                return self._demo(connection)
            return self._plain(connection, HTTPStatus.NOT_FOUND, "Not found.")
        except Guard:
            return self._plain(connection, HTTPStatus.FORBIDDEN, "Private authenticated access required.")
        except Refused as exc:
            return self._plain(connection, HTTPStatus.GONE, str(exc))
        except Exception:
            return self._plain(connection, HTTPStatus.INTERNAL_SERVER_ERROR, "Viewer unavailable.")

    def _page(self, connection, headers, login, handover_id):
        info = self.manager.page_info(handover_id)
        extra = {}
        cookie = _cookie_value(headers, COOKIE_NAME)
        entry = self.cookies.lookup(cookie, login)
        if entry is None:
            cookie, csrf = self.cookies.issue(login)
            extra["Set-Cookie"] = self._set_cookie(cookie)
        else:
            csrf = entry["csrf"]
        nonce = secrets.token_urlsafe(16)
        if not info or not info["active"]:
            body = finished_page(nonce, info["state"] if info else None)
        else:
            body = viewer_page(info, csrf, nonce)
        return self._respond(connection, HTTPStatus.OK, body, "text/html; charset=utf-8", extra, nonce)

    def _ticket(self, connection, headers, login, handover_id):
        self.same_origin(headers, origin_required=False)
        cookie, entry = self.session(headers, login)
        token = headers.get("X-HBH-CSRF")
        if not token or not hmac.compare_digest(token, entry["csrf"]):
            raise Guard("csrf")
        ticket = self.manager.mint_ticket(handover_id, cookie)
        return self._respond(connection, HTTPStatus.OK, json.dumps({"ticket": ticket}), "application/json")

    async def _ws_guard(self, connection, headers, login, handover_id, query):
        if not _ID_RE.fullmatch(handover_id) or "Upgrade" not in headers:
            raise Guard("ws path")
        self.same_origin(headers, origin_required=True)
        cookie, _entry = self.session(headers, login)
        ticket = parse_qs(query, max_num_fields=2).get("t", [""])[0]
        if not _TOKEN_RE.fullmatch(ticket or ""):
            raise Guard("ticket")
        connection.hbh = {"handover": handover_id, "cookie": cookie, "ticket": ticket, "login": login}
        return None

    async def handler(self, ws):
        ctx = getattr(ws, "hbh", None)
        if not ctx:
            await ws.close(4403, "forbidden")
            return
        session = asyncio.create_task(self.manager.attach_viewer(ctx["handover"], ctx["ticket"], ctx["cookie"], ws))
        # The handshake validated the cookie once; a socket may stay open for hours, so the same
        # session check (TTL + identity binding) repeats while it lives. Failure stops frames and
        # input on this socket only. The human lease is never touched: the owner reopens the link,
        # is re-identified, and hands back from there.
        try:
            while not session.done():
                await asyncio.wait({session}, timeout=AUTH_RECHECK_S)
                if session.done():
                    break
                if self.cookies.lookup(ctx["cookie"], ctx["login"]) is None:
                    await self.manager.expire_viewer(ctx["handover"], ws)
                    try:
                        await asyncio.wait_for(ws.close(4401, "session expired"), 3)
                    except Exception:
                        pass
                    break
            await session
        finally:
            if not session.done():
                session.cancel()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self):
        self._server = await serve(self.handler, self.config.listen_host, self.config.listen_port,
                                   process_request=self.process_request, max_size=4096, max_queue=16,
                                   ping_interval=20, ping_timeout=20, close_timeout=3, compression=None,
                                   server_header=None, reuse_address=True)
        return self._server

    async def stop(self):
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
