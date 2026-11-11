"""M2 L03, second review: each proof of concept of the counter-review is a regression test.

Each test asserts the SAFE behaviour."""

import io
import os
import signal
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

import shelfhold.library.adapters.epub_derivative as mod
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust import (
    DERIVED_BY,
    NeutralizationError,
    epub_payload_hash,
    neutralize_epub,
)
from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import (
    EpubRefusedError,
    OriginalNotServableError,
    StalePlanError,
    servable_epub,
)
from shelfhold.sdk.testing.corpus.trapped_epubs.build import (
    CONTAINER,
    NAV,
    STYLE,
    TINY_PNG,
    Member,
    epub_bytes,
    opf,
    xhtml,
    zip_bytes,
)
from tests.integration.test_untrusted_epub_pipeline import (
    CLEAN,
    TRAPPED,
    V2,
    ingest,
    reneutralizer,
    store,
)
from tests.library_env import lib_env

pytestmark = pytest.mark.req("EF-BIB-051")

EVIL = xhtml('<p>x</p><script>alert(document.cookie)</script><img src="x" onerror="alert(1)"/>')
ACTIVE = (b"<script", b"onerror")


def zentries(path: Path | bytes) -> dict[str, bytes]:
    src = io.BytesIO(path) if isinstance(path, bytes) else path
    with zipfile.ZipFile(src) as z:
        return {n: z.read(n) for n in z.namelist()}


def active_entries(data: bytes) -> list[str]:
    return [n for n, b in zentries(data).items() if any(a in b for a in ACTIVE)]


def derive(data: bytes) -> bytes:
    out = io.BytesIO()
    neutralize_epub(io.BytesIO(data), out)
    return out.getvalue()


def assert_safe(data: bytes) -> None:
    """Either refused, or the derivative has no active markup at all."""
    try:
        got = derive(data)
    except NeutralizationError:
        return
    leaked = active_entries(got)
    assert not leaked, f"raw active content copied: {leaked}"


CH = xhtml("<p>chapter</p>")


def book(opf_bytes: bytes, extra: list[Member], container: bytes = CONTAINER,
         opf_name: str = "OEBPS/content.opf") -> bytes:  # fmt: skip
    return zip_bytes([
        Member("mimetype", b"application/epub+zip", deflate=False),
        Member("META-INF/container.xml", container),
        Member(opf_name, opf_bytes),
        Member("OEBPS/nav.xhtml", NAV),
        Member("OEBPS/ch1.xhtml", CH),
        Member("OEBPS/style.css", STYLE),
        Member("OEBPS/cover.png", TINY_PNG, deflate=False),
        *extra,
    ])  # fmt: skip


def spine_opf(items: str, idrefs: list[str]) -> bytes:
    body = opf(items).decode()
    refs = "".join(f'<itemref idref="{i}"/>' for i in idrefs)
    return body.replace('<itemref idref="ch1"/>', '<itemref idref="ch1"/>' + refs).encode()


# -- 1. the original POCs, replayed ---------------------------------------------------------------

META_EPUB = epub_bytes(
    CH,
    opf_extra='<item id="evil" href="../META-INF/evil.xhtml" media-type="application/xhtml+xml"/>',
    extra=[Member("META-INF/evil.xhtml", EVIL)],
)


def test_poc1_meta_inf_page_refused() -> None:
    with lib_env() as env, pytest.raises(EpubRefusedError):
        ingest(env, META_EPUB)


def test_poc1b_meta_inf_page_not_in_manifest_is_neutralized() -> None:
    assert_safe(epub_bytes(CH, extra=[Member("META-INF/evil.xhtml", EVIL)]))


def test_poc2_restore_from_destination_refuses_active_meta_inf_page() -> None:
    raw = zip_bytes([  # the PoC2 derivative cannot be made any more: build it by hand
        Member("mimetype", b"application/epub+zip", deflate=False),
        Member("META-INF/container.xml", CONTAINER),
        Member("OEBPS/content.opf", opf('<item id="evil" href="../META-INF/evil.xhtml" '
                                       'media-type="application/xhtml+xml"/>')),
        Member("OEBPS/nav.xhtml", NAV), Member("OEBPS/ch1.xhtml", CH),
        Member("OEBPS/style.css", STYLE), Member("OEBPS/cover.png", TINY_PNG, deflate=False),
        Member("META-INF/evil.xhtml", EVIL),
    ])  # fmt: skip
    with lib_env() as env:
        forged = env.stage(raw, "found.epub")
        payload = epub_payload_hash(safe_open_archive(forged, name="x.epub"))
        with pytest.raises(OriginalNotServableError):
            store(env).restore_from_destination(
                unit_id=env.chain["unit"], variant_id=env.chain["variant"], found=forged,
                dest=str(env.manga / "Book" / "Book.epub"), expected_derived_hash=payload,
                derived_by=DERIVED_BY,
            )  # fmt: skip


