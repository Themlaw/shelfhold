"""M2 L04, review of the PR: what the pattern check lets through, and what it must not refuse.

Tokens are built at run time (`synthetic_text`); none is a literal.
"""

import base64
import json
import random
from urllib.parse import quote, quote_plus

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shelfhold.infrastructure.fetcher.refs import SecretInRef, check_ref, has_userinfo
from shelfhold.infrastructure.secret_text import credentialish, sensitive_name, tokenlike
from tests.redaction_support import forget_secret_values, synthetic_text

pytestmark = pytest.mark.req("EF-SRC-035")

TOK = synthetic_text("review-a")[:16]  # mixed case, digits
B36 = "".join(c for c in synthetic_text("review-b").lower() if c.isalnum())[:24]


@pytest.fixture(autouse=True)
def _clean() -> None:
    forget_secret_values()


def test_the_synthetic_tokens_have_the_shape_the_cases_assume() -> None:
    assert tokenlike(TOK) and tokenlike(B36) and len(B36) >= 20


MISSED = [
    f"/series/1?key={TOK}",
    f"/series/1?t={TOK}",
    f"/series/1?accessToken={TOK}",
    f"/series/1?apiToken={TOK}",
    f"/series/1?authToken={TOK}",
    f"/series/1?sessionId={TOK}",
    f"/series/1?token[]={TOK}",
    f"/series/1?hmac={TOK}",
    f"/series/1?code={TOK}",
    f"/series/1?X-Amz-Security-Token={TOK}",
    f"/series/1?Policy={TOK}&Key-Pair-Id=APKA1",
    f"/series/1?SIGNATURE={TOK}",
    f"/api/{TOK}/feed",
    f"/s/{TOK}/ch-1",
    f"/s/{B36}/ch-1",
    f"/series/1#{TOK}",
    f"/series/1#{B36}",
    f"/series/1;jsessionid={TOK}",
    f"https://user:pa ss{TOK}@host.example/x",
    f"https:\\\\user:{TOK}@host.example/x",
    f"https:/user:{TOK}@host.example/x",
    f"https://host.example/a?q=1&token%5B%5D={TOK}",
]


@pytest.mark.parametrize("ref", MISSED)
def test_an_unregistered_credential_is_refused_by_its_shape_and_place(ref: str) -> None:
    with pytest.raises(SecretInRef):
        check_ref(ref, ())


LEGIT = [
    "/manga/secret/chapter-1",
    "/manga/key/chapter-1",
    "/read.php?sid=42&ch=3",
    "/title/12?session=2024",
    "/search?author=Hugo&q=Miserables",
    "/series/one-piece-chapter-1000-volume-100-special",
    "/series/1?code=fr&page=2",
    "/series/1?key=7",
    "/s/ch-001",
    "https://host.example/dl/2024/book.epub",
    "suwayomi:7001:/series-alpha/ch-001",
    "ch-001@x",
]


@pytest.mark.parametrize("ref", LEGIT)
def test_a_legitimate_reference_is_not_taken_for_a_secret(ref: str) -> None:
    assert check_ref(ref, ()) == ref


def test_names_are_read_word_by_word() -> None:
    for name in ("key", "t", "apiToken", "api_key", "token[]", "X-Auth-Token", "Policy", "hmac"):
        assert sensitive_name(name), name
    for name in ("author", "keyword", "page", "turkey", "item", "tag"):
        assert not sensitive_name(name), name


def test_values_need_the_shape_of_a_credential() -> None:
    assert credentialish(TOK) and not credentialish("2024") and not credentialish("fr")
    assert not credentialish("chapters") and has_userinfo("https:/a:b@h")


def test_every_base64_and_encoded_form_of_a_registered_key_is_found() -> None:
    import base64
    import json
    from urllib.parse import quote

    from shelfhold.infrastructure.logging import known_secret_values, register_secret_value
    from shelfhold.infrastructure.secret_text import contains_secret

    key = synthetic_text("review-c")[:16]
    register_secret_value(key)
    known = known_secret_values()
    raw = key.encode()
    blobs = [
        base64.b64encode(raw).decode(),
        base64.b64encode(raw).decode().rstrip("="),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
        quote(base64.b64encode(raw).decode(), safe=""),
        base64.urlsafe_b64encode(json.dumps({"k": key, "n": 1}).encode()).decode(),
        base64.urlsafe_b64encode(base64.b64encode(json.dumps({"k": key}).encode())).decode(),
    ]
    for blob in blobs:
        assert contains_secret(f"/dl/{blob}/ch-1", known), blob
    assert not contains_secret("/dl/" + base64.b64encode(b"nothing here at all").decode(), known)


