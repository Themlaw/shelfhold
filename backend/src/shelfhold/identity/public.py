"""What the interfaces and the composition root may use from the identity context."""

from shelfhold.identity.adapters.factory import local_auth
from shelfhold.identity.application.ports import ApiKeyRecord, ReauthMethod, UserRecord
from shelfhold.identity.application.service import (
    AdminExistsError,
    AuthError,
    CreatedApiKey,
    InvalidCredentialsError,
    IssuedSession,
    LocalAuth,
    SessionAuth,
    ThrottledError,
    UnknownReauthMethodError,
    UnknownUserError,
)
from shelfhold.identity.domain.credentials import (
    MAX_PASSWORD_LENGTH,
    Credential,
    Principal,
    SessionPolicy,
    session_policy,
)
from shelfhold.identity.domain.reserved import (
    REAUTH_EFFECTS,
    SYSTEM_EMITTERS,
    Allowed,
    CredentialNotAllowed,
    Effect,
    ReauthRequired,
    ReservedAction,
    Verdict,
    authorize_reserved,
)
from shelfhold.identity.domain.throttle import LoginThrottle
from shelfhold.identity.domain.tokens import same_secret

__all__ = [
    "MAX_PASSWORD_LENGTH",
    "REAUTH_EFFECTS",
    "SYSTEM_EMITTERS",
    "AdminExistsError",
    "Allowed",
    "ApiKeyRecord",
    "AuthError",
    "CreatedApiKey",
    "Credential",
    "CredentialNotAllowed",
    "Effect",
    "InvalidCredentialsError",
    "IssuedSession",
    "LocalAuth",
    "LoginThrottle",
    "Principal",
    "ReauthMethod",
    "ReauthRequired",
    "ReservedAction",
    "SessionAuth",
    "SessionPolicy",
    "ThrottledError",
    "UnknownReauthMethodError",
    "UnknownUserError",
    "UserRecord",
    "Verdict",
    "authorize_reserved",
    "local_auth",
    "same_secret",
    "session_policy",
]
