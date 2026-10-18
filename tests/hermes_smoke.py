"""Adapter compatibility smoke against the INSTALLED Hermes, in an isolated profile.

Run on VM101 through the supported launcher entry (no venv/bin/python exists there):

    HERMES_HOME=$(mktemp -d) ~/.local/bin/hermes --run-module unittest \
        discover -s /path/to/plugin/tests -p hermes_smoke.py -v

Checks: native lease surface validation, acquire/release/epoch through the real
module, on-disk format compatibility with the plugin's FileLease, the pinned
signatures of Hermes's fenced command path, session binding refusals against
patched session tables (no browser is ever launched), lease-epoch fences around
endpoint discovery including the endpoint-already-in-record path, and tool
registration through a PluginContext-shaped fake. The live lease file is never read
or written: an isolated HERMES_HOME is mandatory and asserted.
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HOME = os.environ.get("HERMES_HOME", "")
assert HOME and Path(HOME).resolve() != (Path.home() / ".hermes").resolve(), "set an isolated HERMES_HOME"
ROOT = Path(__file__).resolve().parents[1]
if "hbh" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("hbh", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["hbh"] = _module
    _spec.loader.exec_module(_module)
hbh = sys.modules["hbh"]

from hbh.binding import BindingRefused, compat_check, resolve_task_session  # noqa: E402
from hbh.lease import FileLease, NativeLease  # noqa: E402

ENDPOINT = "ws://127.0.0.1:1/devtools/browser/x"
RECORD = {"features": {"local": True}, "owner_task_id": "smoke-task", "session_key": "smoke-task", "cdp_url": None}


def _ok(*_a, **_k):
    return {"success": True, "data": {"cdpUrl": ENDPOINT}}


class NativeLeaseSmoke(unittest.TestCase):
    def test_1_surface_and_fence(self):
        native = NativeLease()
        view = native.get()
        self.assertFalse(view.human)
        taken = native.acquire("hbh:smoke", "Phone handover: smoke")
        self.assertTrue(taken.human and taken.viewer_id == "hbh:smoke")
        self.assertEqual(native.acquire("hbh:smoke", "again").epoch, taken.epoch)
        self.assertTrue(native.release("hbh:other").human, "foreign release must be ignored")
        with self.assertRaises(Exception) as ctx:
            native.release("")
        self.assertIn("bare", str(ctx.exception))
        from tools.bot_desktop import lease as native_module
        self.assertTrue(native_module.human_holds())
        with self.assertRaises(native_module.HumanHasControl):
            native_module.assert_agent_may_act()
        released = native.release("hbh:smoke")
        self.assertFalse(released.human)
        self.assertEqual(released.epoch, taken.epoch + 1)

    def test_2_file_format_compatible_both_ways(self):
        native = NativeLease()
        mirror = FileLease(Path(HOME) / "bot-desktop" / "lease.json")
        native.acquire("hbh:smoke", "mirror")
        mirrored = mirror.get()
        self.assertEqual((mirrored.holder, mirrored.viewer_id), ("human", "hbh:smoke"))
        native.release("hbh:smoke")
        self.assertEqual(mirror.get().epoch, native.get().epoch)
        mirror.acquire("hbh:file", "from file lease")
        from tools.bot_desktop import lease as native_module
        self.assertEqual(native.get().viewer_id, "hbh:file")
        self.assertTrue(native_module.human_holds())
        mirror.release("hbh:file")
        self.assertFalse(native.get().human)
        raw = json.loads((Path(HOME) / "bot-desktop" / "lease.json").read_text())
        self.assertEqual(set(raw), {"holder", "viewer_id", "since", "reason", "epoch"})
        self.assertEqual(oct((Path(HOME) / "bot-desktop" / "lease.json").stat().st_mode & 0o777), "0o600")


class BindingSmoke(unittest.TestCase):
    """No browser is launched: the session table and the command path are patched."""

    def setUp(self):
        from tools import browser_tool as bt
        self.bt = bt
        self.native = NativeLease()
        self.assertFalse(self.native.get().human, "smoke lease must start with the agent holding")

    def test_1_compat_surface_and_pinned_signatures(self):
        surface = compat_check()
        self.assertEqual(surface["_run_browser_command"], ["task_id", "command", "args", "timeout"])
        self.assertEqual(surface["run_fenced"], ["session_info", "fn"])
        import inspect
        from tools import browser_tool_session as session
        src = inspect.getsource(session._run_browser_command)
        self.assertIn("run_fenced_pair", src, "endpoint discovery must still run inside Hermes's own fence")

    def test_2_refusals_without_touching_hermes(self):
        bt = self.bt
        with patch.object(bt, "_active_sessions", {}), patch.object(bt, "_last_active_session_key", {}), \
             patch("tools.browser_tool_session._run_browser_command", autospec=True) as run:
            with self.assertRaises(BindingRefused) as ctx:
                resolve_task_session("smoke-task", lease=self.native)
            self.assertIn("No prepared browser session", str(ctx.exception))
            run.assert_not_called()                       # a missing session never creates a browser
        with patch.object(bt, "_active_sessions", {"smoke-task": dict(RECORD, owner_task_id="other")}), \
             patch.object(bt, "_last_active_session_key", {}), patch("tools.browser_tool_session._run_browser_command", autospec=True) as run:
            with self.assertRaises(BindingRefused):
                resolve_task_session("smoke-task", lease=self.native)
            run.assert_not_called()
        with patch.object(bt, "_active_sessions", {"smoke-task": {"features": {"cdp_override": True}, "cdp_url": "ws://x"}}), \
             patch.object(bt, "_last_active_session_key", {}):
            with self.assertRaises(BindingRefused) as ctx:
                resolve_task_session("smoke-task", lease=self.native)
            self.assertIn("not the lease-fenced", str(ctx.exception))

    def test_3_human_lease_refuses_before_any_dispatch(self):
        bt = self.bt
        self.native.acquire("hbh:smoke", "hold")
        try:
            with patch.object(bt, "_active_sessions", {"smoke-task": dict(RECORD)}), patch.object(bt, "_last_active_session_key", {}), \
                 patch("tools.browser_tool_session._dispatch_browser_command") as dispatch, \
                 patch("tools.browser_tool_session._run_browser_command", autospec=True) as run:
                with self.assertRaises(BindingRefused) as ctx:
                    resolve_task_session("smoke-task", lease=self.native)
                self.assertIn("already holds control", str(ctx.exception))
                dispatch.assert_not_called()
                run.assert_not_called()
            # endpoint already in the record (real-profile style): Hermes's fence is not exercised, ours is
            with patch.object(bt, "_active_sessions", {"smoke-task": dict(RECORD, cdp_url=ENDPOINT)}), \
                 patch.object(bt, "_last_active_session_key", {}), \
                 patch("tools.browser_tool_session._shares_bot_desktop_browser", return_value=True):
                with self.assertRaises(BindingRefused) as ctx:
                    resolve_task_session("smoke-task", lease=self.native)
                self.assertIn("already holds control", str(ctx.exception))
            # and the real Hermes fence (no lease adapter given) refuses too, through _run_browser_command
            with patch.object(bt, "_active_sessions", {"smoke-task": dict(RECORD)}), patch.object(bt, "_last_active_session_key", {}), \
                 patch("tools.browser_tool_session._dispatch_browser_command") as dispatch, \
                 patch("tools.browser_tool_session._shares_bot_desktop_browser", return_value=True), \
                 patch("tools.browser_tool_session._browser_command_preflight", return_value={"browser_cmd": "/nonexistent"}), \
                 patch("tools.browser_tool_session._get_session_info", return_value=dict(RECORD)):
                with self.assertRaises(BindingRefused) as ctx:
                    resolve_task_session("smoke-task")
                self.assertIn("already holds control", str(ctx.exception))
                dispatch.assert_not_called()
        finally:
            self.native.release("hbh:smoke")

    def test_4_epoch_change_during_discovery_is_refused(self):
        bt = self.bt
        native = self.native

        def flapping(task_id, command, args=None, timeout=None, _engine_override=None):
            native.acquire("hbh:desk", "mid-binding takeover")
            native.release("hbh:desk")                   # agent again, epoch moved
            return _ok()
        with patch.object(bt, "_active_sessions", {"smoke-task": dict(RECORD)}), patch.object(bt, "_last_active_session_key", {}), \
             patch("tools.browser_tool_session._run_browser_command", autospec=True, side_effect=flapping), \
             patch("tools.browser_tool_session._shares_bot_desktop_browser", return_value=True):
            with self.assertRaises(BindingRefused) as ctx:
                resolve_task_session("smoke-task", lease=native)
            self.assertIn("changed while binding", str(ctx.exception))
        self.assertFalse(native.get().human)

        # a human holding at the end of discovery (took over during it) is refused
        def taken(task_id, command, args=None, timeout=None, _engine_override=None):
            native.acquire("hbh:desk", "takeover")
            return _ok()
        try:
            with patch.object(bt, "_active_sessions", {"smoke-task": dict(RECORD)}), patch.object(bt, "_last_active_session_key", {}), \
                 patch("tools.browser_tool_session._run_browser_command", autospec=True, side_effect=taken), \
                 patch("tools.browser_tool_session._shares_bot_desktop_browser", return_value=True):
                with self.assertRaises(BindingRefused) as ctx:
                    resolve_task_session("smoke-task", lease=native)
                self.assertIn("took control", str(ctx.exception))
        finally:
            native.release("hbh:desk")

    def test_5_replaced_record_refused_and_stable_binding_carries_epoch(self):
        bt = self.bt
        sessions = {"smoke-task": dict(RECORD)}

        def replaced(task_id, command, args=None, timeout=None, _engine_override=None):
            sessions["smoke-task"] = dict(RECORD)
            return _ok()
        with patch.object(bt, "_active_sessions", sessions), patch.object(bt, "_last_active_session_key", {}), \
             patch("tools.browser_tool_session._run_browser_command", autospec=True, side_effect=replaced), \
             patch("tools.browser_tool_session._shares_bot_desktop_browser", return_value=True):
            with self.assertRaises(BindingRefused) as ctx:
                resolve_task_session("smoke-task", lease=self.native)
            self.assertIn("changed while binding", str(ctx.exception))
        with patch.object(bt, "_active_sessions", {"smoke-task": dict(RECORD)}), patch.object(bt, "_last_active_session_key", {}), \
             patch("tools.browser_tool_session._run_browser_command", autospec=True, side_effect=_ok), \
             patch("tools.browser_tool_session._shares_bot_desktop_browser", return_value=True):
            binding = resolve_task_session("smoke-task", lease=self.native)
        self.assertEqual(binding.session_key, "smoke-task")
        self.assertEqual(len(binding.connection_id), 16)
        self.assertNotIn("127.0.0.1", binding.connection_id)
        self.assertEqual(binding.lease_epoch, self.native.get().epoch)


class RegistrationSmoke(unittest.TestCase):
    def test_register_unconfigured_refusal_and_unload(self):
        registered = {}

        class Ctx:
            def register_tool(self, name, toolset, schema, handler, **kw):
                registered[name] = (schema, handler)

            def on_unload(self, cb):
                registered["unload"] = cb

            def register_system_prompt_section(self, id, content, **kw):
                registered["prompt"] = (id, content)
        hbh.register(Ctx())
        self.assertEqual(set(registered), {"homelab_browser_handover", "homelab_browser_handover_status", "unload", "prompt"})
        with patch.dict(os.environ, {"HOME": HOME}):
            answer = json.loads(registered["homelab_browser_handover"][1]({"page_origin": "https://x.example", "purpose": "p"}, task_id="t"))
        self.assertFalse(answer["success"])
        self.assertIn("not configured", answer["error"])
        registered["unload"]()
        self.assertIsNone(hbh._service)


if __name__ == "__main__":
    unittest.main()
