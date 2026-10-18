"""Standalone demo: an isolated temporary Chromium on the plugin's own harmless form.

Runnable directly (the plugin directory name is hyphenated, so it bootstraps itself):

    python3 demo.py --chrome ... --public-origin ... --login ... --port 8791


No Hermes import, no real profile, no native lease: the lease is a private file
in a temporary directory with the same semantics as the native one. Use it to
check the phone experience through the real transport before any live handover.

    python3 demo.py --chrome /path/to/chrome \
        --public-origin https://your-server.your-tailnet.ts.net:8444 \
        --login <tailnet login> --port 8791

Prints the private link and stays up across repeated Take/Release cycles. It exits
when the phone taps Finish session, when the link expires while the agent holds, on an
external hand back, or on Ctrl-C; the temporary browser and profile go.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

if not __package__:  # executed as a file: load the plugin directory as package "hbh"
    import importlib.util as _ilu
    _root = Path(__file__).resolve().parent
    _spec = _ilu.spec_from_file_location("hbh", _root / "__init__.py", submodule_search_locations=[str(_root)])
    _pkg = _ilu.module_from_spec(_spec)
    sys.modules["hbh"] = _pkg
    _spec.loader.exec_module(_pkg)
    from hbh.demo import main as _main
    sys.exit(_main())

from .binding import SessionBinding
from .config import ViewerConfig, public_origin
from .lease import FileLease
from .service import Service


def launch_chromium(chrome: str, profile: Path, url: str, *, headless=True, extra=()):
    argv = [chrome, f"--user-data-dir={profile}", "--remote-debugging-port=0", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking", "--disable-sync",
            "--disable-extensions", "--disable-component-update", "--window-size=1280,900", *extra, url]
    if headless:
        argv.insert(1, "--headless=new")
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={"HOME": str(profile), "PATH": os.environ.get("PATH", ""), **({"DISPLAY": os.environ["DISPLAY"]} if "DISPLAY" in os.environ else {})},
                            start_new_session=True)
    port_file = profile / "DevToolsActivePort"
    deadline = time.time() + 20
    while time.time() < deadline:
        if port_file.exists():
            lines = port_file.read_text().splitlines()
            if len(lines) >= 2:
                port = int(lines[0])
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                        data = json.loads(resp.read())
                    return proc, data["webSocketDebuggerUrl"]
                except Exception:
                    pass
        if proc.poll() is not None:
            raise RuntimeError("Chromium exited before publishing DevTools")
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("Chromium did not publish DevTools in time")


def stop_chromium(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--login", required=True, help="exact Tailscale-User-Login value")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--headed", action="store_true", help="use a visible window (needs DISPLAY)")
    args = parser.parse_args(argv)
    origin = public_origin(args.public_origin)
    tmp = Path(tempfile.mkdtemp(prefix="hbh-demo-"))
    os.chmod(tmp, 0o700)
    config = ViewerConfig(public_origin=origin, tailscale_login=args.login, listen_port=args.port)
    demo_url = f"http://127.0.0.1:{args.port}/demo"
    lease = FileLease(tmp / "lease.json")
    binding_box = {}
    service = Service(config=config, lease=lease, resolver=lambda task: binding_box["binding"],
                      state_dir=tmp / "state", allow_loopback_http=True, local_demo=True, name="hbh-demo")
    service.start()
    proc = None
    # systemd/timeout send SIGTERM; turn it into the same clean shutdown as Ctrl-C.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        # Chromium cannot add the identity header, so this demo process alone also serves the
        # synthetic form on /local-demo to loopback clients. Production never enables that route.
        proc, ws_url = launch_chromium(args.chrome, tmp / "profile", f"http://127.0.0.1:{args.port}/local-demo",
                                       headless=not args.headed)
        binding_box["binding"] = SessionBinding(session_key="demo", cdp_endpoint=ws_url,
                                                connection_id=SessionBinding.connection_hash(ws_url))
        time.sleep(1.0)
        result = service.call(lambda m: m.create("demo", f"http://127.0.0.1:{args.port}", "Demo: finish the synthetic form", None))
        print(json.dumps({k: v for k, v in result.items() if k != "cdp_endpoint"}, indent=2), flush=True)
        print("\nOpen the link on the owner's phone through Tailscale Serve. Ctrl-C to stop.", file=sys.stderr)
        while True:
            time.sleep(5)
            status = service.call(lambda m: m.status(result["handover_id"], "demo"))
            print(json.dumps({k: status.get(k) for k in ("state", "controller", "human_connected", "viewport_restored",
                                                          "cycles", "agent_may_continue", "expires_in_seconds")}), file=sys.stderr)
            if status["state"] in ("returned", "finished", "cancelled", "expired", "failed"):
                break
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        if proc is not None:
            stop_chromium(proc)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
