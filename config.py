"""Protected runtime metadata for the viewer listener. Never stored in this project.

``~/.config/homelab-browser-handover/viewer.json`` (mode 0600), for example:

    {"public_origin": "https://your-server.your-tailnet.ts.net:8444",
     "tailscale_login": "<owner tailnet login>",
     "listen_port": 8791}

The login is the exact value Tailscale Serve places in ``Tailscale-User-Login``.
Nothing here is ever echoed to the model, a page or a log.
"""
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PATH = Path.home() / ".config" / "homelab-browser-handover" / "viewer.json"
DEFAULT_PORT = 8791


class ConfigError(RuntimeError):
    pass


def public_origin(value: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("public_origin must be a string")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError("public_origin must be scheme://host[:port] only")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost"):
        raise ConfigError("plain http public_origin is only allowed for loopback demos")
    return f"{parts.scheme}://{parts.netloc}".lower()


@dataclass(frozen=True)
class ViewerConfig:
    public_origin: str
    tailscale_login: str
    listen_port: int
    listen_host: str = "127.0.0.1"
    state_dir: Path | None = None

    @property
    def secure_cookies(self) -> bool:
        return self.public_origin.startswith("https://")


def load(path: Path = DEFAULT_PATH) -> ViewerConfig:
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise ConfigError("viewer.json must not be group/world readable")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except ConfigError:
        raise
    except FileNotFoundError as exc:
        raise ConfigError("viewer.json is missing") from exc
    except (OSError, ValueError) as exc:
        raise ConfigError("viewer.json is unreadable") from exc
    return from_dict(data)


def from_dict(data) -> ViewerConfig:
    if not isinstance(data, dict):
        raise ConfigError("viewer.json must contain an object")
    login = data.get("tailscale_login")
    if not isinstance(login, str) or not re.fullmatch(r"[^\s\x00-\x1f]{3,200}", login):
        raise ConfigError("tailscale_login is not configured")
    port = data.get("listen_port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ConfigError("listen_port must be an integer between 1024 and 65535")
    host = data.get("listen_host", "127.0.0.1")
    if host not in ("127.0.0.1", "::1"):
        raise ConfigError("listen_host must stay on loopback")
    state_dir = data.get("state_dir")
    return ViewerConfig(public_origin=public_origin(data.get("public_origin")), tailscale_login=login,
                        listen_port=port, listen_host=host,
                        state_dir=Path(state_dir) if isinstance(state_dir, str) and state_dir else None)