def test_a_ref_with_a_secret_is_a_content_error_and_a_published_event() -> None:
    import asyncio

    from shelfhold.acquisition.application.failures import from_service
    from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
    from shelfhold.events.operating import OPERATING_EVENTS
    from shelfhold.plugin_host.errors import RefHasSecret

    async def refuse() -> None:
        raise RefHasSecret("memo: the reference holds a secret (query)")

    with pytest.raises(RefHasSecret) as caught:
        asyncio.run(from_service(refuse()))
    assert caught.value.code == "source.ref_has_secret" in OPERATING_EVENTS
    assert disposition_of(classify(caught.value)) is Disposition.COUNT


@pytest.mark.parametrize(
    "statement",
    [
        "/* note */ INSERT INTO unit_variant (id, remote_unit_id) VALUES (?, ?)",
        "-- note\nINSERT INTO unit_variant (id, remote_unit_id) VALUES (?, ?)",
        "/* a */ /* b */ UPDATE unit_variant SET remote_unit_id = ? WHERE id = ?",
        "INSERT INTO main.unit_variant (id, remote_unit_id) VALUES (?, ?)",
        'INSERT INTO "main"."unit_variant" (id, remote_unit_id) VALUES (?, ?)',
        "WITH x AS (SELECT 1) /* c */ INSERT INTO unit_variant (id, remote_unit_id) VALUES (?, ?)",
    ],
)
def test_the_write_guard_reads_through_comments_and_schema_prefixes(statement: str) -> None:
    from shelfhold.infrastructure.logging import register_secret_value
    from shelfhold.infrastructure.write_guard import SecretInWriteError, check_write

    key = synthetic_text("review-d")[:16]
    register_secret_value(key)
    with pytest.raises(SecretInWriteError):
        check_write(statement, ("a", f"/x/{key}"), many=False)


def test_the_write_guard_sees_a_value_written_into_the_sql_text() -> None:
    from shelfhold.infrastructure.logging import register_secret_value
    from shelfhold.infrastructure.write_guard import SecretInWriteError, check_write

    key = synthetic_text("review-e")[:16]
    register_secret_value(key)
    with pytest.raises(SecretInWriteError):
        check_write(
            "INSERT INTO unit_variant (id, remote_unit_id) VALUES ('a', '/x/" + key + "')",  # noqa: S608
            (),
            many=False,
        )
    check_write("INSERT INTO unit_variant (id) VALUES ('a') -- /* ok */", (), many=False)


def test_redact_url_masks_short_names_and_bare_tokens_but_keeps_an_author() -> None:
    from shelfhold.infrastructure.logging import redact_url, route_of

    tok = synthetic_text("review-f")[:16]
    b36 = "".join(c for c in synthetic_text("review-g").lower() if c.isalnum())[:24]
    for url in (
        f"https://h.example/x?t={tok}",
        f"https://h.example/x?Policy={tok}&k=1",
        f"https://h.example/x?accessToken={tok}",
        f"https://h.example/api/{b36}/feed",
        f"https://h.example/s/{tok}/ch-1",
        f"https://h.example/x#{tok}",
        f"https://h.example/x?token[]={tok}",
    ):
        out = redact_url(url)
        assert tok not in out and b36 not in out, (url, out)
    assert route_of(f"https://h.example/s/{b36}/ch-1") == "h.example/s/***/ch-1"
    kept = "https://h.example/search?author=Hugo&q=Miserables&keyword=a#top"
    assert redact_url(kept) == kept


# -- property tests with adversarial inputs (review) ------------------------------------------


_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_NAMES = [
    "key", "t", "token", "accessToken", "apiToken", "authToken", "sessionId", "hmac", "Policy",
    "Signature", "code", "sig", "secret", "password", "X-Amz-Security-Token",
]  # fmt: skip


