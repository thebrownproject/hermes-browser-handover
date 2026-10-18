"""Resolve the task's prepared local browser to a CDP endpoint with provenance.

Private Hermes internals are confined to this module and validated before use.
The install verified on 18/10/2026 (v0.22.2) runs the built-in browser tools, not
the Browser Use harness, so the prepared page lives in the task's own local
agent-browser session: ``tools.browser_tool._active_sessions`` keyed by
``_last_session_key(task_id)``. Only sessions that share the Bot Desktop browser
are accepted, because only those are fenced by the human-control lease.
"""
import hashlib
import inspect
import re
from dataclasses import dataclass


class BindingRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionBinding:
    session_key: str
    cdp_endpoint: str          # never returned to the model or a client
    connection_id: str         # sha256(endpoint)[:16], safe to show
    lease_epoch: int | None = None   # lease epoch the endpoint was obtained under (None: no lease given)

    @staticmethod
    def connection_hash(endpoint: str) -> str:
        return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


_SESSION_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_@.:-]{0,127}")


def compat_check() -> dict:
    """Return the exact internal surface this module depends on; raise when it moved."""
    try:
        from tools import browser_tool as bt
        from tools import browser_tool_session as session
    except Exception as exc:
        raise BindingRefused("Hermes browser internals are unavailable.") from exc
    required_bt = ("_active_sessions", "_cleanup_lock", "_last_session_key", "_session_info_owned_by_task")
    required_session = ("_run_browser_command", "_shares_bot_desktop_browser", "run_fenced")
    missing = [f"browser_tool.{n}" for n in required_bt if not hasattr(bt, n)]
    missing += [f"browser_tool_session.{n}" for n in required_session if not hasattr(session, n)]
    if missing:
        raise BindingRefused("Hermes browser internals changed: " + ", ".join(missing))
    if not isinstance(bt._active_sessions, dict):
        raise BindingRefused("Hermes active-session table has an unexpected type.")
    # The endpoint query is Hermes's own fenced path: ``_run_browser_command(session_key, ...)``
    # wraps ``_dispatch_browser_command`` in ``run_fenced_pair`` (admission + epoch check). The
    # signatures are pinned here so a reshuffled parameter order cannot silently change meaning.
    try:
        run_params = list(inspect.signature(session._run_browser_command).parameters)
        fence_params = list(inspect.signature(session.run_fenced).parameters)
    except (TypeError, ValueError) as exc:
        raise BindingRefused("Hermes browser command signatures are not inspectable.") from exc
    if run_params[:4] != ["task_id", "command", "args", "timeout"]:
        raise BindingRefused("Hermes _run_browser_command signature changed.")
    if fence_params[:2] != ["session_info", "fn"]:
        raise BindingRefused("Hermes run_fenced signature changed.")
    return {"browser_tool": sorted(required_bt), "browser_tool_session": sorted(required_session),
            "_run_browser_command": run_params[:4], "run_fenced": fence_params[:2]}


def _lease_epoch(lease, *, phase: str) -> int | None:
    """Fresh native lease read; refuses while a human holds. ``None`` when no adapter was given."""
    if lease is None:
        return None
    view = lease.get()
    if view.human:
        raise BindingRefused("A human already holds control of the browser. Wait for hand back." if phase == "before"
                             else "A human took control while binding. Wait for hand back.")
    return view.epoch


def resolve_task_session(task_id: str, lease=None) -> SessionBinding:
    """The task's current local browser session and its live CDP endpoint.

    Refuses: no session, foreign ownership, cloud/user-supplied CDP browsers (not
    lease fenced), a session record replaced during the endpoint query, a human
    holding the lease before, during or after the query, a lease epoch change across
    the query (even when the endpoint was already in the record and Hermes's own
    fence was not exercised), and any failure to obtain an endpoint. Never creates
    a new browser: a missing session is refused before Hermes is asked anything.
    """
    compat_check()
    from tools import browser_tool as bt
    from tools.browser_tool_session import _run_browser_command, _shares_bot_desktop_browser
    task = task_id or "default"
    key = bt._last_session_key(task)
    if not isinstance(key, str) or not _SESSION_KEY_RE.fullmatch(key):
        raise BindingRefused("The task's browser session key is malformed.")
    epoch_before = _lease_epoch(lease, phase="before")
    with bt._cleanup_lock:
        record = bt._active_sessions.get(key)
        if not record or not bt._session_info_owned_by_task(record, task, key):
            raise BindingRefused("No prepared browser session exists for this task. Open the page with the browser tools first.")
        info = dict(record)
    if not _shares_bot_desktop_browser(info):
        raise BindingRefused("The task's browser is not the lease-fenced local Bot Desktop browser; handover is refused.")
    endpoint = info.get("cdp_url")
    if not endpoint:
        # Same call Hermes's own vault tool makes for the packaged Chromium; fenced inside Hermes.
        result = _run_browser_command(key, "get", ["cdp-url"], timeout=10)
        if not isinstance(result, dict) or not result.get("success"):
            if isinstance(result, dict) and result.get("code") == "human_has_control":
                raise BindingRefused("A human already holds control of the browser. Wait for hand back.")
            raise BindingRefused("The prepared browser did not report a connection endpoint.")
        endpoint = (result.get("data") or {}).get("cdpUrl")
    if not isinstance(endpoint, str) or not endpoint.startswith(("ws://", "wss://", "http://", "https://")):
        raise BindingRefused("The prepared browser reported an unusable endpoint.")
    epoch_after = _lease_epoch(lease, phase="after")
    if epoch_after != epoch_before:
        raise BindingRefused("Control of the browser changed while binding. Retry on the current page.")
    with bt._cleanup_lock:
        current = bt._active_sessions.get(key)
        if current is not record or not bt._session_info_owned_by_task(current, task, key):
            raise BindingRefused("The browser session changed while binding. Retry on the current page.")
    return SessionBinding(session_key=key, cdp_endpoint=endpoint,
                          connection_id=SessionBinding.connection_hash(endpoint), lease_epoch=epoch_after)
