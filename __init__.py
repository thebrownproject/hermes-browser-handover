"""Native Hermes plugin entry point: two tools, a prompt section and an unload hook.

The tool hands the agent's prepared page to the owner's phone through a private,
identity-authenticated viewer and reports when the owner explicitly hands it back.
It never navigates, fills, submits or reads page content, and it never sends
messages on its own: the agent relays the returned link in the conversation.
"""
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("homelab_browser_handover")

GUIDANCE = (
    "To let the owner finish a page on their phone (details, choices, payment entry), prepare the exact page "
    "with the browser tools first, then call homelab_browser_handover with the page's exact origin and a short "
    "plain-language purpose. When using named browser_exec, pass browser_session with that exact name. Send only the returned link in the conversation; say he will see Take control, "
    "Release control and Finish session at the top. While he holds control (status controller human, "
    "agent_may_continue false), do not use browser tools, screenshots, computer_use or payment tools on that "
    "browser: they are refused. Release control hands the page back but keeps the same link open: he can tap "
    "Take control again at any time until the owner taps Finish session or the link expires. So treat "
    "agent_may_continue as valid for one step only: check homelab_browser_handover_status immediately before "
    "each browser action while the handover is not finished, and stop again as soon as it reports false. "
    "Poll sparingly otherwise, or wait for the owner to say they are done. Never describe the handover as a payment "
    "completion; confirm outcomes on the page afterwards. Do not cancel a handover a human currently holds."
)

_service = None
_service_lock = threading.RLock()


def _refused(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _get_service():
    """Lazy, idempotent service start with protected runtime configuration.

    Serialized: concurrent first tool calls wait for one start instead of each binding
    a listener. A service that stopped (or is still stopping) is restarted in place.
    """
    with _service_lock:
        return _get_service_locked()


def _get_service_locked():
    global _service
    if _service is not None:
        if not _service.running:
            _service.start()   # refuses while the previous loop thread is still shutting down
        return _service
    from . import config as config_module
    from .binding import resolve_task_session, BindingRefused
    from .handover import Refused
    from .lease import NativeLease, LeaseUnavailable
    from .service import Service
    try:
        cfg = config_module.load()
    except config_module.ConfigError as exc:
        raise Refused(f"Viewer is not configured: {exc}") from None
    try:
        lease = NativeLease()
    except LeaseUnavailable as exc:
        raise Refused(f"Human-control lease adapter unavailable: {exc}") from None

    def resolver(task_id, browser_session=None):
        try:
            return resolve_task_session(task_id, lease=lease, browser_session=browser_session)
        except BindingRefused as exc:
            raise Refused(str(exc)) from None
    if cfg.state_dir:
        state_dir = cfg.state_dir
    else:
        try:
            from hermes_constants import get_hermes_home
            state_dir = Path(get_hermes_home()) / "homelab-browser-handover"
        except Exception:
            raise Refused("Hermes home directory is unavailable for viewer state.") from None
    service = Service(config=cfg, lease=lease, resolver=resolver, state_dir=state_dir)
    service.start()
    _service = service
    return _service


def register(ctx):
    from .handover import Refused

    def unload():
        global _service
        with _service_lock:
            service, _service = _service, None
            if service is not None:
                # Closes the listener and streams. The native lease is left exactly as it is.
                service.stop()
    ctx.on_unload(unload)

    def handover(args, **kwargs):
        try:
            task = kwargs.get("task_id") or "default"
            service = _get_service()
            result = service.call(lambda m: m.create(task, args["page_origin"], args["purpose"], args.get("target_id"), args.get("browser_session")))
            result["instructions"] = ("Send the link to the owner in this conversation. Wait until status reports "
                                      "agent_may_continue true before touching the browser, and re-check it before "
                                      "each later browser step: the owner can take control again on the same link until "
                                      "the owner taps Finish session.")
            return json.dumps(result)
        except Refused as exc:
            return _refused(str(exc))
        except Exception as exc:  # noqa: BLE001 - type only: no message, no traceback, no endpoint
            log.warning("handover creation failed (%s)", exc.__class__.__name__)
            return _refused("Browser handover could not be prepared.")

    def status(args, **kwargs):
        try:
            task = kwargs.get("task_id") or "default"
            service = _get_service()
            return json.dumps(service.call(lambda m: m.status(args["handover_id"], task, bool(args.get("cancel", False)))))
        except Refused as exc:
            return _refused(str(exc))
        except Exception as exc:  # noqa: BLE001
            log.warning("handover status failed (%s)", exc.__class__.__name__)
            return _refused("Handover status is unavailable.")

    ctx.register_tool(
        name="homelab_browser_handover", toolset="homelab_browser_handover", emoji="📱", handler=handover,
        schema={"name": "homelab_browser_handover",
                "description": ("Hand the CURRENT prepared browser page to the owner's phone through a private owner-only link. "
                                "The owner takes control, finishes the page and explicitly releases control; browser, "
                                "screenshot and payment tools are refused meanwhile. The link stays open for repeat "
                                "Take control until the owner taps Finish session. Does not navigate, fill or submit. "
                                "Exactly one open page on page_origin is required."),
                "parameters": {"type": "object", "properties": {
                    "page_origin": {"type": "string", "description": "Exact HTTPS origin of the prepared page, e.g. https://shop.example.com"},
                    "purpose": {"type": "string", "description": "Short plain-language reason shown on the phone, max 160 characters."},
                    "browser_session": {"type": "string", "description": "Exact existing browser_exec session name, required when the page was prepared in a named session."},
                    "target_id": {"type": "string", "description": "Optional exact browser target id (e.g. checkout_target_id from payment inspect)."}},
                    "required": ["page_origin", "purpose"], "additionalProperties": False}})
    ctx.register_tool(
        name="homelab_browser_handover_status", toolset="homelab_browser_handover", emoji="📱", handler=status,
        schema={"name": "homelab_browser_handover_status",
                "description": ("State of a phone handover: who controls the browser, whether the owner is connected, "
                                "how many times they have handed back (cycles), whether they may still take control again "
                                "(human_may_retake) and whether the agent may act RIGHT NOW (agent_may_continue; re-check "
                                "before each browser step). Never returns page content. cancel=true only works while no "
                                "human holds control."),
                "parameters": {"type": "object", "properties": {
                    "handover_id": {"type": "string"}, "cancel": {"type": "boolean"}},
                    "required": ["handover_id"], "additionalProperties": False}})
    ctx.register_system_prompt_section(id="homelab-browser-handover", content=GUIDANCE)
