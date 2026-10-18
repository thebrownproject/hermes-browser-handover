"""Human-control lease adapters.

The plugin never implements its own lock for the agent's browser. It reuses the
native Bot Desktop lease (``tools.bot_desktop.lease``), which is the single fence
the built-in browser tools, computer_use, vault fills and the janitor consult.

That module is NOT a documented public plugin API. ``NativeLease`` isolates the
import, validates the exact surface it relies on and fails closed when anything
differs. ``FileLease`` is a small reimplementation of the same on-disk format used
only by tests and the standalone demo, so neither ever touches a real profile.
"""
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

AGENT = "agent"
HUMAN = "human"


class LeaseUnavailable(RuntimeError):
    """The native lease cannot be used safely; callers must refuse, never fall back."""


@dataclass(frozen=True)
class LeaseView:
    holder: str
    viewer_id: str | None
    epoch: int
    reason: str = ""
    since: float = 0.0

    @property
    def human(self) -> bool:
        return self.holder == HUMAN


class NativeLease:
    """Validated wrapper around the installed Hermes lease module."""

    REQUIRED = ("acquire", "release", "get", "human_holds", "assert_agent_may_act", "HumanHasControl", "Lease")

    def __init__(self):
        try:
            from tools.bot_desktop import lease as module  # private core module; see README
        except Exception as exc:  # pragma: no cover - depends on host install
            raise LeaseUnavailable("Native Bot Desktop lease module is unavailable.") from exc
        missing = [name for name in self.REQUIRED if not hasattr(module, name)]
        if missing:
            raise LeaseUnavailable("Native lease module lacks: " + ", ".join(missing))
        fields = getattr(getattr(module, "Lease"), "__dataclass_fields__", {})
        for name in ("holder", "viewer_id", "epoch", "reason", "since"):
            if name not in fields:
                raise LeaseUnavailable(f"Native lease record lacks field {name}.")
        import inspect
        try:
            acquire = inspect.signature(module.acquire)
            release = inspect.signature(module.release)
        except (TypeError, ValueError) as exc:
            raise LeaseUnavailable("Native lease signatures are not inspectable.") from exc
        if "viewer_id" not in acquire.parameters or "reason" not in acquire.parameters:
            raise LeaseUnavailable("Native lease.acquire signature changed.")
        if "viewer_id" not in release.parameters or "unless_human" not in release.parameters:
            raise LeaseUnavailable("Native lease.release signature changed.")
        if getattr(module, "AGENT", None) != AGENT or getattr(module, "HUMAN", None) != HUMAN:
            raise LeaseUnavailable("Native lease holder constants changed.")
        self._module = module

    @staticmethod
    def _view(lease) -> LeaseView:
        holder = getattr(lease, "holder", None)
        if holder not in (AGENT, HUMAN):
            # Unknown holder: treat as human so nothing automated proceeds.
            return LeaseView(HUMAN, None, int(getattr(lease, "epoch", 0) or 0), "unknown holder")
        return LeaseView(holder, getattr(lease, "viewer_id", None), int(getattr(lease, "epoch", 0) or 0),
                         str(getattr(lease, "reason", "") or ""), float(getattr(lease, "since", 0.0) or 0.0))

    def get(self) -> LeaseView:
        try:
            return self._view(self._module.get())
        except Exception as exc:
            raise LeaseUnavailable("Native lease read failed.") from exc

    def acquire(self, viewer_id: str, reason: str) -> LeaseView:
        try:
            return self._view(self._module.acquire(viewer_id, reason=reason))
        except Exception as exc:
            raise LeaseUnavailable("Native lease acquire failed.") from exc

    def release(self, viewer_id: str) -> LeaseView:
        """Release only when ``viewer_id`` holds. Never a bare/forced release."""
        if not viewer_id:
            raise LeaseUnavailable("Refusing a bare lease release.")
        try:
            return self._view(self._module.release(viewer_id))
        except Exception as exc:
            raise LeaseUnavailable("Native lease release failed.") from exc


class FileLease:
    """Same semantics and JSON shape as the native module, for tests and the demo only."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> LeaseView:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return LeaseView(AGENT, None, 0)
        except (OSError, ValueError):
            return LeaseView(HUMAN, "unreadable-lease", 0, "lease file corrupt")
        if not isinstance(data, dict) or data.get("holder") not in (AGENT, HUMAN):
            return LeaseView(HUMAN, "unreadable-lease", 0, "lease file corrupt")
        return LeaseView(data["holder"], data.get("viewer_id"), int(data.get("epoch", 0)),
                         str(data.get("reason", "")), float(data.get("since", 0.0)))

    def _write(self, view: LeaseView) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"holder": view.holder, "viewer_id": view.viewer_id, "since": view.since,
                                 "reason": view.reason, "epoch": view.epoch}))
        os.replace(tmp, self.path)

    def get(self) -> LeaseView:
        with self._lock:
            return self._read()

    def acquire(self, viewer_id: str, reason: str) -> LeaseView:
        with self._lock:
            current = self._read()
            if current.holder == HUMAN and current.viewer_id == viewer_id:
                return current
            view = LeaseView(HUMAN, viewer_id, current.epoch + 1, reason or "", time.time())
            self._write(view)
            return view

    def release(self, viewer_id: str) -> LeaseView:
        if not viewer_id:
            raise LeaseUnavailable("Refusing a bare lease release.")
        with self._lock:
            current = self._read()
            if current.holder == AGENT:
                return current
            if current.viewer_id != viewer_id:
                return current
            view = LeaseView(AGENT, None, current.epoch + 1, "", time.time())
            self._write(view)
            return view

    # Test helper mirroring a Desktop-pane takeover by someone else.
    def evict_with(self, other_viewer: str, reason: str = "desktop takeover") -> LeaseView:
        with self._lock:
            current = self._read()
            view = LeaseView(HUMAN, other_viewer, current.epoch + 1, reason, time.time())
            self._write(view)
            return view
