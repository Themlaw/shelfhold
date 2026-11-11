"""M2 L04 (EF-SRC-035, EF-SRC-023 ApiKey path/query): a remote reference never holds a secret.

Every credential here is built at run time (`synthetic_text`): no literal, so the scanner of the
repository has nothing to find. The registry of secret values is process-wide and is emptied around
each test.
"""

import json
import logging
import re
import string
from collections.abc import Iterator
from urllib.parse import quote, quote_plus, unquote

import pytest
import structlog
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shelfhold.infrastructure.fetcher.refs import (
    SECRET_PLACEHOLDER,
    SecretInRef,
    check_ref,
    rewrite_ref,
)
from shelfhold.infrastructure.logging import (
    configure_logging,
    redact_url,
    redact_value,
    register_secret_value,
    route_of,
)
from shelfhold.infrastructure.schema import metadata
from shelfhold.infrastructure.write_guard import (
    EXEMPT_TABLES,
    SecretInWriteError,
    check_write,
)
from shelfhold.plugin_host.errors import RefHasSecret
from shelfhold.plugin_host.refs_guard import RefsGuard
from shelfhold.sdk.refs import (
    FileAsset,
    ImageRequest,
    RangedPayload,
    RangeRef,
    SeriesRef,
    UnitInfo,
)
from tests.redaction_support import forget_secret_values, synthetic_text

pytestmark = pytest.mark.req("EF-SRC-035")

KEY = synthetic_text("key-a")
SPECIAL = synthetic_text("key-b", specials=True)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    forget_secret_values()
    yield
    forget_secret_values()


def _encodings(value: str) -> dict[str, str]:
    """The forms a credential takes once it sits in a URL."""
    return {
        "raw": value,
        "percent": quote(value, safe=""),
        "plus": quote_plus(value),
        "double": quote(quote(value, safe=""), safe=""),
        "lower_hex": re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), quote(value, safe="")),
        "slashes_kept": quote(value, safe="/"),
    }


def _decoded(text: str) -> list[str]:
    seen, current = [text], text
    for _ in range(4):
        current = unquote(current.replace("+", " ")) if "%" in current else current
        seen.append(current)
    return seen


def _holds(text: str, value: str) -> bool:
    return any(value in view or quote(value, safe="") in view for view in _decoded(text))


def _series(remote_id: str) -> SeriesRef:
    return SeriesRef(source_key="synthetic", remote_id=remote_id, title="Synthetic")


def _unit(remote_unit_id: str) -> UnitInfo:
    return UnitInfo(remote_unit_id=remote_unit_id, source_order=0)


# -- userinfo ----------------------------------------------------------------------------------

USERINFO = [
    "https://user:pw@host.example/series/1",
    "https://onlyname@host.example/series/1",
    "//user:pw@host.example/series/1",
    "https://user%3Apw%40extra@host.example/series/1",
    "https://host.example%40user:pw@evil.example/x",
    "user:pw@host.example/series/1",
]


@pytest.mark.parametrize("ref", USERINFO)
def test_userinfo_in_ref_rejected(ref: str) -> None:
    with pytest.raises(SecretInRef):
        check_ref(ref, ())
    guard = RefsGuard(lambda: ())
    with pytest.raises(RefHasSecret):
        guard.screen(_series(ref))
    with pytest.raises(RefHasSecret):
        guard.screen(_unit(ref))
    with pytest.raises(RefHasSecret):
        guard.screen(
            FileAsset(request=ImageRequest(url=ref), mime="application/epub+zip", filename="a.epub")
        )
    with pytest.raises(RefHasSecret):  # a template does not excuse a userinfo part
        RefsGuard(lambda: (), template=SECRET_PLACEHOLDER).screen(_series(ref))


@pytest.mark.parametrize(
    "ref", ["/series/12", "https://host.example/a@b", "ch-001", "isbn:9780000000002"]
)
def test_plain_refs_pass_the_guard(ref: str) -> None:
    assert check_ref(ref, ()) == ref
    assert RefsGuard(lambda: ()).screen(_series(ref)).remote_id == ref


# -- secret values in path, query and fragment ---------------------------------------------------

POSITIONS = {
    "path": "/api/opds/{}/series/12",
    "path_end": "/series/12/{}",
    "query": "/series/12?apiKey={}&page=2",
    "fragment": "/series/12#access_token={}",
    "absolute": "https://host.example/dl/{}/book.epub",
}


