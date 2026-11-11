"""What a source hands back is checked for secrets before the host keeps any of it (EF-SRC-035).

The Fetcher knows the values it injects (EF-SRC-023). Every reference a source returns
(`remote_id`, `remote_unit_id`, the `memo` it asks to have kept, a file URL, a range) is looked at
here: one that holds such a value, or a `user:password@` part, is **refused** with `RefHasSecret`
(a contract error: the plugin is not conforming, the service pauses on a schema problem), or
**rewritten** when the adapter declared a template (`AuthApiKey.ref_template`, the `{secret}`
placeholder in place of the key segment). Rewriting is what makes a rotation of the key change no
identifier. `infrastructure.write_guard` is the second line, at the database.

A rewritten reference is sent back to the source as it is stored: the Fetcher puts the key where
`{secret}` stands when the request leaves (`AuthInjectingFetcher`).
"""

import functools
import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final, cast
from urllib.parse import quote

from pydantic import BaseModel

from shelfhold.infrastructure.fetcher.refs import SecretInRef, check_ref, rewrite_ref
from shelfhold.infrastructure.logging import known_secret_index
from shelfhold.infrastructure.secret_text import SecretIndex, contains_secret
from shelfhold.plugin_host.errors import RefHasSecret
from shelfhold.sdk.refs import SECRET_PLACEHOLDER

__all__ = ["RefsGuard", "ScreenedService", "screened", "secret_of", "values_of"]

# Identifiers the host stores and compares: the full check (known values, userinfo, and a token or
# a key-named parameter nobody registered).
_STORED: Final = frozenset({"remote_id", "remote_unit_id", "memo", "covers", "ticket", "id"})
# Addresses that are used and not stored (a file to download, an image): known values and
# userinfo only; a signature in the query of a signed address is the source's business.
_ADDRESSES: Final = frozenset({"url", "href", "referer", "token", "bounds", "locator"})


class RefsGuard:
    def __init__(
        self,
        known: "Callable[[], Iterable[str] | SecretIndex]",
        *,
        template: str | Callable[[], str | None] | None = None,
        secret: Callable[[], str | None] | None = None,
    ) -> None:
        self._known = known
        self._secret = secret  # the key `{secret}` stands for, to give it back to the plugin
        self._declared = template  # read at each call: the adapter may declare it after start
        self.quarantined: list[str] = []  # the closed reasons of the items left out of a list

    @property
    def _template(self) -> str | None:
        declared = self._declared
        return declared() if callable(declared) else declared

    def ref(self, text: str, *, field: str, patterns: bool = True) -> str:
        """`text` free of secrets: as it is, or rewritten to the template; else `RefHasSecret`."""
        known = self._known()
        template = self._template
        try:
            if template is None:
                return check_ref(text, known, patterns=patterns)
            return rewrite_ref(text, known, template, patterns=patterns)
        except SecretInRef as error:
            # the field and the closed reason, never the reference
            raise RefHasSecret(f"{field}: the reference holds a secret ({error.reason})") from None

    def screen[T](self, value: T) -> T:
        """`value` with every reference in it screened (models, lists, dictionaries)."""
        return cast("T", self._walk(value, "", None))

    def restore[T](self, value: T) -> T:
        """What goes back to the plugin: `{secret}` is the key again (percent-encoded, as the
        Fetcher writes it into a URL). The stored form keeps the placeholder; the plugin must
        find the reference it first handed over, or it cannot find its own URL (review of L04)."""
        key = self._secret() if self._secret is not None else None
        if not key:
            return value
        return cast("T", self._restore(value, quote(key, safe="")))

    def _restore(self, value: Any, key: str) -> Any:
        if isinstance(value, str):
            return value.replace(SECRET_PLACEHOLDER, key) if SECRET_PLACEHOLDER in value else value
        if isinstance(value, BaseModel):
            changes = {}
            for name in type(value).model_fields:
                inner = getattr(value, name)
                done = self._restore(inner, key)
                if done is not inner:
                    changes[name] = done
            return value.model_copy(update=changes) if changes else value
        if isinstance(value, Mapping):
            out = {k: self._restore(v, key) for k, v in value.items()}
            return out if any(out[k] is not value[k] for k in out) else value
        if isinstance(value, list | tuple):
            items = [self._restore(v, key) for v in value]
            if all(a is b for a, b in zip(items, value, strict=True)):
                return value
            return tuple(items) if isinstance(value, tuple) else items
        return value

    def _walk(self, value: Any, field: str, kind: str | None) -> Any:
        if isinstance(value, str):
            return self._string(value, field, kind)
        if isinstance(value, BaseModel):
            changes: dict[str, Any] = {}
            for name in type(value).model_fields:
                inner = getattr(value, name)
                walked = self._walk(inner, name, self._kind(name, kind))
                if walked is not inner:
                    changes[name] = walked
            return value.model_copy(update=changes) if changes else value
        if isinstance(value, Mapping):
            out = {k: self._walk(v, field, kind) for k, v in value.items()}
            return out if any(out[k] is not value[k] for k in out) else value
        if isinstance(value, list | tuple):
            items: list[Any] = []
            for v in value:
                try:
                    items.append(self._walk(v, field, kind))
                except RefHasSecret as error:
                    if not isinstance(v, BaseModel):
                        raise
                    self.quarantined.append(str(error))  # one Unit out, the others go on
            if len(items) == len(value) and all(a is b for a, b in zip(items, value, strict=True)):
                return value
            return type(value)(items) if isinstance(value, tuple) else items
        return value

    def take_quarantined(self) -> list[str]:
        """The reasons of the items left out since the last call (the field and a closed reason)."""
        taken, self.quarantined = self.quarantined, []
        return taken

    @staticmethod
    def _kind(name: str, inherited: str | None) -> str | None:
        if name in _STORED:
            return "stored"
        if name in _ADDRESSES:
            return "address"
        return inherited  # the inside of `memo` or `covers` keeps its kind

    def _string(self, text: str, field: str, kind: str | None) -> str:
        if kind == "stored":
            return self.ref(text, field=field)
        if kind == "address":
            return self.ref(text, field=field, patterns=False)
        if contains_secret(text, self._known()):
            raise RefHasSecret(f"{field or 'value'}: the value holds a secret (secret)")
        return text


