"""Configuration validation without leaking proxy credentials."""
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


def default_config():
    return config_home() / "opencode-zen-rotator" / "config.json"


def validate_route(route):
    if route == "direct":
        return route
    try:
        parsed = urlsplit(route)
        if (parsed.scheme not in {"socks5", "socks5h", "http", "https"}
                or not parsed.hostname or not parsed.port
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Invalid route: use direct or a proxy URL with a host and port") from None
    return route


@dataclass(frozen=True)
class Settings:
    routes: tuple[str, ...]
    token: str
    port: int = 18998
    max_body_bytes: int = 25_000_000
    cooldown: float = 60
    transport_cooldown: float = 30
    max_cooldown: float = 3600

    def __post_init__(self):
        if not self.routes or len(set(self.routes)) != len(self.routes):
            raise ValueError("Provide at least one route, without duplicates")
        for route in self.routes:
            validate_route(route)
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise ValueError("Set a local access token of at least 32 characters")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1024 <= self.port <= 65535:
            raise ValueError("Port must be an integer between 1024 and 65535")
        if self.max_body_bytes < 1:
            raise ValueError("Body limit must be positive")

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text())
            if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
                raise ValueError()
            return cls(routes=tuple(data["routes"]), token=data.get("token", ""), port=data.get("port", 18998))
        except OSError:
            raise ValueError("Cannot read configuration; run install.py or specify --config") from None
        except (ValueError, TypeError, KeyError):
            raise ValueError("Invalid configuration; check routes, token and port locally") from None