def test_poc3_regenerate_never_overwrites_user_file() -> None:
    with lib_env() as env:
        _r, library = ingest(env)
        user = epub_bytes(xhtml("<p>my own corrected edition</p>"))
        library.write_bytes(user)
        r = reneutralizer(env)
        plan = r.plan()
        assert plan.items[0].source == "blocked"
        assert plan.items[0].reason == "library_file_was_replaced"
        r.apply(plan.fingerprint)
        assert library.read_bytes() == user


# -- 1. variants ----------------------------------------------------------------------------------


@pytest.mark.parametrize("folder", ["meta-inf", "Meta-Inf", "META-inf"])
def test_meta_inf_other_case_page(folder: str) -> None:
    data = epub_bytes(
        CH,
        opf_extra=f'<item id="evil" href="../{folder}/evil.xhtml" '
        'media-type="application/xhtml+xml"/>',
        extra=[Member(f"{folder}/evil.xhtml", EVIL)],
    )
    assert_safe(data)


@pytest.mark.parametrize(
    "href",
    [
        "./../META-INF/evil.xhtml",
        "../META-INF/./evil.xhtml",
        "..%2FMETA-INF%2Fevil.xhtml",
        "%2E%2E/META-INF/evil.xhtml",
        "../META-INF%2Fevil.xhtml",
        "../%4DETA-INF/evil.xhtml",
        "../OEBPS/../META-INF/evil.xhtml",
        ".//../META-INF/evil.xhtml",
    ],
)
def test_meta_inf_dot_and_percent_hrefs(href: str) -> None:
    data = epub_bytes(
        CH,
        opf_extra=f'<item id="evil" href="{href}" media-type="application/xhtml+xml"/>',
        extra=[Member("META-INF/evil.xhtml", EVIL)],
    )
    assert_safe(data)


@pytest.mark.parametrize("entry", ["META-INF/./evil.xhtml", "./META-INF/evil.xhtml",
                                   "META-INF//evil.xhtml"])  # fmt: skip
def test_meta_inf_dot_entry_names(entry: str) -> None:
    data = epub_bytes(
        CH,
        opf_extra='<item id="evil" href="../META-INF/evil.xhtml" '
        'media-type="application/xhtml+xml"/>',
        extra=[Member(entry, EVIL)],
    )
    with pytest.raises(NeutralizationError):  # the item names a missing entry or META-INF
        derive(data)
    assert_safe(epub_bytes(CH, extra=[Member(entry, EVIL)]))


CONTAINER_XHTML = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    b'<rootfiles><rootfile full-path="OEBPS/content.opf" '
    b'media-type="application/oebps-package+xml"/></rootfiles>'
    b'<html xmlns="http://www.w3.org/1999/xhtml"><body><script>alert(1)</script></body></html>'
    b"</container>"
)
CONTAINER_NONS = CONTAINER.replace(
    b"</container>", b'<img src="../OEBPS/cover.png" onerror="alert(1)"/></container>'
)


@pytest.mark.parametrize("container", [CONTAINER_XHTML, CONTAINER_NONS])
@pytest.mark.parametrize("name", ["container.xml", "manifest.xml", "metadata.xml"])
def test_known_meta_inf_name_holding_markup_named_by_the_spine(container: bytes, name: str) -> None:
    items = f'<item id="evil" href="../META-INF/{name}" media-type="application/xhtml+xml"/>'
    extra = [] if name == "container.xml" else [Member(f"META-INF/{name}", container)]
    data = book(spine_opf(items, ["evil"]), extra,
                container=container if name == "container.xml" else CONTAINER)  # fmt: skip
    assert_safe(data)


def test_rootfile_in_meta_inf_benign_is_accepted() -> None:
    cont = CONTAINER.replace(b"OEBPS/content.opf", b"META-INF/content.opf")
    body = opf().decode()
    for h in ("nav.xhtml", "ch1.xhtml", "style.css", "cover.png"):
        body = body.replace(f'href="{h}"', f'href="../OEBPS/{h}"')
    data = book(body.encode(), [], container=cont, opf_name="META-INF/content.opf")
    derive(data)  # accepted (not a page): not a finding either way


