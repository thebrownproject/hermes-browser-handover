"""Owns one asyncio loop thread hosting the manager and the loopback listener.

Start is lazy (first tool call) and idempotent; stop joins the thread, closes
the socket and never touches the native lease. A failed or timed-out start leaves
no listener behind: the loop is told to shut down, and while the thread is still
alive the service refuses to start a second one instead of orphaning the first.
"""
import asyncio
import logging
import threading
import time
from pathlib import Path

from .handover import HandoverManager, Refused, RestoreError
from .server import ViewerServer

log = logging.getLogger("homelab_browser_handover")


class Service:
    def __init__(self, *, config, lease, resolver, state_dir: Path, allow_loopback_http=False,
                 local_demo=False, name="homelab-browser-handover", clock=time.time):
        self.config = config
        self.local_demo = local_demo
        self.lease = lease
        self.resolver = resolver
        self.state_dir = Path(state_dir)
        self.allow_loopback_http = allow_loopback_http
        self.name = name
        self.clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.manager: HandoverManager | None = None
        self.server: ViewerServer | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._stop: asyncio.Event | None = None
        self._aborted = False

    @property
    def running(self) -> bool:
        return (self._thread is not None and self._thread.is_alive() and self._ready.is_set()
                and self._start_error is None and self.manager is not None)

    def start(self, timeout=10.0):
        with self._lock:
            if self.running:
                return
            if self._thread is not None and self._thread.is_alive():
                raise Refused("Viewer service is still starting or stopping.")
            self._ready.clear()
            self._start_error = None
            self._aborted = False
            self._loop = None
            self._stop = None
            self._thread = threading.Thread(target=self._run, name=f"{self.name}-loop", daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout):
                # Too slow: tell the loop to shut down whatever it reaches, then wait briefly.
                self._aborted = True
                self._signal_stop()
                self._thread.join(timeout=5)
                if self._thread.is_alive():
                    # Keep the reference: running stays False and the next start() refuses until
                    # the thread is gone, so no second listener is ever bound beside it.
                    log.warning("viewer service start timed out; loop thread still shutting down")
                    raise Refused("Viewer service did not start in time; it is shutting down. Retry shortly.")
                self._thread = None
                raise Refused("Viewer service did not start in time.")
            if self._start_error is not None:
                error, self._start_error = self._start_error, None
                self._thread.join(timeout=5)
                if self._thread.is_alive():
                    log.warning("viewer service failed to start and its thread is still exiting")
                else:
                    self._thread = None
                if isinstance(error, Refused):
                    raise Refused(f"Viewer service failed to start: {error}")
                # Type only: the message could carry a path or socket detail.
                raise Refused(f"Viewer service failed to start ({error.__class__.__name__}).")

    def _signal_stop(self):
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._stop = asyncio.Event()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                self._loop = None

    async def _main(self):
        if self._aborted:
            self._ready.set()
            return
        manager = HandoverManager(lease=self.lease, resolver=self.resolver, public_origin=self.config.public_origin,
                                  state_path=self.state_dir / "state.json", allow_loopback_http=self.allow_loopback_http,
                                  clock=self.clock)
        server = ViewerServer(config=self.config, manager=manager, local_demo=self.local_demo, clock=self.clock)
        try:
            restored = await manager.restore()
            if restored:
                log.info("restored %d held handover record(s)", restored)
            await server.start()
            manager.start()
        except RestoreError as exc:
            self._start_error = Refused(f"{exc}; inspect the state file before use. The lease is untouched.")
            self._ready.set()
            await server.stop()
            return
        except BaseException as exc:  # noqa: BLE001 - reported to the starter by type
            self._start_error = exc
            self._ready.set()
            await server.stop()
            return
        if self._aborted or self._stop.is_set():
            # The starter gave up while we were binding: undo immediately, never leave a listener.
            await manager.unload()
            await server.stop()
            self._ready.set()
            return
        self.manager, self.server = manager, server
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            await manager.unload()
            await server.stop()
            self.manager = self.server = None

    def stop(self, timeout=10.0):
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._aborted = True
            self._signal_stop()
            thread.join(timeout)
            self._ready.clear()
            if thread.is_alive():
                # Keep the reference so start() refuses instead of binding a duplicate listener.
                log.warning("viewer service thread did not stop within %ss", timeout)
                return
            self._thread = None

    def call(self, coro_factory, timeout=30.0):
        """Run ``coro_factory()`` on the service loop from any thread."""
        loop, manager = self._loop, self.manager
        if not self.running or loop is None or manager is None:
            raise Refused("Viewer service is not running.")
        future = asyncio.run_coroutine_threadsafe(coro_factory(manager), loop)
        try:
            return future.result(timeout)
        except Refused:
            raise
        except TimeoutError:
            future.cancel()
            raise Refused("Viewer operation timed out.") from None