def _mixed_case(name: str, seed: int) -> str:
    rng = random.Random(seed)  # noqa: S311 - a test shuffle
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in name)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    name=st.sampled_from(_NAMES),
    seed=st.integers(0, 10_000),
    value=st.text(_ALNUM, min_size=16, max_size=40).filter(
        lambda v: any(c.isdigit() for c in v) and any(c.isalpha() for c in v)
    ),
    shape=st.sampled_from(["?{n}={v}", "?a=1&{n}={v}", "?{n}[]={v}", "#{n}={v}", ";{n}={v}"]),
)
def test_property_a_never_registered_credential_is_refused_whatever_the_case_of_its_name(
    name: str, seed: int, value: str, shape: str
) -> None:
    forget_secret_values()
    ref = "/series/1" + shape.format(n=_mixed_case(name, seed), v=value)
    with pytest.raises(SecretInRef):
        check_ref(ref, ())


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    seed=st.text(_ALNUM, min_size=1, max_size=12),
    encoding=st.sampled_from(
        ["raw", "percent", "plus", "double", "b64", "b64_unpadded", "b64url", "b64_json", "b64_pct"]
    ),
    where=st.sampled_from(["/dl/{}/ch-1", "/ch-1?x={}", "/ch-1#{}", "/a/{}"]),
)
def test_property_a_registered_key_is_refused_in_every_encoding(
    seed: str, encoding: str, where: str
) -> None:
    from shelfhold.infrastructure.logging import known_secret_values, register_secret_value
    from shelfhold.infrastructure.secret_text import contains_secret

    forget_secret_values()
    key = synthetic_text("prop-" + seed, specials=True)
    register_secret_value(key)
    raw = key.encode()
    forms = {
        "raw": key,
        "percent": quote(key, safe=""),
        "plus": quote_plus(key),
        "double": quote(quote(key, safe=""), safe=""),
        "b64": base64.b64encode(raw).decode(),
        "b64_unpadded": base64.b64encode(raw).decode().rstrip("="),
        "b64url": base64.urlsafe_b64encode(raw).decode(),
        "b64_json": base64.urlsafe_b64encode(json.dumps({"k": key}).encode()).decode(),
        "b64_pct": quote(base64.b64encode(raw).decode(), safe=""),
    }
    text = where.format(forms[encoding])
    assert contains_secret(text, known_secret_values()), (encoding, text)


@settings(max_examples=200, deadline=None)
@given(
    segments=st.lists(st.text(_ALNUM, min_size=1, max_size=9), min_size=1, max_size=5),
    names=st.lists(st.sampled_from(["page", "ch", "lang", "order", "q", "n"]), max_size=3),
    numbers=st.lists(st.integers(0, 99_999), max_size=3),
)
def test_property_a_clean_reference_over_the_full_alphabet_is_never_refused(
    segments: list[str], names: list[str], numbers: list[int]
) -> None:
    forget_secret_values()
    ref = "/" + "/".join(segments)
    if names:
        ref += "?" + "&".join(f"{n}={v}" for n, v in zip(names, numbers, strict=False))
    assert check_ref(ref, ()) == ref


def test_shelfhold_own_identifiers_are_not_taken_for_credentials() -> None:
    """ULIDs, UUIDs and ids built from them (`ch-<ulid>`) are shown to the administrator and typed
    back: no shape check may mask or refuse them. A registered secret still is, whatever it looks
    like."""
    from shelfhold.infrastructure.ids import new_ulid
    from shelfhold.infrastructure.logging import (
        redact_url,
        redact_value,
        register_secret_value,
        route_of,
    )

    ulid, uid = new_ulid(), "0b9f6c1e-3a52-4c7d-9e0a-5d2f8b7a1c34"
    for ident in (ulid, ulid.lower(), uid, f"ch-{ulid}", f"series-{ulid}", f"suwayomi:{ulid}"):
        assert not tokenlike(ident), ident
        assert check_ref(f"/s/{ident}/x?id={ident}", ()) == f"/s/{ident}/x?id={ident}"
        assert redact_url(f"https://h.example/s/{ident}?id={ident}#{ident}") == (
            f"https://h.example/s/{ident}?id={ident}#{ident}"
        )
        assert ident in route_of(f"https://h.example/s/{ident}/x")
        assert redact_value(f"id {ident}") == f"id {ident}"
    assert check_ref("suwayomi:1", ()) == "suwayomi:1"
    forget_secret_values()
    register_secret_value(ulid)  # registered: masked even though it looks like an ULID
    assert ulid not in redact_url(f"https://h.example/s/{ulid}")
    with pytest.raises(SecretInRef):
        check_ref(f"/s/{ulid}", {ulid})