def test_rootfile_in_meta_inf_listing_a_meta_inf_page() -> None:
    cont = CONTAINER.replace(b"OEBPS/content.opf", b"META-INF/content.opf")
    body = spine_opf('<item id="evil" href="evil.xhtml" media-type="application/xhtml+xml"/>',
                     ["evil"]).decode()  # fmt: skip
    for h in ("nav.xhtml", "ch1.xhtml", "style.css", "cover.png"):
        body = body.replace(f'href="{h}"', f'href="../OEBPS/{h}"')
    data = book(body.encode(), [Member("META-INF/evil.xhtml", EVIL)], container=cont,
                opf_name="META-INF/content.opf")  # fmt: skip
    assert_safe(data)


def test_symlink_entry_refused() -> None:
    data = epub_bytes(CH, extra=[Member("OEBPS/link.xhtml", b"../META-INF/evil.xhtml",
                                        attr=(0o120777 << 16))])  # fmt: skip
    with pytest.raises(NeutralizationError):
        derive(data)


@pytest.mark.parametrize("dup", ["META-INF/container.xml", "meta-inf/container.xml",
                                 "OEBPS/ch1.xhtml", "OEBPS/CH1.xhtml", "mimetype"])  # fmt: skip
def test_duplicate_names_refused(dup: str) -> None:
    payload = EVIL if dup.endswith(".xhtml") else CONTAINER_XHTML
    data = epub_bytes(CH, extra=[Member(dup, payload)])
    with pytest.raises(NeutralizationError):
        derive(data)


# -- 1. new: entries copied raw because they are "OPF" ---------------------------------------------

EVIL_NONS = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
    b'<div><img src="cover.png" onerror="alert(document.cookie)"/></div></package>'
)


def test_second_rootfile_with_html_extension_is_copied_raw() -> None:
    """A second rootfile names `OEBPS/evil.html`; the first OPF lists it as an XHTML page in the
    spine. Every rootfile is `copy`: the page is never neutralized."""
    cont = CONTAINER.replace(
        b"</rootfiles>",
        b'<rootfile full-path="OEBPS/evil.html" media-type="application/oebps-package+xml"/>'
        b"</rootfiles>",
    )
    items = '<item id="evil" href="evil.html" media-type="application/xhtml+xml"/>'
    data = book(spine_opf(items, ["evil"]), [Member("OEBPS/evil.html", EVIL_NONS)], container=cont)
    assert_safe(data)


def test_an_opf_listed_as_a_spine_page_is_copied_raw() -> None:
    items = '<item id="evil" href="evil.opf" media-type="application/xhtml+xml"/>'
    data = book(spine_opf(items, ["evil"]), [Member("OEBPS/evil.opf", EVIL_NONS)])
    assert_safe(data)


def test_html_rootfile_reaches_library_and_is_servable() -> None:
    cont = CONTAINER.replace(
        b"</rootfiles>",
        b'<rootfile full-path="OEBPS/evil.html" media-type="application/oebps-package+xml"/>'
        b"</rootfiles>",
    )
    items = '<item id="evil" href="evil.html" media-type="application/xhtml+xml"/>'
    data = book(spine_opf(items, ["evil"]), [Member("OEBPS/evil.html", EVIL_NONS)], container=cont)
    with lib_env() as env:
        try:
            _r, _library = ingest(env, data)
        except EpubRefusedError:
            return
        s = servable_epub(env.engine, env.fsops, env.chain["unit"])
        leaked = active_entries(Path(s.path).read_bytes())
        assert not leaked, f"servable derivative holds raw active entries {leaked}"


def test_destination_adds_an_opf_page_outside_the_payload_hash() -> None:
    """restore_from_destination: the payload hash leaves every `*.opf` out, the clean check copies
    every OPF raw: a destination may add `evil.opf` and list it in the spine."""
    with lib_env() as env:
        good = derive(CLEAN)
        payload = epub_payload_hash(safe_open_archive(io.BytesIO(good), name="x.epub"))
        z = zentries(good)
        items = '<item id="evil" href="evil.opf" media-type="application/xhtml+xml"/>'
        z["OEBPS/content.opf"] = spine_opf(items, ["evil"])
        forged = zip_bytes(
            [Member("mimetype", z.pop("mimetype"), deflate=False)]
            + [Member(n, b) for n, b in z.items()]
            + [Member("OEBPS/evil.opf", EVIL_NONS)]
        )
        f = env.stage(forged, "found.epub")
        try:
            store(env).restore_from_destination(
                unit_id=env.chain["unit"], variant_id=env.chain["variant"], found=f,
                dest=str(env.manga / "Book" / "Book.epub"), expected_derived_hash=payload,
                derived_by=DERIVED_BY,
            )  # fmt: skip
        except OriginalNotServableError:
            return
        s = servable_epub(env.engine, env.fsops, env.chain["unit"])
        leaked = active_entries(Path(s.path).read_bytes())
        assert not leaked, f"restored and servable with {leaked}"