@pytest.mark.parametrize("encoding", sorted(_encodings(KEY)))
@pytest.mark.parametrize("position", sorted(POSITIONS))
@pytest.mark.parametrize("secret", [KEY, SPECIAL], ids=["plain", "special"])
def test_registered_secret_is_refused_without_a_template(
    secret: str, position: str, encoding: str
) -> None:
    register_secret_value(secret)
    ref = POSITIONS[position].format(_encodings(secret)[encoding])
    with pytest.raises(RefHasSecret):
        RefsGuard(lambda: (secret,)).screen(_series(ref))


@pytest.mark.parametrize("encoding", sorted(_encodings(KEY)))
@pytest.mark.parametrize("position", ["path", "path_end", "absolute"])
@pytest.mark.parametrize("secret", [KEY, SPECIAL], ids=["plain", "special"])
def test_declared_template_rewrites_the_key_segment(
    secret: str, position: str, encoding: str
) -> None:
    register_secret_value(secret)
    guard = RefsGuard(lambda: (secret,), template=SECRET_PLACEHOLDER)
    ref = POSITIONS[position].format(_encodings(secret)[encoding])
    out = guard.screen(_unit(ref)).remote_unit_id
    assert SECRET_PLACEHOLDER in out and not _holds(out, secret)
    assert rewrite_ref(ref, (secret,)) == out  # the same function, the same answer


def test_the_rewrite_is_the_same_whatever_the_key() -> None:
    """EF-SRC-035: a rotation changes no identifier."""
    first, second = synthetic_text("rot-1"), synthetic_text("rot-2", specials=True)
    ids = {
        rewrite_ref(f"/api/opds/{quote(k, safe='')}/series/12", (first, second))
        for k in (first, second)
    }
    assert ids == {f"/api/opds/{SECRET_PLACEHOLDER}/series/12"}


def test_a_rewrite_that_cannot_hide_the_key_is_a_refusal() -> None:
    """A form nobody registered (one character in percent form, the rest as is): the rewrite does
    not find it, and the check after it still sees the key once decoded."""
    odd = KEY[:3] + f"%{ord(KEY[3]):02X}" + KEY[4:]
    guard = RefsGuard(lambda: (KEY,), template=SECRET_PLACEHOLDER)
    assert KEY not in guard.screen(_series(f"/k/{KEY}/{KEY[:5]}")).remote_id
    with pytest.raises(RefHasSecret):
        guard.screen(_series(f"/k/{odd}/x"))


def test_screen_walks_every_reference_of_every_model() -> None:
    register_secret_value(KEY)
    guard = RefsGuard(lambda: (KEY,), template=SECRET_PLACEHOLDER)
    series = _series(f"/o/{KEY}/s")
    first, last = _unit(f"/o/{KEY}/1"), _unit(f"/o/{KEY}/9")
    target = RangeRef(series=series, first=first, last=last)
    cleaned = guard.screen(target)
    asset = FileAsset(
        request=ImageRequest(url=f"https://h.example/o/{KEY}/f.epub"), mime="a/b", filename="f"
    )
    ranged = guard.screen(
        RangedPayload(asset=asset, covers=[first.remote_unit_id], volume_closed=True)
    )
    dumped = cleaned.model_dump_json() + ranged.model_dump_json()
    assert KEY not in dumped and dumped.count(SECRET_PLACEHOLDER) >= 5
    memo = guard.screen(_series("/ok")).model_copy(update={"memo": {"u": f"/o/{KEY}/m"}})
    assert KEY not in guard.screen(memo).model_dump_json()


def test_sensitive_names_in_a_stored_reference_are_refused_even_when_unregistered() -> None:
    """A secret nobody registered still shows by its place: a key-named parameter, a key segment."""
    blob = synthetic_text("unregistered")
    for ref in (
        f"/series/1?token={blob}",
        f"/series/1?x=1&api_key={blob}",
        f"/series/1;jsessionid={blob}",
        f"/series/1#access_token={blob}",
        f"/api/opds/{blob}/series/1",
        f"/v1/token/{blob}/series/1",
        f"/x/{'eyJ' + blob[:12]}.{blob[:14]}.{blob[14:]}",
    ):
        with pytest.raises(SecretInRef):
            check_ref(ref, ())
    check_ref(f"/api/opds/{SECRET_PLACEHOLDER}/series/1", ())
    check_ref("/series/1?page=2&sort=asc", ())


