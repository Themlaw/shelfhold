"""What the interfaces and the composition root may use from the identity context."""

from shelfhold.identity.adapters.factory import local_auth
from shelfhold.identity.application.ports import ApiKeyRecord, ReauthMethod, UserRecord
from shelfhold.identity.application.service import (
    IDENTITY_HEADER_AUDIT,
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
from shelfhold.identity.domain.catalogue import (
    EXIT_ROUTES,
    EXIT_STATES_LATER,
    RESERVED_ACTIONS,
    ExitRoute,
)
from shelfhold.identity.domain.credentials import (
    CREDENTIAL_AXIS,
    MAX_PASSWORD_LENGTH,
    Credential,
    Principal,
    SessionPolicy,
    credential_axis,
    session_policy,
)
from shelfhold.identity.domain.emitters import DELETION_SITES, OWN_SCRATCH, PRIMITIVE, REPLAY
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
    "CREDENTIAL_AXIS",
    "DELETION_SITES",
    "EXIT_ROUTES",
    "EXIT_STATES_LATER",
    "IDENTITY_HEADER_AUDIT",
    "MAX_PASSWORD_LENGTH",
    "OWN_SCRATCH",
    "PRIMITIVE",
    "REAUTH_EFFECTS",
    "REPLAY",
    "RESERVED_ACTIONS",
    "SYSTEM_EMITTERS",
    "AdminExistsError",
    "Allowed",
    "ApiKeyRecord",
    "AuthError",
    "CreatedApiKey",
    "Credential",
    "CredentialNotAllowed",
    "Effect",
    "ExitRoute",
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
    "credential_axis",
    "local_auth",
    "same_secret",
    "session_policy",
]
