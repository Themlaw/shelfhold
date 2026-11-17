"""The configuration facade, opened when a command needs it (it may create the keyring).

Shared by the doors that renew a credential or revoke a key (`ops/wiring.py`) and by the job that
revokes the dedicated key of a detached target (M4 L34): one instance per process each, the
facade itself is cached.
"""

from functools import cache

from shelfhold.config import Settings
from shelfhold.headless import HeadlessAdmin
from shelfhold.headless import wiring as headless_wiring

__all__ = ["LazyHeadless"]


class LazyHeadless:
    """The configuration facade, opened when a credential is renewed (it may create the keyring)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @cache  # noqa: B019 (one instance per process)
    def _admin(self) -> HeadlessAdmin:
        return headless_wiring.open_headless(self._settings)

    def target_rotate_key(self, *, target: str, password: str) -> object:
        return self._admin().target_rotate_key(target=target, password=password)

    def target_find_key(self, *, target: str, password: str | None) -> str | None:
        return self._admin().target_find_key(target=target, password=password)

    def target_revoke_key_id(self, *, target: str, password: str | None, key_id: str) -> str:
        return self._admin().target_revoke_key_id(target=target, password=password, key_id=key_id)

    def source_set_secret(self, secret: str) -> object:
        return self._admin().source_set_secret(secret)

    def source_recheck(self) -> object:
        return self._admin().source_recheck()

    def source_reconnect(self) -> object:
        return self._admin().source_reconnect()
