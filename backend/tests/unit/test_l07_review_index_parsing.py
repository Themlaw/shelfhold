"""Relecture adverse de M6 L07: the verifier and the v1 parser refuse, they never crash or guess.

- a global signature that is not 64 bytes makes `nacl` raise its own `ValueError`, which no
  caller catches (`PluginIndexError` only): a 500 on the API, a traceback in the CLI, and in the
  rotation path the typed `rotation_refused` is skipped;
- the parser says "anything unexpected is a refusal" but takes unknown fields, duplicate JSON
  keys, a non-boolean `deprecated` and two entries for one plugin key (which wheel is vouched?).
"""

from __future__ import annotations

import base64
import json

import pytest

from shelfhold.plugin_host.index import PluginIndexError, parse_document, parse_public_key
from shelfhold.plugin_host.index.minisign import verify_detached
from tests.plugin_index_env import index_document, make_key, plugin_entry, sign_index

pytestmark = pytest.mark.req("EF-SRC-017")

EXPIRES = "2027-03-01T00:00:00Z"
COMMENT = f"shelfhold-index v1 version=1 expires={EXPIRES}"


@pytest.mark.parametrize("length", [0, 63, 65])
def test_global_signature_of_wrong_length_is_a_typed_refusal(length: int) -> None:
    key = make_key()
    document = index_document(version=1, expires=EXPIRES)
    lines = sign_index(document, key, comment=COMMENT).decode("ascii").splitlines()
    lines[3] = base64.b64encode(b"\x01" * length).decode("ascii")
    signature = ("\n".join(lines) + "\n").encode("ascii")
    with pytest.raises(PluginIndexError):
        verify_detached(document, signature, parse_public_key(key.public_b64))


def _base() -> dict[str, object]:
    return json.loads(index_document(version=1, expires=EXPIRES, plugins=[plugin_entry()]))


def _unknown_field() -> bytes:
    raw = _base()
    raw["mirror"] = "https://elsewhere.example.org/"
    return json.dumps(raw).encode()


def _duplicate_json_key() -> bytes:
    text = json.dumps(_base())
    return (text[:-1] + ', "revoked": {"plugin_keys": [], "wheel_sha256": []}}').encode()


def _deprecated_not_bool() -> bytes:
    raw = _base()
    raw["plugins"] = [plugin_entry(deprecated="yes")]
    return json.dumps(raw).encode()


def _two_entries_one_key() -> bytes:
    raw = _base()
    raw["plugins"] = [
        plugin_entry(sha256="sha256:" + "a" * 64),
        plugin_entry(sha256="sha256:" + "b" * 64),
    ]
    return json.dumps(raw).encode()


@pytest.mark.parametrize(
    "build",
    [_unknown_field, _duplicate_json_key, _deprecated_not_bool, _two_entries_one_key],
    ids=["unknown-field", "duplicate-json-key", "deprecated-not-bool", "two-entries-one-key"],
)
def test_parser_is_strict(build: object) -> None:
    with pytest.raises(PluginIndexError) as caught:
        parse_document(build())  # type: ignore[operator]
    assert caught.value.code == "document_invalid"
