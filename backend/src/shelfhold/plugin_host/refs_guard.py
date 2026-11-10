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

from pydantic import BaseModel

from shelfhold.infrastructure.fetcher.refs import SecretInRef, check_ref, rewrite_ref
from shelfhold.infrastructure.logging import known_secret_values
from shelfhold.infrastructure.secret_text import contains_secret
from shelfhold.plugin_host.errors import RefHasSecret

__all__ = ["RefsGuard", "ScreenedService", "screened", "values_of"]

# Identifiers the host stores and compares: the full check (known values, userinfo, and a token or
# a key-named parameter nobody registered).
_STORED: Final = frozenset({"remote_id", "remote_unit_id", "memo", "covers", "ticket", "id"})
# Addresses that are used and not stored (a file to download, an image): known values and
# userinfo only; a signature in the query of a signed address is the source's business.
_ADDRESSES: Final = frozenset({"url", "href", "referer", "token", "bounds", "locator"})


class RefsGuard:
    def __init__(
        self,
        known: Callable[[], Iterable[str]],
        *,
        template: str | Callable[[], str | None] | None = None,
    ) -> None:
        self._known = known
        self._declared = template  # read at each call: the adapter may declare it after start

    @property
    def _template(self) -> str | None:
        declared = self._declared
        return declared() if callable(declared) else declared

    def ref(self, text: str, *, field: str, patterns: bool = True) -> str:
        """`text` free of secrets: as it is, or rewritten to the template; else `RefHasSecret`."""
        known = tuple(self._known())
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
            items = [self._walk(v, field, kind) for v in value]
            if all(a is b for a, b in zip(items, value, strict=True)):
                return value
            return type(value)(items) if isinstance(value, tuple) else items
        return value

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
        known = tuple(self._known())
        if contains_secret(text, known):
            raise RefHasSecret(f"{field or 'value'}: the value holds a secret (secret)")
        return text


def values_of(fetcher: Any) -> Callable[[], frozenset[str]]:
    """The values to check references against: every registered secret, and what this Fetcher
    says it injects (`secret_values()`, when it has one)."""

    def known() -> frozenset[str]:
        own = getattr(fetcher, "secret_values", None)
        return known_secret_values() | (own() if callable(own) else frozenset())

    return known


def screened(
    service: Any,
    known: Callable[[], Iterable[str]],
    template: str | Callable[[], str | None] | None = None,
) -> Any:
    """`service` whose every answer goes through the guard."""
    return ScreenedService(service, RefsGuard(known, template=template))


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
                return guard.screen(await attr(*args, **kwargs))

            return guarded
        if inspect.ismethod(
            attr
        ):  # a plain method (`migrate_ref`...) answers through the guard too

            @functools.wraps(attr)
            def guarded_sync(*args: Any, **kwargs: Any) -> Any:
                return guard.screen(attr(*args, **kwargs))

            return guarded_sync
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.__dict__["_inner"], name, value)
