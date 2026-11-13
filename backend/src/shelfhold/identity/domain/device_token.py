"""The device-token hook (EF-USR-022 (4), K5): the filters read the SUBJECT, never the issuer.

A device token belongs to a non-interactive client (an e-reader, OPDS, kosync) and is issued by an
admin (`issued_by`) FOR a reader profile (`subject_user_id`). Every right and every filter (the
ceiling of EF-USR-006, the table of truth of ENF-SEC-010) is that of the subject: an admin who
pairs the e-reader of a child gives it the child's view, not their own. The subject has no default
(the pairing asks for the profile), and the issuer is kept for the audit and nothing else.

A reset of the subject's password (`admin reset-password`) must close the tokens issued for that
profile, as it closes its sessions, passkeys and API keys: the M5 store that holds them joins
`reset_credentials` (EF-USR-011), or a reader's e-reader would outlive a stolen-device reset.

M3 lands the hook and its rule; the token itself (storage, pairing, revocation) is M5 (EF-USR-008).
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
