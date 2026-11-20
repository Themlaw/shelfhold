"""« Soutenir l'auteur » (EF-BIB-032, M4): official links with provenance.

The links a provider hands over (AniList `externalLinks`, MangaUpdates) are kept as
`external_link` rows, each with its provenance: which provider said so, and when. A link is
third-party data (ADR-0047): it is shown only when it is plain `https`, never `javascript:` nor
`data:`, and it is never affiliated.

The rule against affiliation is closed and written here: every query parameter of
`AFFILIATE_PARAMS` (and any `utm_*`) is removed before a URL is kept, the fragment is dropped, and
a URL that carries credentials or names no real host is refused. A URL that is not `https` is
refused whole. Nothing is ever stored or served that this module has not cleaned.

A community binding is never paused by itself: when an official Edition exists in the language
the household follows the Work in, the admin is *proposed* the suspension, and decides.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from shelfhold_sdk.metadata import IdentifyResult, Provenance

from shelfhold.catalog.application.queries import ReaderScope, visible_edition_ids
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.infrastructure.schema.catalog import EXTERNAL_LINK_KINDS

__all__ = [
    "AFFILIATE_PARAMS",
    "LINK_PROVIDERS",
    "MAX_LINKS_PER_PROVIDER",
    "OfficialLink",
    "SupportAuthor",
    "is_safe_url",
    "official_links",
    "pause_community_binding",
    "pause_community_binding_of",
    "read_official_links",
    "sanitize_url",
    "store_official_links",
    "strip_affiliate_params",
    "suggest_pausing_community_binding",
    "support_author",
]

# The providers whose links are kept (a closed list: a provider that only points at an
# identifier page, such as a correspondence, is not a place to read or buy).
LINK_PROVIDERS: frozenset[str] = frozenset({"anilist", "mangaupdates"})
MAX_LINKS_PER_PROVIDER = 20
MAX_URL_LENGTH = 2048

# Query parameters that exist only to track or pay a referrer. They are removed, never stored.
AFFILIATE_PARAMS: frozenset[str] = frozenset(
    {
        "aff", "aff_id", "affid", "affiliate", "affiliate_id", "associate", "associates",
        "ref", "referrer", "referer", "tag", "partner", "partner_id", "adid", "ad_id", "camp",
        "creative", "creativeasin", "creative_asin", "linkcode", "link_code", "linkid",
        "link_id", "ascsubtag", "subtag", "irgwc", "irclickid", "clickid", "click_id", "icid",
        "gclid", "fbclid", "msclkid",
    }
)  # fmt: skip


def _is_affiliate_param(name: str) -> bool:
    lowered = name.strip().lower()
    return lowered in AFFILIATE_PARAMS or lowered.startswith("utm_")


def strip_affiliate_params(url: str) -> str:
    """`url` without any tracking or affiliate query parameter nor fragment (the rest is kept)."""
    parts = urlsplit(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_affiliate_param(k)
    ]
    return urlunsplit(parts._replace(query=urlencode(kept), fragment=""))


def _real_host(host: str | None) -> bool:
    """A name with a dot: no bare name (`localhost`, an intranet host), no IP address."""
    if not host or "." not in host.strip("."):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def is_safe_url(url: object) -> bool:
    """ADR-0047: plain `https` only, never `javascript:` nor `data:` nor any other scheme, with
    no credentials in it and a real host."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        return False
    if url != url.strip() or any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        return False  # a space or a control character: not a link a person typed
    try:
        parts = urlsplit(url)
        host, _port = parts.hostname, parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and _real_host(host)
    )


def sanitize_url(url: object) -> str | None:
    """A safe link without affiliation, or None when it cannot be made one."""
    if not isinstance(url, str) or not is_safe_url(url):
        return None
    return strip_affiliate_params(url)


@dataclass(frozen=True)
class OfficialLink:
    """One official read/buy/licensed link and where it comes from."""

    url: str
    provider: str
    kind: str = "official_read"
    lang: str = "und"
    provenance: Provenance | None = None
    fetched_at: datetime | None = None


def _lang_of(value: object) -> str:
    if (
        isinstance(value, str)
        and 0 < len(value) <= 35
        and all(c.isascii() and (c.isalnum() or c == "-") for c in value)
        and not value.startswith("-")
        and not value.endswith("-")
    ):
        return value
    return "und"


def official_links(result: IdentifyResult, *, edition_id: str | None = None) -> list[OfficialLink]:
    """The links an identification handed over, cleaned. With `edition_id`, only the ones that
    are about that Edition. A relation that carries `correspondences` is an identifier page, not
    a link to read or buy; a provider outside `LINK_PROVIDERS` is not asked to name one."""
    found: dict[tuple[str, str], OfficialLink] = {}
    per_provider: dict[str, int] = {}
    for relation in result.relations:
        if relation.kind not in ("official_link", "licensed_in"):
            continue
        provenance = relation.provenance
        provider = provenance.provider
        if provider not in LINK_PROVIDERS or "correspondences" in relation.data:
            continue
        subject = relation.subject
        if edition_id is not None and (
            subject.entity_type != "edition" or subject.entity_id not in (None, edition_id)
        ):
            continue
        url = sanitize_url(relation.data.get("url"))
        if url is None or (provider, url) in found:
            continue
        if per_provider.get(provider, 0) >= MAX_LINKS_PER_PROVIDER:
            continue
        per_provider[provider] = per_provider.get(provider, 0) + 1
        kind = relation.data.get("kind")
        found[(provider, url)] = OfficialLink(
            url=url,
            provider=provider,
            kind=(
                "licensed_in"
                if relation.kind == "licensed_in"
                else kind
                if kind in EXTERNAL_LINK_KINDS
                else "official_read"
            ),
            lang=_lang_of(relation.data.get("lang")),
            provenance=provenance,
            fetched_at=provenance.fetched_at,
        )
    return list(found.values())


