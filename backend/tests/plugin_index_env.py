"""Helpers for the signed plugin index tests: keys made in the test, a spying fetcher.

No secret key ever lives in this file or in a fixture (D-P3): the test that needs a signer
generates it here with `pynacl`, and only public vectors (a document, a public key, a signature
built in the test) exist on disk. The signature writer speaks the minisign prehashed format
(`ED`: Ed25519 over the BLAKE2b-512 of the file) that `plugin_host.index.minisign` verifies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from nacl.signing import SigningKey
from shelfhold_sdk.fetcher import Response

from shelfhold.infrastructure.fetcher.client import Download
from shelfhold.infrastructure.fetcher.errors import SsrfBlocked

WHEEL_URL = "https://index.example.org/wheels/acme-1.0.0-py3-none-any.whl"


@dataclass(frozen=True)
class IndexKey:
    """A signer of the index: the secret stays in the test, the public key is the pin."""

    signing: SigningKey
    public_b64: str
    key_id: str  # 16 uppercase hex characters, the minisign key id

    @property
    def raw_id(self) -> bytes:
        return bytes.fromhex(self.key_id)


def make_key() -> IndexKey:
    signing = SigningKey.generate()
    raw_id = secrets.token_bytes(8)
    blob = b"Ed" + raw_id + bytes(signing.verify_key)
    return IndexKey(signing, base64.b64encode(blob).decode("ascii"), raw_id.hex().upper())


def trusted_comment(version: int, expires: str) -> str:
    """The one trusted comment an index of this host carries (D14)."""
    return f"shelfhold-index v1 version={version} expires={expires}"


def sign_index(
    document: bytes,
    key: IndexKey,
    *,
    comment: str,
    prehashed: bool = True,
    key_id: bytes | None = None,
    global_signature: bool = True,
) -> bytes:
    """A detached minisign signature, as `index.json.minisig` carries it.

    `prehashed=False` writes the old `Ed` format (a signature of the file itself), which the
    verifier refuses; `key_id` lets a test sign under another key's id; `global_signature=False`
    breaks the signature of the trusted comment.
    """
    message = hashlib.blake2b(document, digest_size=64).digest() if prehashed else document
    algo = b"ED" if prehashed else b"Ed"
    signature = bytes(key.signing.sign(message).signature)
    blob = algo + (key.raw_id if key_id is None else key_id) + signature
    if global_signature:
        global_sig = bytes(key.signing.sign(comment.encode("utf-8") + signature).signature)
    else:
        global_sig = b"\x00" * 64
    return (
        f"untrusted comment: signature by {key.key_id}\n"
        f"{base64.b64encode(blob).decode('ascii')}\n"
        f"trusted comment: {comment}\n"
        f"{base64.b64encode(global_sig).decode('ascii')}\n"
    ).encode("ascii")


def plugin_entry(
    key: str = "acme",
    version: str = "1.0.0",
    sha256: str | None = None,
    wheel_url: str = WHEEL_URL,
    **over: Any,
) -> dict[str, Any]:
    return {
        "key": key,
        "version": version,
        "api": 2,
        "rpc_protocol": 1,
        "wheel_url": wheel_url,
        "sha256": sha256 or "sha256:" + "a" * 64,
        "min_host": "1.0.0",  # below the host's own version: an entry the host can run
        "deprecated": False,
        "replaced_by": None,
        **over,
    }


def index_document(
    *,
    version: int,
    expires: str,
    plugins: list[dict[str, Any]] | None = None,
    plugin_keys: list[str] | None = None,
    wheel_sha256: list[str] | None = None,
    next_pubkey: str | None = None,
) -> bytes:
    document: dict[str, Any] = {
        "format": 1,
        "version": version,
        "expires": expires,
        "plugins": [plugin_entry() for _ in range(0)] if plugins is None else plugins,
        "revoked": {"plugin_keys": plugin_keys or [], "wheel_sha256": wheel_sha256 or []},
    }
    if next_pubkey is not None:
        document["next_pubkey"] = next_pubkey
    return json.dumps(document).encode("utf-8")


@dataclass
class FakeFetcher:
    """The only network of the index tests: it records every URL it is asked for."""

    documents: dict[str, bytes] = field(default_factory=dict)
    wheels: dict[str, bytes] = field(default_factory=dict)
    blocked: frozenset[str] = frozenset()
    calls: list[str] = field(default_factory=list)

    def serving(self, url: str, document: bytes, signature: bytes) -> FakeFetcher:
        self.documents[url] = document
        self.documents[url + ".minisig"] = signature
        return self

    async def get(
        self,
        url: str,
        *,
        headers: Any = None,
        params: Any = None,
        referer: str | None = None,
        max_bytes: int | None = None,
    ) -> Response:
        self.calls.append(url)
        if url in self.blocked:
            raise SsrfBlocked(f"{urlsplit(url).hostname} is not an address the Fetcher reaches")
        body = self.documents.get(url)
        return Response(status=200 if body is not None else 404, body=body or b"")

    async def download(
        self,
        url: str,
        sink: Any,
        *,
        max_bytes: int,
        headers: Any = None,
        referer: str | None = None,
        total_timeout: float | None = None,
    ) -> Download:
        self.calls.append(url)
        if url in self.blocked:
            raise SsrfBlocked(f"{urlsplit(url).hostname} is not an address the Fetcher reaches")
        data = self.wheels.get(url)
        if data is None:
            return Download(status=404, headers={}, size=0, sha256="sha256:" + "0" * 64)
        sink.write(data)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        return Download(status=200, headers={}, size=len(data), sha256=digest)

    async def aclose(self) -> None:
        return None
