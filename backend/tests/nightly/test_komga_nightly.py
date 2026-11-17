"""Nightly against the official Komga image (ENF-COMP-001, EF-EXP-020).

Not required to merge a pull request: `.github/workflows/nightly.yml` runs it, with `KOMGA_IMAGE`
set. No real content source: every file is built by the test. Run by hand:
`KOMGA_IMAGE=<image> uv run pytest -m nightly tests/nightly/test_komga_nightly.py`.

What the nightly exercises is the contract of `KomgaClient` against the real server: the version
it says, the libraries and the accounts it reads, the scan of the library that holds a folder
(Komga has no scan of a folder) and the trash emptied after a book the host removed. The journey
of a shelf exported to Komga is another lot's: no route of Shelfhold creates a Komga target yet
(M4 L08a, gap 1).
"""

from __future__ import annotations

import pytest

from shelfhold.export.targets.komga import KomgaNoLibrary

from .komga_real import BOOTSTRAP, RealKomga, cbz, comicinfo, run

pytestmark = pytest.mark.nightly


@pytest.mark.req("ENF-COMP-001", "EF-EXP-020")
def test_komga_nightly_client_contract(komga: RealKomga) -> None:
    """The adapter against the real server: the version, the libraries and the accounts it reads,
    the scan asked for one folder that reads the whole library (a series of another library is
    left alone), and the trash emptied after the host removed a book — the other library keeps
    its own."""
    client = komga.client
    assert client is not None
    komga.write("alpha/Series Alpha/Series Alpha - Ch.1.cbz", cbz(comicinfo("Series Alpha", "1")))
    komga.write("alpha/Series Gamma/Series Gamma - Ch.1.cbz", cbz(comicinfo("Series Gamma", "1")))
    komga.write("beta/Series Beta/Series Beta - Ch.1.cbz", cbz(comicinfo("Series Beta", "1")))
    alpha = komga.create_library("Alpha", komga.remote("alpha"))
    beta = komga.create_library("Beta", komga.remote("beta"))

    # what the connection reads: the version, the libraries, the accounts of the audit
    assert run(client.probe()), "the server said no version"
    libraries = run(client.list_libraries())
    assert {(lib.name, lib.root) for lib in libraries} == {
        ("Alpha", komga.remote("alpha")),
        ("Beta", komga.remote("beta")),
    }
    accounts = run(client.list_accounts())
    assert [account.username for account in accounts] == [BOOTSTRAP["admin"]["email"]]
    assert all(account.is_admin for account in accounts), accounts

    # the library that holds a folder, by components: a folder no library holds is an error,
    # never a scan of everything
    assert run(client.library_of(komga.remote("alpha/Series Alpha"))) == alpha
    assert run(client.library_of(komga.remote("beta/Series Beta"))) == beta
    with pytest.raises(KomgaNoLibrary):
        run(client.library_of("/elsewhere/Series Alpha"))

    # a scan is the scan of a library: one folder asked for, both series of the library read,
    # the other library left alone
    run(client.request_scan_for_path(komga.remote("alpha/Series Alpha")))
    assert run(client.flush_scans(now=True)) == [alpha]
    found = komga.wait_series(
        "Series", lambda s: {"Series Alpha", "Series Gamma"} <= set(s), "the scanned library"
    )
    assert "Series Beta" not in found, "the other library was scanned too"
    assert found["Series Alpha"]["deleted"] is False

    # the host removed a book: the library is scanned, then its trash emptied (the scan runs at
    # the server's own pace, so the emptying is asked again once it has marked the series)
    for path in (komga.library_dir / "alpha" / "Series Gamma").iterdir():
        path.unlink()
    client.note_deletion(alpha)
    assert run(client.flush_scans(now=True)) == [alpha]
    komga.wait_series(
        "Series Gamma",
        lambda s: not s or s["Series Gamma"]["deleted"] is True,
        "the scan marked the removed series",
    )
    client.note_deletion(alpha)
    assert run(client.flush_scans(now=True)) == [alpha]
    komga.wait_series("Series Gamma", lambda s: not s, "the trash of the library emptied")
    # the library keeps what it holds, and the other library was never asked anything
    settled = komga.wait_series(
        "Series", lambda s: "Series Alpha" in s, "the series the library keeps"
    )
    assert "Series Gamma" not in settled
    assert "Series Beta" not in settled
    assert settled["Series Alpha"]["deleted"] is False
