"""A software authenticator for the passkey tests: it signs what a browser would send.

It builds the same bytes as a platform authenticator (attestation `none`, ES256, authenticator
data with the user-presence and user-verification flags), so the server verifies real WebAuthn
messages through `create_app()`, never a mock of the verification.
"""

import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class SoftPasskey:
    """One credential on an authenticator that believes it talks to `origin` / `rp_id`."""

    origin: str = "https://testserver"
    rp_id: str = "testserver"
    uv: bool = True
    counter_step: int = 1  # 0: an authenticator that never counts (synced passkeys)
    credential_id: bytes = field(default_factory=lambda: os.urandom(32))
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    counter: int = 0
    user_handle: bytes | None = None

    def _client_data(self, kind: str, challenge: str, *, cross_origin: bool = False) -> bytes:
        data: dict[str, Any] = {"type": kind, "challenge": challenge, "origin": self.origin}
        if cross_origin:
            data["crossOrigin"] = True
        return json.dumps(data).encode()

    def _flags(self, extra: int = 0) -> int:
        return FLAG_UP | (FLAG_UV if self.uv else 0) | extra

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32), -3: numbers.y.to_bytes(32)}
        )

    def create(self, options: dict[str, Any], *, cross_origin: bool = False) -> dict[str, Any]:
        """The credential a browser returns for `navigator.credentials.create`."""
        self.user_handle = unb64u(options["user"]["id"])
        client_data = self._client_data(
            "webauthn.create", options["challenge"], cross_origin=cross_origin
        )
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        attested = (
            b"\x00" * 16
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_key()
        )
        auth_data = (
            rp_hash + bytes([self._flags(FLAG_AT)]) + struct.pack(">I", self.counter) + attested
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64u(self.credential_id),
            "rawId": b64u(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64u(client_data),
                "attestationObject": b64u(attestation),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
        }

    def get(
        self,
        options: dict[str, Any],
        *,
        counter: int | None = None,
        with_user_handle: bool = True,
        cross_origin: bool = False,
    ) -> dict[str, Any]:
        """The assertion a browser returns for `navigator.credentials.get`."""
        self.counter = counter if counter is not None else self.counter + self.counter_step
        client_data = self._client_data(
            "webauthn.get", options["challenge"], cross_origin=cross_origin
        )
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([self._flags()])
            + struct.pack(">I", self.counter)
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        response: dict[str, Any] = {
            "clientDataJSON": b64u(client_data),
            "authenticatorData": b64u(auth_data),
            "signature": b64u(signature),
        }
        if with_user_handle and self.user_handle is not None:
            response["userHandle"] = b64u(self.user_handle)
        return {
            "id": b64u(self.credential_id),
            "rawId": b64u(self.credential_id),
            "type": "public-key",
            "response": response,
            "clientExtensionResults": {},
        }
