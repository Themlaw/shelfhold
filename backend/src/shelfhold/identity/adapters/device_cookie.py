"""Signed "this browser already logged in to this account" cookie (review PR #41, ENF-SEC-008).

It only lets its holder skip the per-account attempt counter (never the per-address one), so a
third party flooding an account cannot keep its owner out. HMAC-SHA256 under a random key kept in
`/data/device-cookie.key` (0600, created on first use); whoever reads `/data` owns the instance
anyway. Value: `<base64url payload>.<base64url mac>`, payload `{"u": <casefolded username>,
"iat": <unix seconds>}`; valid for one year.
"""

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from shelfhold.infrastructure.secrets.atomic import write_private

KEY_FILE = "device-cookie.key"
MAX_AGE = timedelta(days=365)
_MAX_VALUE = 512


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class DeviceCookieSigner:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / KEY_FILE
        self._key: bytes | None = None
        self._lock = threading.Lock()

    def _secret(self) -> bytes:
        with self._lock:
            if self._key is None:
                with contextlib.suppress(FileExistsError):  # another process made it first
                    write_private(self._path, secrets.token_bytes(32), overwrite=False)
                self._key = self._path.read_bytes()
            return self._key

    def _mac(self, payload: str) -> str:
        return _b64(hmac.new(self._secret(), payload.encode("ascii"), hashlib.sha256).digest())

    def issue(self, username: str, now: datetime) -> str:
        body = json.dumps({"u": username.casefold(), "iat": int(now.timestamp())})
        payload = _b64(body.encode("utf-8"))
        return f"{payload}.{self._mac(payload)}"

    def verify(self, value: str | None, username: str, now: datetime) -> bool:
        if not value or len(value) > _MAX_VALUE or value.count(".") != 1:
            return False
        payload, mac = value.split(".")
        try:
            if not hmac.compare_digest(mac.encode("ascii"), self._mac(payload).encode("ascii")):
                return False
            claims = json.loads(_unb64(payload))
        except (ValueError, UnicodeError):
            return False
        if not isinstance(claims, dict) or claims.get("u") != username.casefold():
            return False
        issued = claims.get("iat")
        if not isinstance(issued, int):
            return False
        age = now.timestamp() - issued
        return 0 <= age <= MAX_AGE.total_seconds()