def values_of(fetcher: Any) -> Callable[[], SecretIndex]:
    """The values to check references against: every registered secret, and what this Fetcher
    says it injects (`secret_values()`, when it has one). The index is rebuilt only when either
    changes."""
    last: list[Any] = [None, None, SecretIndex()]  # registry index, injected values, result

    def known() -> SecretIndex:
        base = known_secret_index()
        own = getattr(fetcher, "secret_values", None)
        extra: frozenset[str] = own() if callable(own) else frozenset()
        if last[0] is not base or last[1] != extra:
            merged = SecretIndex(base.values | extra) if extra - base.values else base
            last[:] = [base, extra, merged]
        result: SecretIndex = last[2]
        return result

    return known


def secret_of(fetcher: Any) -> Callable[[], str | None]:
    """The key a `{secret}` placeholder stands for, when the Fetcher injects one in a URL."""

    def key() -> str | None:
        own = getattr(fetcher, "ref_secret", None)
        value = own() if callable(own) else None
        return value if isinstance(value, str) else None

    return key


def screened(
    service: Any,
    known: "Callable[[], Iterable[str] | SecretIndex]",
    template: str | Callable[[], str | None] | None = None,
    secret: Callable[[], str | None] | None = None,
) -> Any:
    """`service` whose every answer goes through the guard (and whose every argument gets the key
    back in place of `{secret}`)."""
    return ScreenedService(service, RefsGuard(known, template=template, secret=secret))


class ScreenedService:
    """A connected service as the host sees it: each method returns what the guard let through.
    The rest (manifest, capabilities) is the service's own."""

    def __init__(self, inner: Any, guard: RefsGuard) -> None:
        self.__dict__["_inner"] = inner
        self.__dict__["_guard"] = guard

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.__dict__["_inner"], name)
        guard: RefsGuard = self.__dict__["_guard"]
        if inspect.iscoroutinefunction(attr):

            @functools.wraps(attr)
            async def guarded(*args: Any, **kwargs: Any) -> Any:
                args, kwargs = guard.restore((args, kwargs))
                return guard.screen(await attr(*args, **kwargs))

            return guarded
        if inspect.ismethod(
            attr
        ):  # a plain method (`migrate_ref`...) answers through the guard too

            @functools.wraps(attr)
            def guarded_sync(*args: Any, **kwargs: Any) -> Any:
                args, kwargs = guard.restore((args, kwargs))
                return guard.screen(attr(*args, **kwargs))

            return guarded_sync
        return attr

    def take_quarantined(self) -> list[str]:
        guard: RefsGuard = self.__dict__["_guard"]
        return guard.take_quarantined()

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.__dict__["_inner"], name, value)
