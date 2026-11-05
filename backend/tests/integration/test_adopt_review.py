"""Regressions of the review of PR #54 (L29): every case is a proof of concept that now fails."""

import contextlib
import zipfile

import pytest

from shelfhold.export.targets.kavita import ConfirmationRequired
from shelfhold.export.targets.kavita.adopt import PreviewStale, number_of, read_comicinfo
from shelfhold.export.targets.kavita.dto import LibraryType, ParseResult
from shelfhold.export.targets.kavita.wiring import TargetUrlRefused, checked_target_url
from shelfhold.infrastructure.schema import export
from tests.adopt_env import adoption, comicinfo
from tests.export_env import export_env
from tests.fakes_fetcher import FakeResolver
from tests.integration.test_adopt import _count
from tests.kavita_env import run
from tests.library_env import cbz_bytes


def _read(name: str = "x", chapter: float = 0.0, volume: float = 0.0) -> ParseResult:
    return ParseResult(name, None, chapter, chapter, volume, volume)


@pytest.mark.req("EF-EXP-028")
def test_confirmation_names_the_fingerprint_of_the_preview() -> None:
    """The preview and the adoption are two reads: what changed between them is not adopted."""
    with export_env() as env:
        a = adoption(env)
        a.add_series("Dr Stone", ["Dr Stone - Ch.1.cbz"], info=comicinfo("Dr Stone"))
        client, destination = a.wired.client, env.destination_id
        seen = run(a.adopter.preview(client, destination))
        assert seen.fingerprint == run(a.adopter.preview(client, destination)).fingerprint
        with pytest.raises(ConfirmationRequired):  # no fingerprint, no adoption
            run(a.adopter.adopt(client, destination, confirm=None))
        a.add_series("Autre", ["Autre - Ch.1.cbz", "Autre - Ch.2.cbz"])
        with pytest.raises(PreviewStale):  # a series came in after the preview
            run(a.adopter.adopt(client, destination, confirm=seen.fingerprint))
        assert _count(env, export.export_record) == 0 and a.wired.fake.writes == []
        fresh = run(a.adopter.preview(client, destination))
        assert fresh.fingerprint != seen.fingerprint
        assert (
            run(a.adopter.adopt(client, destination, confirm=fresh.fingerprint)).adopted_files == 3
        )


@pytest.mark.req("EF-EXP-028")
def test_a_file_changed_after_the_preview_is_not_adopted() -> None:
    with export_env() as env:
        a = adoption(env)
        a.add_series("Dr Stone", ["Dr Stone - Ch.1.cbz"], info=comicinfo("Dr Stone"))
        client, destination = a.wired.client, env.destination_id
        seen = run(a.adopter.preview(client, destination))
        path = env.root / "Dr Stone" / "Dr Stone - Ch.1.cbz"
        path.write_bytes(path.read_bytes() + b"\0")  # same name, other size and mtime
        with pytest.raises(PreviewStale):
            run(a.adopter.adopt(client, destination, confirm=seen.fingerprint))


@pytest.mark.req("EF-EXP-028")
@pytest.mark.parametrize("number", ["NaN", "sNaN", "Infinity", "-Infinity", "1e4000", "1" * 40])
def test_an_absurd_comicinfo_number_is_ignored_without_an_exception(number: str) -> None:
    assert number_of(_read(), {"Number": number}) == ("unknown", "", "filename")
    assert number_of(_read(), {"Number": "12"}) == ("chapter", "12", "comicinfo")


@pytest.mark.req("EF-EXP-028")
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1e300])
def test_an_absurd_parsed_number_is_ignored(value: float) -> None:
    assert number_of(_read(chapter=value), {}) == ("unknown", "", "filename")


