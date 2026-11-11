"""M2 L04, review of the PR: what the pattern check lets through, and what it must not refuse.

Tokens are built at run time (`synthetic_text`); none is a literal.
"""

import pytest

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
