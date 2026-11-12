"""M3 L07 (EF-USR-022, EF-USR-023, EF-USR-011): the device-token hook, the device label of a
session and the recovery kit, as pure rules."""

import base64
import dataclasses

import pytest

from shelfhold.identity.domain.credentials import Credential
from shelfhold.identity.public import (
    KEYRING_EXPORT_NAME,
    device_label,
    device_token,
    render_recovery_kit,
    scope_of,
    token_principal,
)

ADMIN_ID = "01HZZZZZZZZZZZZZZZZZZZZZZA"
CHILD_ID = "01HZZZZZZZZZZZZZZZZZZZZZZC"


@pytest.mark.req("EF-USR-022")
def test_device_token_filters_apply_to_subject_not_issuer() -> None:
    """The reader of a token is the profile it was issued FOR: its ceiling and its role are the
    child's, whoever the admin that paired the e-reader was."""
    token = device_token(subject_user_id=CHILD_ID, issued_by=ADMIN_ID)
    assert token.subject_user_id == CHILD_ID and token.issued_by == ADMIN_ID
    principal = token_principal(token, username="kid", role="child", can_rate=False)
    assert principal.credential is Credential.DEVICE_TOKEN
    assert principal.user_id == CHILD_ID  # what every filter reads
    assert principal.role == "child" and principal.can_rate is False
    assert ADMIN_ID not in {principal.user_id, principal.username, principal.actor_id}
    assert principal.key_id == token.token_id
    assert principal.actor_id == f"device_token:{token.token_id}"
    assert scope_of("child", 10) != scope_of("admin", None)
    assert not scope_of("child", 10).unrestricted


@pytest.mark.req("EF-USR-022")
def test_device_token_needs_a_subject_and_never_defaults_to_the_issuer() -> None:
    for bad in ("", " ", None, 7):
        with pytest.raises((ValueError, TypeError)):
            device_token(subject_user_id=bad, issued_by=ADMIN_ID)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        device_token(issued_by=ADMIN_ID)  # type: ignore[call-arg]  # no default subject
    with pytest.raises((ValueError, TypeError)):
        device_token(subject_user_id=CHILD_ID, issued_by="")


@pytest.mark.req("EF-USR-022")
def test_device_token_is_immutable_and_its_principal_cannot_become_the_issuer() -> None:
    token = device_token(subject_user_id=CHILD_ID, issued_by=ADMIN_ID)
    with pytest.raises(dataclasses.FrozenInstanceError):
        token.subject_user_id = ADMIN_ID  # type: ignore[misc]
    principal = token_principal(token, username="kid", role="admin-ish", can_rate=True)
    assert principal.user_id == CHILD_ID
    # a role the rules do not know sees nothing, whoever issued the token
    assert scope_of("admin-ish", None) == scope_of(None, None)


@pytest.mark.req("EF-USR-011")
def test_device_label_is_a_coarse_label_never_the_raw_header() -> None:
    firefox = device_label("Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0")
    assert firefox == "Firefox · Linux"
    phone = device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) Safari/605.1.15")
    assert phone is not None and "iPhone" in phone and "17_5" not in phone
    assert device_label(None) is None and device_label("") is None
    hostile = device_label("<script>alert(1)</script>" + "A" * 5000)
    assert hostile is None or (len(hostile) <= 80 and "<" not in hostile and ">" not in hostile)
    assert device_label("curl/8.5.0") == "curl"


@pytest.mark.req("EF-USR-023")
def test_recovery_kit_is_one_self_contained_printable_page() -> None:
    page = render_recovery_kit()
    lowered = page.lower()
    assert lowered.startswith("<!doctype html>")
    assert "@media print" in lowered
    assert "<script" not in lowered and "<img" not in lowered and "<link" not in lowered
    assert "http://" not in lowered and "src=" not in lowered
    assert "shelfhold admin reset-password" in page
    assert "shelfhold secrets export" in page
    assert KEYRING_EXPORT_NAME in page
    assert "shelfhold admin sessions revoke" in page
    assert "Kavita" in page


@pytest.mark.req("EF-USR-023")
def test_recovery_kit_takes_no_value_that_could_be_a_secret() -> None:
    """The renderer has no parameter a secret could go in; a caller cannot slip one into the
    page. The name of the export file is the only free text and it is escaped and bounded."""
    import inspect

    from shelfhold.identity.application import recovery_kit

    parameters = set(inspect.signature(recovery_kit.render_recovery_kit).parameters)
    assert parameters <= {"keyring_export_name", "public_url"}
    page = render_recovery_kit(keyring_export_name='<b onload="x">' + "k" * 500)
    assert "<b onload" not in page and "k" * 200 not in page
    assert base64.b64encode(b"x" * 32).decode() not in page
