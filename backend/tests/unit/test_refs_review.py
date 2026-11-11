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
