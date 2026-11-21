"""Access: two tokens in the gateway home directory, Host header check, signed dashboard cookie."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_router.clock import Clock
from agent_router.settings_store import SqliteSettings

CLIENT_FILE = "client.token"
ADMIN_FILE = "admin.token"
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})

Role = Literal["client", "admin"]


@dataclass(frozen=True)
class Tokens:
    client: str
    admin: str

    def __repr__(self) -> str:  # token values must not leak into logs or traces
        return "Tokens(client=***, admin=***)"


def generate_token() -> str:
    return secrets.token_urlsafe(32)


class TokenFileError(ValueError):
    """A token file exists but holds no token; init never overwrites it."""


def _read(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise TokenFileError(
            f"empty token file: {path}; delete it and run agent-router init to create a new one"
        )
    return value


def _create(path: Path) -> None:
    """Creates the token file if missing; leaves an existing one untouched."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(generate_token() + "\n")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def ensure_tokens(home: Path) -> Tokens:
    """Creates client.token and admin.token if missing; never overwrites existing ones."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    _create(home / CLIENT_FILE)
    _create(home / ADMIN_FILE)
    return load_tokens(home)


def load_tokens(home: Path) -> Tokens:
    home = Path(home)
    return Tokens(client=_read(home / CLIENT_FILE), admin=_read(home / ADMIN_FILE))


def role_for(token: str, tokens: Tokens) -> Role | None:
    """Role for a token; constant-time comparison, both tokens are always checked."""
    if not isinstance(token, str) or not token:
        return None
    given = token.encode("utf-8")
    is_admin = hmac.compare_digest(given, tokens.admin.encode("utf-8"))
    is_client = hmac.compare_digest(given, tokens.client.encode("utf-8"))
    if is_admin:
        return "admin"
    if is_client:
        return "client"
    return None


def host_allowed(host_header: str | None) -> bool:
    """Host header name must be 127.0.0.1 or localhost; the port is ignored."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    name, sep, port = host.rpartition(":")
    if sep:
        if not port.isascii() or not port.isdigit():
            return False
        host = name
    return host in _ALLOWED_HOSTS


_PANEL_KEY_INFO = b"agent-router/panel/v1"


class PanelAuth:
    """Signed dashboard cookie: key derived from the admin token, epoch in SQLite for revocation."""

    def __init__(self, admin_token: str, settings: SqliteSettings, days: int, clock: Clock) -> None:
        self._key = hmac.new(admin_token.encode("utf-8"), _PANEL_KEY_INFO, hashlib.sha256).digest()
        self._settings = settings
        self._days = days
        self._clock = clock

    def _epoch(self) -> int:
        raw = self._settings.get("panel_epoch")
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def _sign(self, text: str) -> str:
        digest = hmac.new(self._key, text.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def issue(self) -> str:
        issued = int(self._clock.now().timestamp())
        epoch = self._epoch()
        return f"v1.{issued}.{epoch}.{self._sign(f'v1|{issued}|{epoch}')}"

    def _parts(self, cookie: str | None) -> tuple[int, int, str] | None:
        if not cookie:
            return None
        pieces = cookie.split(".")
        if len(pieces) != 4 or pieces[0] != "v1":
            return None
        try:
            issued = int(pieces[1])
            epoch = int(pieces[2])
        except ValueError:
            return None
        return issued, epoch, pieces[3]

    def valid(self, cookie: str | None) -> bool:
        parts = self._parts(cookie)
        if parts is None:
            return False
        issued, epoch, sig = parts
        expected = self._sign(f"v1|{issued}|{epoch}").encode("utf-8")
        if not hmac.compare_digest(sig.encode("utf-8"), expected):
            return False
        now = int(self._clock.now().timestamp())
        if issued > now or now - issued > self._days * 86400:
            return False
        return epoch == self._epoch()

    def csrf_for(self, cookie: str) -> str:
        """CSRF token for form protection; raises KeyError for an invalid cookie."""
        parts = self._parts(cookie)
        if parts is None:
            raise KeyError(cookie)
        issued, epoch, _ = parts
        return self._sign(f"csrf|{issued}|{epoch}")

    def csrf_ok(self, cookie: str | None, value: str | None) -> bool:
        if not cookie or not value or not self.valid(cookie):
            return False
        return hmac.compare_digest(value.encode("utf-8"), self.csrf_for(cookie).encode("utf-8"))

    def logout(self) -> None:
        """Bumps the epoch: cookies issued earlier stop validating."""
        self._settings.bump("panel_epoch")