# -- 2. regenerate ------------------------------------------------------------------------------


def _mutate(source: Any) -> io.BytesIO:
    data = source.read() if hasattr(source, "read") else Path(source).read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for info in zin.infolist():
            body = zin.read(info.filename)
            if info.filename.endswith("ch1.xhtml"):
                body = body.replace(b"<p>", b"<p class='v2'>", 1)
            zout.writestr(info, body)
    buf.seek(0)
    return buf


def v2m(s: Any, o: Any, **kw: Any) -> Any:
    return replace(neutralize_epub(_mutate(s), o, **kw), derived_by=V2)


def _with_opf(data: bytes, new_opf: bytes) -> bytes:
    z = zentries(data)
    z["OEBPS/content.opf"] = new_opf
    return zip_bytes([Member("mimetype", z.pop("mimetype"), deflate=False)]
                     + [Member(n, b) for n, b in z.items()])  # fmt: skip


def test_forged_ours_user_edited_opf_is_overwritten() -> None:
    """The user edits the metadata (OPF only, as Calibre does): the payload is unchanged, so the
    file counts as Shelfhold's and the user's edit is lost."""
    with lib_env() as env:
        _r, library = ingest(env)
        mine = _with_opf(library.read_bytes(), opf().replace(b"Synthetic", b"My Title"))
        library.write_bytes(mine)
        r = reneutralizer(env, neutralize=v2m)
        plan = r.plan()
        r.apply(plan.fingerprint)
        assert b"My Title" in zentries(library)["OEBPS/content.opf"], "user OPF edit overwritten"


def test_race_user_replaces_file_during_regeneration() -> None:
    """Under the Work lock: the user (who does not take it) replaces the file after `_ours`."""
    with lib_env() as env:
        _r, library = ingest(env)
        user = epub_bytes(xhtml("<p>my own corrected edition</p>"))

        def slow(s: Any, o: Any, **kw: Any) -> Any:
            library.write_bytes(user)  # between the check and the replacement
            return v2m(s, o, **kw)

        r = reneutralizer(env, neutralize=slow)
        r.apply(r.plan().fingerprint)
        assert library.read_bytes() == user, "user file replaced after the check"


def test_plan_confirmed_after_user_replaced_file_is_stale() -> None:
    with lib_env() as env:
        _r, library = ingest(env)
        r = reneutralizer(env, neutralize=v2m)
        plan = r.plan()
        user = epub_bytes(xhtml("<p>my own corrected edition</p>"))
        library.write_bytes(user)
        with pytest.raises(StalePlanError):
            r.apply(plan.fingerprint)
        assert library.read_bytes() == user


def test_plan_confirmed_after_store_original_lost() -> None:
    with lib_env() as env:
        ingest(env)
        r = reneutralizer(env, neutralize=v2m)
        plan = r.plan()
        assert plan.items[0].source == "original"
        for f in env.files_under(env.store):
            f.write_bytes(b"gone")
        with pytest.raises(StalePlanError):
            r.apply(plan.fingerprint)


# -- 3. SIGKILL at every step of the regeneration ----------------------------------------------

STEPS = ["before_write", "after_rename", "before_rewrite_rows", "after_rewrite_rows",
         "in_rollback", "after_rollback"]  # fmt: skip


def _kill() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _child(env: Any, step: str, with_store: bool) -> None:
    env.engine.dispose(close=False)
    fs = env.fsops
    real_write = fs.write_atomic
    real_rewrite = mod.record_library_rewrite
    real_set = mod.UntrustedEpubStore._set_generation
    calls = {"set": 0}

    def write(local: str, data: Any, **kw: Any) -> Any:
        if "/library/" in local and kw.get("replace", True):
            if step == "before_write":
                _kill()
            if step in ("in_rollback", "after_rollback"):
                raise OSError("disk full")
            got = real_write(local, data, **kw)
            if step == "after_rename":
                _kill()
            return got
        return real_write(local, data, **kw)

    def rewrite(*a: Any, **k: Any) -> Any:
        if step == "before_rewrite_rows":
            _kill()
        real_rewrite(*a, **k)
        if step == "after_rewrite_rows":
            _kill()  # inside the transaction, before its commit

    def set_gen(self: Any, *a: Any, **k: Any) -> Any:
        calls["set"] += 1
        if step == "in_rollback" and calls["set"] == 2:
            _kill()
        real_set(self, *a, **k)
        if step == "after_rollback" and calls["set"] == 2:
            _kill()

    fs.write_atomic = write  # type: ignore[method-assign]
    mod.record_library_rewrite = rewrite  # type: ignore[assignment]
    mod.UntrustedEpubStore._set_generation = set_gen  # type: ignore[method-assign]
    r = reneutralizer(env, with_store=with_store, neutralize=v2m)
    try:
        r.apply(r.plan().fingerprint)
    finally:
        os._exit(3)