@pytest.mark.req("EF-EXP-028")
def test_comicinfo_is_found_whatever_its_case() -> None:
    with export_env() as env:
        pages = (("a.png", b"\x89PNG\r\n\x1a\n"),)
        path = env.root / "x.cbz"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a.png", pages[0][1])
            zf.writestr("comicinfo.XML", comicinfo("Dr Stone", number="4"))
        assert read_comicinfo(env.lib.fsops, str(path))["Series"] == "Dr Stone"
        plain = env.root / "y.cbz"
        plain.write_bytes(cbz_bytes(pages))
        assert read_comicinfo(env.lib.fsops, str(plain)) == {}


@pytest.mark.req("EF-INT-014", "EF-EXP-045")
def test_replay_needs_the_names_read_in_bulk() -> None:
    """The environment's own `parse` raises: only `parse-bulk` answers can place a file."""
    from shelfhold.export.targets.kavita.adopt import BulkParse

    def refuse(name: str) -> object:
        raise AssertionError(f"{name!r} read without plugin/parse")

    with export_env() as env:
        a = adoption(env)
        a.add_series("Dr Stone", ["Dr Stone - Ch.1.cbz"], info=comicinfo("Dr Stone"))
        a.adopt()
        env.add_chapter(env.new_series("Blue Lock"), "9")
        env.pipeline.bind_parse(refuse, verified=True)  # type: ignore[arg-type]
        with pytest.raises(AssertionError):
            env.pipeline.run(env.destination_id, job_id="01J00000000000000000000000")
        names, _ = env.pipeline.wanted_names(env.destination_id)
        assert "Blue Lock - Ch.9.cbz" in names
        before = len(a.wired.fake.parse_calls)
        bulk = run(BulkParse.for_plan(a.wired.client, env.pipeline, env.destination_id))
        asked = {c["name"] for c in a.wired.fake.parse_calls[before:]}
        assert set(names) <= asked  # every name the plan needs went through parse-bulk
        env.pipeline.bind_parse(bulk, verified=True)
        env.pipeline.run(env.destination_id, job_id="01J00000000000000000000000")
        assert (env.root / "Blue Lock" / "Blue Lock - Ch.9.cbz").exists()


@pytest.mark.req("EF-EXP-045")
def test_the_simulated_parse_is_the_manga_one_only() -> None:
    with export_env() as env:
        a = adoption(env)
        for kind in (LibraryType.COMIC, LibraryType.BOOK):
            with contextlib.suppress(Exception):
                run(a.wired.client.parse_bulk(["Dr Stone - Ch.1.cbz"], kind))
        assert len(a.wired.fake.violations) == 2  # never a silent Manga answer for another type


@pytest.mark.req("EF-EXP-009")
@pytest.mark.parametrize(
    ("url", "table"),
    [
        ("http://8.8.8.8:80", {"8.8.8.8": ["8.8.8.8"]}),
        ("https://93.184.215.14", {"93.184.215.14": ["93.184.215.14"]}),
        ("https://kavita.example", {"kavita.example": ["93.184.215.14"]}),  # a public name
        ("http://kavita.lan", {"kavita.lan": ["192.168.1.5", "8.8.8.8"]}),  # one public answer
        ("ftp://192.168.1.5", {"192.168.1.5": ["192.168.1.5"]}),
        ("http://user:pw@192.168.1.5", {"192.168.1.5": ["192.168.1.5"]}),
        ("http://192.168.1.5?x=1", {"192.168.1.5": ["192.168.1.5"]}),
    ],
)
def test_the_target_url_must_stay_on_the_local_network(
    url: str, table: dict[str, list[str]]
) -> None:
    with pytest.raises(TargetUrlRefused):
        run(checked_target_url(url, FakeResolver(table)))


@pytest.mark.req("EF-EXP-009")
def test_a_lan_target_url_is_accepted() -> None:
    resolver = FakeResolver({"192.168.1.50": ["192.168.1.50"], "kavita.lan": ["10.0.0.4"]})
    assert run(checked_target_url("http://192.168.1.50:5000", resolver)) == ("192.168.1.50", 5000)
    assert run(checked_target_url("https://kavita.lan", resolver)) == ("kavita.lan", 443)
