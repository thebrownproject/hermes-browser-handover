"""Pure unit tests: lease file semantics, config validation, origin/purpose validation, cookie store."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from support import LOGIN
from hbh.config import ConfigError, ViewerConfig, from_dict, public_origin
from hbh.handover import Refused, validate_origin, validate_purpose
from hbh.lease import AGENT, HUMAN, FileLease, LeaseUnavailable
from hbh.server import CookieStore


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lease = FileLease(self.tmp / "bot-desktop" / "lease.json")

    def test_fresh_profile_agent_holds(self):
        view = self.lease.get()
        self.assertEqual((view.holder, view.epoch, view.human), (AGENT, 0, False))

    def test_acquire_release_epochs_and_file_mode(self):
        a = self.lease.acquire("hbh:one", "test")
        self.assertEqual((a.holder, a.viewer_id, a.epoch), (HUMAN, "hbh:one", 1))
        self.assertEqual(oct((self.tmp / "bot-desktop" / "lease.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.lease.acquire("hbh:one", "again").epoch, 1, "re-acquire by holder is idempotent")
        self.assertEqual(self.lease.release("hbh:other").holder, HUMAN, "non-holder release is ignored")
        self.assertEqual(self.lease.release("hbh:one").holder, AGENT)
        self.assertEqual(self.lease.get().epoch, 2)
        self.assertEqual(self.lease.release("hbh:one").epoch, 2, "release when agent holds does not bump")
        with self.assertRaises(LeaseUnavailable):
            self.lease.release("")

    def test_corrupt_file_fails_closed(self):
        path = self.tmp / "bot-desktop" / "lease.json"
        path.parent.mkdir()
        path.write_text("{not json")
        self.assertTrue(self.lease.get().human)
        path.write_text(json.dumps({"holder": "nobody"}))
        self.assertTrue(self.lease.get().human)

    def test_native_file_shape_is_compatible(self):
        """The native module writes exactly these keys; ours must read and write the same."""
        self.lease.acquire("v", "r")
        data = json.loads((self.tmp / "bot-desktop" / "lease.json").read_text())
        self.assertEqual(set(data), {"holder", "viewer_id", "since", "reason", "epoch"})


class ConfigTests(unittest.TestCase):
    def test_public_origin(self):
        self.assertEqual(public_origin("https://Host.Example:8444"), "https://host.example:8444")
        for bad in ("https://host/path", "https://host?x=1", "ftp://host", "http://remote.example:1", "", None):
            with self.assertRaises(ConfigError):
                public_origin(bad)
        self.assertEqual(public_origin("http://127.0.0.1:8791"), "http://127.0.0.1:8791")

    def test_from_dict(self):
        cfg = from_dict({"public_origin": "https://h.example:8444", "tailscale_login": LOGIN})
        self.assertEqual((cfg.listen_port, cfg.listen_host, cfg.secure_cookies), (8791, "127.0.0.1", True))
        for bad in ({"public_origin": "https://h.example"}, {"public_origin": "https://h.example", "tailscale_login": ""},
                    {"public_origin": "https://h.example", "tailscale_login": LOGIN, "listen_port": 80},
                    {"public_origin": "https://h.example", "tailscale_login": LOGIN, "listen_host": "0.0.0.0"}, []):
            with self.assertRaises(ConfigError):
                from_dict(bad)

    def test_world_readable_file_refused(self):
        from hbh.config import load
        tmp = Path(tempfile.mkdtemp()) / "viewer.json"
        tmp.write_text(json.dumps({"public_origin": "https://h.example", "tailscale_login": LOGIN}))
        os.chmod(tmp, 0o644)
        with self.assertRaises(ConfigError):
            load(tmp)
        os.chmod(tmp, 0o600)
        self.assertEqual(load(tmp).public_origin, "https://h.example")


class ValidationTests(unittest.TestCase):
    def test_origin(self):
        self.assertEqual(validate_origin("https://Shop.Example", allow_loopback_http=False), "https://shop.example")
        for bad in ("https://shop.example/checkout", "http://shop.example", "shop.example", "https://127.0.0.1:1/x", 5):
            with self.assertRaises(Refused):
                validate_origin(bad, allow_loopback_http=False)
        with self.assertRaises(Refused):
            validate_origin("http://127.0.0.1:9", allow_loopback_http=False)
        self.assertEqual(validate_origin("http://127.0.0.1:9", allow_loopback_http=True), "http://127.0.0.1:9")

    def test_purpose(self):
        self.assertEqual(validate_purpose("  Finish parking booking "), "Finish parking booking")
        for bad in ("", "x" * 161, "line\nbreak", None):
            with self.assertRaises(Refused):
                validate_purpose(bad)


class CookieTests(unittest.TestCase):
    def test_issue_lookup_expiry_and_bound(self):
        now = [1000.0]
        store = CookieStore(clock=lambda: now[0])
        cookie, csrf = store.issue(LOGIN)
        self.assertIsNotNone(store.lookup(cookie, LOGIN))
        self.assertIsNone(store.lookup(cookie, "other@example"))
        self.assertIsNone(store.lookup(cookie + "x", LOGIN))
        self.assertIsNone(store.lookup("../etc", LOGIN))
        now[0] += 13 * 3600
        self.assertIsNone(store.lookup(cookie, LOGIN))
        for _ in range(100):
            store.issue(LOGIN)
        self.assertLessEqual(len(store.entries), 64)


if __name__ == "__main__":
    unittest.main()
