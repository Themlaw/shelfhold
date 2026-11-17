"""The device-token hook (EF-USR-022 (4), K5): the filters read the SUBJECT, never the issuer.

A device token belongs to a non-interactive client (an e-reader, OPDS, kosync) and is issued by an
admin (`issued_by`) FOR a reader profile (`subject_user_id`). Every right and every filter (the
ceiling of EF-USR-006, the table of truth of ENF-SEC-010) is that of the subject: an admin who
pairs the e-reader of a child gives it the child's view, not their own. The subject has no default
(the pairing asks for the profile), and the issuer is kept for the audit and nothing else.

A reset of the subject's password (`admin reset-password`), a change of the subject's role and a
restore close the tokens held for that profile, as they close its sessions, passkeys and API keys
(EF-USR-011, M4 L46): a reader's e-reader does not outlive a stolen-device reset.

M3 landed the hook and its rule; M4 (L46, ADR-0053 §4) stores the token (one SHA-256 of the md5 of
a generated secret, never the secret), pairs it and revokes it; M5 L02 (EF-USR-008, D19) adds the
**device** a token belongs to (`identity/domain/device.py`: one per pairing, a label, a kind, when
it was seen last) and the **rotation** of its secret, in place and with no grace period: the old
secret is refused from the next request, nothing is paired again.
"""

from dataclasses import dataclass, field
from secrets import token_hex

from shelfhold.identity.domain.credentials import Credential, Principal


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a profile identifier")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{name} must be a profile identifier")
    return cleaned


@dataclass(frozen=True)
class DeviceToken:
    subject_user_id: str
    issued_by: str
    token_id: str = field(default_factory=lambda: token_hex(13))


def device_token(
    *, subject_user_id: str, issued_by: str, token_id: str | None = None
) -> DeviceToken:
    """Both ends are named; neither stands in for the other."""
    subject = _identifier(subject_user_id, "subject_user_id")
    issuer = _identifier(issued_by, "issued_by")
    if token_id is None:
        return DeviceToken(subject, issuer)
    return DeviceToken(subject, issuer, _identifier(token_id, "token_id"))


def token_principal(token: DeviceToken, *, username: str, role: str, can_rate: bool) -> Principal:
    """Who the token acts as: the subject, with the subject's role and right, from the database
    at each request (the caller reads them by `token.subject_user_id`). Never the issuer."""
    return Principal(
        user_id=token.subject_user_id,
        username=username,
        role=role,
        credential=Credential.DEVICE_TOKEN,
        key_id=token.token_id,
        can_rate=can_rate,
    )
