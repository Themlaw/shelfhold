"""The py_webauthn binding: the only import site of the library (contract `webauthn-confined`).

It turns the library's structures into plain dicts and bytes for the application layer, and
adds the checks the library leaves to the relying party: user verification on every message, no
cross-origin ceremony, and a signature counter whose policy is ours (ADR-0050).
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

import webauthn as webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from shelfhold.identity.domain.passkeys import (
    KNOWN_TRANSPORTS,
    MAX_CREDENTIAL_BYTES,
    RelyingParty,
    VerifiedCredential,
    WebAuthnRefusedError,
)

TIMEOUT_MS = 60_000


def _json(options: object) -> dict[str, Any]:
    loaded = json.loads(webauthn.options_to_json(options))  # type: ignore[arg-type]
    return dict(loaded)


def _descriptors(ids: Sequence[bytes]) -> list[PublicKeyCredentialDescriptor]:
    return [PublicKeyCredentialDescriptor(id=raw) for raw in ids]


def _checked(credential: Mapping[str, object]) -> Mapping[str, object]:
    """Shape and size, and no ceremony run from another browsing context."""
    try:
        if len(json.dumps(credential)) > MAX_CREDENTIAL_BYTES:
            raise WebAuthnRefusedError("too_large")
        response = credential["response"]
        if not isinstance(response, Mapping):
            raise WebAuthnRefusedError("malformed")
        client = json.loads(base64url_to_bytes(str(response["clientDataJSON"])))
    except WebAuthnRefusedError:
        raise
    except Exception as error:
        raise WebAuthnRefusedError("malformed") from error
    if not isinstance(client, dict) or client.get("crossOrigin") or "topOrigin" in client:
        raise WebAuthnRefusedError("cross_origin")
    return credential


class WebAuthnLibrary:
    def registration_options(
        self,
        *,
        rp: RelyingParty,
        user_handle: bytes,
        username: str,
        challenge: bytes,
        exclude: Sequence[bytes],
    ) -> dict[str, Any]:
        return _json(
            webauthn.generate_registration_options(
                rp_id=rp.id,
                rp_name=rp.id,
                user_id=user_handle,
                user_name=username,
                challenge=challenge,
                timeout=TIMEOUT_MS,
                attestation=AttestationConveyancePreference.NONE,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    require_resident_key=True,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ),
                exclude_credentials=_descriptors(exclude),
            )
        )

    def authentication_options(
        self, *, rp: RelyingParty, challenge: bytes, allow: Sequence[bytes]
    ) -> dict[str, Any]:
        return _json(
            webauthn.generate_authentication_options(
                rp_id=rp.id,
                challenge=challenge,
                timeout=TIMEOUT_MS,
                allow_credentials=_descriptors(allow) or None,
                user_verification=UserVerificationRequirement.REQUIRED,
            )
        )

    def verify_registration(
        self, *, rp: RelyingParty, credential: Mapping[str, object], challenge: bytes
    ) -> VerifiedCredential:
        try:
            verified = webauthn.verify_registration_response(
                credential=dict(_checked(credential)),
                expected_challenge=challenge,
                expected_rp_id=rp.id,
                expected_origin=rp.origin,
                require_user_verification=True,
            )
            response = credential["response"]
            declared = response.get("transports") if isinstance(response, Mapping) else None
        except WebAuthnRefusedError:
            raise
        except Exception as error:  # the library raises its own and low-level parsing errors
            raise WebAuthnRefusedError("verification") from error
        transports = tuple(
            sorted({t for t in declared if t in KNOWN_TRANSPORTS})
            if isinstance(declared, list)
            else ()
        )
        return VerifiedCredential(
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            aaguid=verified.aaguid or None,
            transports=transports,
        )

    def credential_id(self, credential: Mapping[str, object]) -> bytes:
        """The raw identifier; `id` and `rawId` must agree."""
        try:
            raw = base64url_to_bytes(str(credential["rawId"]))
            if bytes_to_base64url(raw) != credential["id"]:
                raise WebAuthnRefusedError("id_mismatch")
        except WebAuthnRefusedError:
            raise
        except Exception as error:
            raise WebAuthnRefusedError("malformed") from error
        return raw

    def user_handle(self, credential: Mapping[str, object]) -> bytes | None:
        try:
            response = credential["response"]
            handle = response.get("userHandle") if isinstance(response, Mapping) else None
            return base64url_to_bytes(str(handle)) if handle else None
        except Exception as error:
            raise WebAuthnRefusedError("malformed") from error

    def verify_assertion(
        self,
        *,
        rp: RelyingParty,
        credential: Mapping[str, object],
        challenge: bytes,
        public_key: bytes,
    ) -> int:
        """The counter the authenticator presented, after signature, challenge, origin, RP ID
        and user verification passed. The counter policy is the caller's: the library is told
        the stored value is 0 so that it never decides it."""
        try:
            verified = webauthn.verify_authentication_response(
                credential=dict(_checked(credential)),
                expected_challenge=challenge,
                expected_rp_id=rp.id,
                expected_origin=rp.origin,
                credential_public_key=public_key,
                credential_current_sign_count=0,
                require_user_verification=True,
            )
        except WebAuthnRefusedError:
            raise
        except Exception as error:
            raise WebAuthnRefusedError("verification") from error
        return int(verified.new_sign_count)