# -- logs --------------------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-035", "ENF-OBS-001")
def test_log_redaction_covers_path_segments_and_query() -> None:
    register_secret_value(KEY)
    register_secret_value(SPECIAL)
    blob = synthetic_text("unregistered-2")
    for form in (*_encodings(KEY).values(), *_encodings(SPECIAL).values()):
        out = redact_url(f"https://host.example/api/opds/{form}/series/1?page=2")
        assert "host.example/api/opds/" in out and "page=2" in out
        for secret in (KEY, SPECIAL):
            assert not _holds(out, secret)
            assert not _holds(route_of(f"https://host.example/dl/{form}/f.epub?x=1"), secret)
            assert not _holds(str(redact_value(f"GET /dl/{form}/f.epub failed")), secret)
            assert not _holds(redact_url(f"https://h.example/{form}/x?k={form}"), secret)
    out = redact_url(f"https://user:{blob}@host.example/a?token={blob}&page=2#access_token={blob}")
    assert blob not in out and "page=2" in out and "host.example/a" in out
    for url in (
        f"https://host.example/api/opds/{blob}/series/1",
        f"https://host.example/x/eyJ{blob[:10]}.{blob[:12]}.{blob[12:]}/y",
        f"https://host.example/v1/apikey/{blob}/y",
        f"https://host.example/a?Signature={blob}&n=1",
        f"https://host.example/a#key={blob}",
    ):
        assert blob[:12] not in redact_url(url), url
    assert (
        redact_url("https://host.example/series/12?page=2#top")
        == "https://host.example/series/12?page=2#top"
    )


@pytest.mark.req("EF-SRC-035", "ENF-OBS-001")
def test_log_lines_hold_no_secret_from_a_url(capsys: pytest.CaptureFixture[str]) -> None:
    register_secret_value(SPECIAL)
    configure_logging(level="DEBUG", log_format="json")
    capsys.readouterr()
    try:
        log = structlog.get_logger("synthetic")
        for form in _encodings(SPECIAL).values():
            log.info(
                "fetch", url=f"https://host.example/api/opds/{form}/s?a=1", note=f"/x/{form}/y"
            )
            logging.getLogger("synthetic.std").warning("got %s", f"https://h.example/p/{form}?q=1")
        text = capsys.readouterr().out
    finally:
        root = logging.getLogger()
        for handler in [h for h in root.handlers if getattr(h, "_shelfhold_handler", False)]:
            root.removeHandler(handler)
        structlog.reset_defaults()
    assert text.count("\n") >= 12
    assert not _holds(text, SPECIAL)
    assert all(json.loads(line) for line in text.splitlines() if line.startswith("{"))


# -- the write guard ---------------------------------------------------------------------------


def test_every_table_is_guarded_unless_exempt_on_purpose() -> None:
    """A table nobody decided about is guarded; an exemption names its reason and a real table;
    the references, the journals and the payloads are never exempt."""
    assert set(EXEMPT_TABLES) <= set(metadata.tables), "an exemption names a table that is gone"
    assert all(EXEMPT_TABLES[t].strip() for t in EXEMPT_TABLES)  # each exemption has its reason
    for table in (
        "source_item", "unit_variant", "source_binding", "upstream_snapshot", "download_request",
        "audit", "event", "notification_pending", "fs_intent", "export_record", "http_cache",
        "metadata_cache", "kavita_access_audit",
    ):  # fmt: skip
        assert table in metadata.tables and table not in EXEMPT_TABLES, table
    with pytest.raises(SecretInWriteError):  # a table added later is guarded without a line here
        register_secret_value(KEY)
        check_write("INSERT INTO a_new_table (c) VALUES (?)", (f"/x/{KEY}",), many=False)