@pytest.mark.parametrize("with_store", [True, False])
@pytest.mark.parametrize("step", STEPS)
def test_sigkill_during_regeneration(step: str, with_store: bool) -> None:
    with lib_env() as env:
        ingest(env, with_store=with_store)
        env.engine.dispose()
        pid = os.fork()
        if pid == 0:
            _child(env, step, with_store)
        _p, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status
        env.engine.dispose()
        # never an original (or anything active) served
        try:
            s = servable_epub(env.engine, env.fsops, env.chain["unit"])
            assert not active_entries(Path(s.path).read_bytes())
            served = True
        except OriginalNotServableError:
            served = False
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
        r = reneutralizer(env, with_store=with_store, neutralize=v2m)
        plan = r.plan()
        state = (served, row.derived_by, [(i.source, i.reason) for i in plan.items])
        blocked = [i for i in plan.items if i.source == "blocked"]
        assert not blocked, f"Unit blocked after kill at {step}: {state}"
        if not served:  # not servable: the migration must still see it, and repair it
            assert plan.items, f"not servable and absent from the plan at {step}: {state}"
            r.apply(plan.fingerprint)
            servable_epub(env.engine, env.fsops, env.chain["unit"])


# -- ingest crash: file in /library, no rows ----------------------------------------------------


def test_sigkill_between_library_file_and_rows_on_ingest() -> None:
    with lib_env() as env:
        env.engine.dispose()
        dest = env.manga / "Book" / "Book.epub"
        staged = env.stage(TRAPPED, "received.epub")
        pid = os.fork()
        if pid == 0:
            env.engine.dispose(close=False)
            real = mod.UntrustedEpubStore._record_kept

            def kill(*a: Any, **k: Any) -> None:
                _kill()

            mod.UntrustedEpubStore._record_kept = kill  # type: ignore[method-assign]
            try:
                store(env).ingest(unit_id=env.chain["unit"], variant_id=env.chain["variant"],
                                  staged=staged, dest=str(dest))  # fmt: skip
            finally:
                mod.UntrustedEpubStore._record_kept = real  # type: ignore[method-assign]
                os._exit(3)
        os.waitpid(pid, 0)
        env.engine.dispose()
        assert dest.exists()
        assert not active_entries(dest.read_bytes())  # what is there is the derivative
        from shelfhold.infrastructure.schema import coherence

        with env.engine.begin() as c:  # the lease of the killed job has expired
            c.execute(sa.delete(coherence.resource_lock))
        staged = env.stage(TRAPPED, "received.epub")
        # a retry of the same release must be able to file it
        store(env).ingest(unit_id=env.chain["unit"], variant_id=env.chain["variant"],
                          staged=staged, dest=str(dest))  # fmt: skip


def test_an_orphan_derivative_of_a_killed_filing_is_reused_by_the_retry() -> None:
    """Ingest killed after the library file, before the rows: the retry of the same release files
    the very bytes it finds instead of failing with FileExistsError."""
    with lib_env() as env:
        _r, library = ingest(env)
        data = library.read_bytes()
        with env.engine.begin() as c:
            c.execute(sa.delete(storage.replica))
            c.execute(sa.delete(storage.content))
        for f in env.files_under(env.store):
            f.unlink()
        again = store(env).ingest(
            unit_id=env.chain["unit"], variant_id=env.chain["variant"],
            staged=env.stage(TRAPPED, "received.epub"), dest=str(library),
        )  # fmt: skip
        assert library.read_bytes() == data
        assert again.derived_hash


def test_store_copies_leaves_the_untrusted_originals_out_unless_asked() -> None:
    from shelfhold.library.replica import store_copies

    with lib_env() as env:
        ingest(env)
        with env.engine.connect() as c:
            assert not list(store_copies(c, env.chain["unit"]))
            assert len(list(store_copies(c, env.chain["unit"], include_untrusted=True))) == 1