def store_official_links(
    connection: sa.Connection,
    edition_id: str,
    links: list[OfficialLink],
    *,
    provider: str,
    now: datetime,
) -> int:
    """Replace what `provider` said about the Edition by `links` (a fresh answer replaces the
    old one; another provider's rows stay). Returns the number of rows kept."""
    table = catalog.external_link
    connection.execute(
        sa.delete(table).where(
            table.c.entity_type == "edition",
            table.c.entity_id == edition_id,
            table.c.provider == provider,
        )
    )
    rows = [
        {
            "id": new_ulid(),
            "entity_type": "edition",
            "entity_id": edition_id,
            "kind": link.kind,
            "url": link.url,
            "lang": link.lang,
            "provider": provider,
            "fetched_at": link.fetched_at or now,
        }
        for link in links
        if link.provider == provider and is_safe_url(link.url)
    ]
    if rows:
        connection.execute(sa.insert(table), rows)
    return len(rows)


def read_official_links(connection: sa.Connection, edition_id: str) -> list[OfficialLink]:
    """The links stored for an Edition, cleaned again on the way out (a row written by any other
    door is never trusted)."""
    table = catalog.external_link
    rows = connection.execute(
        sa.select(table)
        .where(table.c.entity_type == "edition", table.c.entity_id == edition_id)
        .order_by(table.c.provider, table.c.kind, table.c.url)
    ).mappings()
    found: list[OfficialLink] = []
    for row in rows:
        url = sanitize_url(row["url"])
        if url is not None:
            found.append(
                OfficialLink(
                    url=url,
                    provider=str(row["provider"]),
                    kind=str(row["kind"]),
                    lang=str(row["lang"]),
                    fetched_at=row["fetched_at"],
                )
            )
    return found


def suggest_pausing_community_binding(connection: sa.Connection, *, edition_id: str) -> list[str]:
    """The ids of the community bindings the admin may be *proposed* to pause (nothing is written).

    `edition_id` is an official Edition. A binding qualifies when it is active, on a
    non-official Edition of the same Work, in the same language: the language the household
    follows the Work in, now offered officially."""
    edition, binding = catalog.edition, acquisition.source_binding
    official = connection.execute(
        sa.select(edition.c.work_id, edition.c.lang).where(
            edition.c.id == edition_id, edition.c.is_official.is_(True)
        )
    ).first()
    if official is None:
        return []
    rows = connection.execute(
        sa.select(binding.c.id)
        .select_from(binding.join(edition, edition.c.id == binding.c.edition_id))
        .where(
            edition.c.work_id == official.work_id,
            edition.c.lang == official.lang,
            edition.c.is_official.is_(False),
            binding.c.active.is_(True),
        )
        .order_by(binding.c.id)
    ).scalars()
    return [str(r) for r in rows]


@dataclass(frozen=True)
class SupportAuthor:
    """What the « Soutenir l'auteur » section shows for an Edition."""

    edition_id: str
    links: list[OfficialLink]
    pause_suggestions: list[str]  # binding ids; only ever filled for an admin


def _official_siblings(connection: sa.Connection, edition_id: str) -> list[str]:
    """The official Editions that could replace the community binding of this Edition: the
    Edition itself when it is official, else the official ones of its Work in its language."""
    edition = catalog.edition
    row = connection.execute(
        sa.select(edition.c.work_id, edition.c.lang, edition.c.is_official).where(
            edition.c.id == edition_id
        )
    ).first()
    if row is None:
        return []
    if row.is_official:
        return [edition_id]
    return [
        str(i)
        for i in connection.execute(
            sa.select(edition.c.id).where(
                edition.c.work_id == row.work_id,
                edition.c.lang == row.lang,
                edition.c.is_official.is_(True),
            )
        ).scalars()
    ]


def pending_pause_suggestions(connection: sa.Connection, edition_id: str) -> list[str]:
    """The proposals for an Edition (an admin's view: no reader filter here)."""
    found: list[str] = []
    for official in _official_siblings(connection, edition_id):
        found += suggest_pausing_community_binding(connection, edition_id=official)
    return sorted(set(found))


def pause_community_binding(connection: sa.Connection, edition_id: str, binding_id: str) -> bool:
    """The admin's decision: pause one binding that was proposed for this Edition. A binding that
    is not among the proposals (another Work's, an official one, already paused) is refused:
    False, nothing written."""
    if binding_id not in pending_pause_suggestions(connection, edition_id):
        return False
    binding = acquisition.source_binding
    connection.execute(
        sa.update(binding)
        .where(binding.c.id == binding_id, binding.c.active.is_(True))
        .values(active=False, disabled_reason="manual")
    )
    return True


def pause_community_binding_of(engine: sa.Engine, edition_id: str, binding_id: str) -> bool:
    """`pause_community_binding` in its own write transaction (the door of the route)."""
    with write_transaction(engine) as connection:
        return pause_community_binding(connection, edition_id, binding_id)


def support_author(
    connection: sa.Connection, scope: ReaderScope, edition_id: str, *, is_admin: bool
) -> SupportAuthor | None:
    """The section for one Edition, or None when the reader may not see it (the same answer as an
    Edition that does not exist: no oracle for a child, a member or a guessed identifier). The
    proposal to pause a binding is the admin's alone."""
    if edition_id not in visible_edition_ids(connection, scope, [edition_id]):
        return None
    return SupportAuthor(
        edition_id=edition_id,
        links=read_official_links(connection, edition_id),
        pause_suggestions=pending_pause_suggestions(connection, edition_id) if is_admin else [],
    )
