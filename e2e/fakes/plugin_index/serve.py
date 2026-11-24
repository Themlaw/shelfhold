"""The simulated plugin repository and the synthetic catalogue of the example plugin (M6 journeys).

It has no socket of its own: `fakes/m2/serve.py` (the simulated sources, one server answering by
the host name of the request, on the one address that exists on the loopback of the stack's
namespace) hands it the requests of the two names below. What it serves is whatever the test put
under `/tmp/plugin-index/<host>/<path>` of that container: the signed index, its signature, the
wheels and the catalogue. The test makes the signing key for the run and nothing here holds a
secret, a document or a wheel. Synthetic data only; nothing leaves the stack.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path("/tmp/plugin-index")  # noqa: S108 - the tmpfs of the simulated server's own container
INDEX_HOST = "plugins.e2e.test"
CATALOGUE_HOST = "catalogue.exemple.invalid"  # the address the example plugin asks, as it ships
HOSTS = (INDEX_HOST, CATALOGUE_HOST)

Reply = tuple[int, dict[str, str], bytes]

KINDS = {
    ".json": "application/json",
    ".whl": "application/octet-stream",
    ".minisig": "text/plain",
}


def handle(host: str, path: str) -> Reply:
    """The file the test published for this host and path, else a 404."""
    relative = Path(path.lstrip("/"))
    if ".." in relative.parts or not relative.parts:
        return 404, {}, b""
    target = ROOT / host / relative
    if not target.is_file():
        return 404, {}, b""
    kind = KINDS.get(target.suffix, "application/octet-stream")
    return 200, {"content-type": kind}, target.read_bytes()
