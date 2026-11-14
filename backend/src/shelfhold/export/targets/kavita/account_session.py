"""The admin account's own session, for the one case where the dedicated key is gone (M3 L30).

`detach --full --key-gone`: the key was deleted by hand, so every call that carries it answers 401
and the library settings could never be given back. The settings go back through a login of the
admin account (its password is given for this command only, never kept), on the same two library
routes as the key's client and nothing else. The session answers what `restore_library_settings`
asks of a client: `list_libraries` and `replace_exclude_patterns`.
"""

import json
from collections.abc import Sequence
from typing import Any

from shelfhold.export.application.library_settings import InstanceChangedError
from shelfhold.export.domain.kavita_instance import instance_fingerprint
from shelfhold.export.targets.kavita import dto, routes
from shelfhold.export.targets.kavita.errors import ProtocolError, TargetError
from shelfhold.infrastructure.fetcher import GuardedFetcher

__all__ = ["KavitaAccountSession"]


class KavitaAccountSession:
    def __init__(
        self,
        fetcher: GuardedFetcher,
        base_url: str,
        username: str,
        password: str,
        *,
        stored_fingerprint: str | None = None,
    ) -> None:
        self._stored = stored_fingerprint  # the instance the target was linked to (None: unknown)
        self._fetcher = fetcher
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None

    def __repr__(self) -> str:  # never the password
        return f"KavitaAccountSession({self._base!r})"

    async def _headers(self) -> dict[str, str]:
        if self._token is None:
            login = await self._fetcher.post(
                self._base + routes.LOGIN.path,
                json={"username": self._username, "password": self._password},
            )
            token = _token(login.body) if login.status == 200 else None
            if token is None:
                raise TargetError("Kavita refused the account's credentials")
            self._token = token
            await self._check_instance()
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    async def fingerprint(self) -> str | None:
        """URL + installation id + creation date of the oldest account (EF-EXP-039)."""
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        info = await self._fetcher.get(self._base + routes.SERVER_INFO.path, headers=headers)
        loaded = json.loads(info.body) if info.status == 200 else {}
        install = loaded.get("installId") if isinstance(loaded, dict) else None
        created: str | None
        try:
            users = await self._fetcher.get(
                self._base + routes.USERS.path, headers=headers, params={"includePending": "true"}
            )
            dates = [
                m.created for m in map(dto.Member.from_json, json.loads(users.body)) if m.created
            ]
            created = min(dates) if dates else None
        except Exception:  # unreadable accounts: the fingerprint cannot match a stored one
            created = "unreadable"
        return instance_fingerprint(
            self._base, install if isinstance(install, str) else None, created
        )

    async def _check_instance(self) -> None:
        """Before anything is written: the same Kavita as the one the target was linked to. A
        stored fingerprint that no longer matches (a reinstall hands the library ids out again
        from 1) stops it."""
        if self._stored is not None and await self.fingerprint() != self._stored:
            raise InstanceChangedError("Kavita is not the instance this target was linked to")

    async def list_libraries(self) -> list[dict[str, Any]]:
        response = await self._fetcher.get(
            self._base + routes.LIBRARIES.path, headers=await self._headers()
        )
        data = json.loads(response.body) if response.status == 200 else None
        if not isinstance(data, list):
            raise ProtocolError("libraries did not answer a list")
        return [item for item in data if isinstance(item, dict)]

    async def replace_exclude_patterns(
        self, library_id: int, patterns: Sequence[str]
    ) -> dto.LibraryUpdate:
        current = next(
            (lib for lib in await self.list_libraries() if lib.get("id") == int(library_id)), None
        )
        if current is None:
            raise ProtocolError(f"library {library_id} is not in Kavita")
        before = tuple(current.get("excludePatterns") or ())
        if tuple(patterns) == before:
            return dto.LibraryUpdate(int(library_id), False, before)
        response = await self._fetcher.post(
            self._base + routes.LIBRARY_UPDATE.path,
            json=dto.update_dto(current, exclude_patterns=patterns),
            headers=await self._headers(),
        )
        if response.status != 200:
            raise ProtocolError(f"the library update answered {response.status}")
        return dto.LibraryUpdate(int(library_id), True, before)

    async def aclose(self) -> None:
        await self._fetcher.aclose()


def _token(body: bytes) -> str | None:
    try:
        loaded = json.loads(body)
    except ValueError:
        return None
    token = loaded.get("token") if isinstance(loaded, dict) else None
    return token if isinstance(token, str) and token else None