@pytest.mark.parametrize("encoding", sorted(_encodings(KEY)))
def test_write_guard_refuses_a_registered_secret_in_any_form(encoding: str) -> None:
    register_secret_value(SPECIAL)
    form = _encodings(SPECIAL)[encoding]
    insert = "INSERT INTO source_item (id, remote_id) VALUES (?, ?)"
    with pytest.raises(SecretInWriteError) as raised:
        check_write(insert, ("01J0", f"/api/opds/{form}/s"), many=False)
    assert SPECIAL not in str(raised.value) and form not in str(raised.value)
    with pytest.raises(SecretInWriteError):
        check_write(insert, [("01J0", "/fine"), ("01J1", f"/x?k={form}")], many=True)
    check_write("INSERT INTO app_setting (key, value) VALUES (?, ?)", ("a", SPECIAL), many=False)
    check_write(insert, ("01J0", "/fine"), many=False)


_ALPHABET = st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="\x00")


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    secret=st.text(_ALPHABET, min_size=8, max_size=40).filter(
        lambda s: s.strip() == s and s != SECRET_PLACEHOLDER
    ),
    encode=st.sampled_from(sorted(_encodings(KEY))),
    template=st.sampled_from(
        ["/o/{}/s", "/s?k={}", "/s#t={}", "https://h.example/{}/f", "{}", "/a/{}/b/{}"]
    ),
    noise=st.text(st.sampled_from("abcdef/-_.?&=#%"), max_size=12),
)
def test_property_no_write_carries_a_registered_secret(
    secret: str, encode: str, template: str, noise: str
) -> None:
    """For any credential, any encoding and any place in the reference, the guard says no, and the
    host-side rewrite leaves nothing the guard would still refuse."""
    forget_secret_values()
    register_secret_value(secret)
    form = _encodings(secret)[encode]
    ref = noise + template.replace("{}", form)
    with pytest.raises(SecretInWriteError):
        check_write("UPDATE unit_variant SET remote_unit_id = ?", (ref,), many=False)
    for ascii_only in (True, False):  # a JSON column arrives serialised and escaped
        document = json.dumps({"url": ref, "list": [ref]}, ensure_ascii=ascii_only)
        with pytest.raises(SecretInWriteError):
            check_write(
                "INSERT INTO event (type, payload) VALUES (?, ?)", ("t", document), many=False
            )
    try:
        rewritten = rewrite_ref(ref, (secret,))
    except SecretInRef:
        return  # refused is acceptable; written with the secret is not
    check_write("UPDATE unit_variant SET remote_unit_id = ?", (rewritten,), many=False)
    assert not _holds(rewritten, secret)


@settings(max_examples=100, deadline=None)
@given(
    ref=st.text(
        st.sampled_from(string.ascii_letters + string.digits + "/-_.:?&=#%+~@ "), max_size=60
    )
)
def test_property_a_clean_reference_is_never_refused_by_the_guard(ref: str) -> None:
    forget_secret_values()
    register_secret_value(KEY)
    check_write("INSERT INTO source_item (id, remote_id) VALUES (?, ?)", ("01J0", ref), many=False)


def test_a_dict_of_parameters_is_scanned_too() -> None:
    register_secret_value(KEY)
    with pytest.raises(SecretInWriteError):
        check_write(
            "INSERT INTO event (id, payload) VALUES (:a, :b)",
            {"a": "1", "b": {"u": KEY}},
            many=False,
        )


def test_a_secret_with_json_special_characters_is_found_in_a_serialised_column() -> None:
    secret = 'q"u\\o\u00e9te\n' + synthetic_text("json")
    register_secret_value(secret)
    for document in (json.dumps({"a": secret}), json.dumps([{"b": [secret]}], ensure_ascii=False)):
        assert secret not in document  # the serialised text hides the raw value
        with pytest.raises(SecretInWriteError):
            check_write("UPDATE upstream_snapshot SET payload = ?", (document,), many=False)


def test_a_write_hidden_in_a_with_statement_is_guarded() -> None:
    register_secret_value(KEY)
    with pytest.raises(SecretInWriteError):
        check_write(
            "WITH x AS (SELECT ? AS v) "
            "INSERT INTO source_item (id, remote_id) SELECT 'a', v FROM x",
            (f"/o/{KEY}",),
            many=False,
        )
    with pytest.raises(SecretInWriteError):  # the SET of an upsert is not a table
        check_write(
            "INSERT INTO source_item (id, remote_id) VALUES (?, ?) "
            "ON CONFLICT (id) DO UPDATE SET remote_id = ?",
            ("a", "/ok", f"/o/{KEY}"),
            many=False,
        )
