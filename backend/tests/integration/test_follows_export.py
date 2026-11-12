"""EF-INT-027 (M3): `shelfhold follow export --format opml|csv` writes the followed series.

The renderers are attacked with hostile text (XML and CSV metacharacters, formulas, control
characters), the collector with a source URL that carries credentials, and the command runs for
real over a real instance."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.follows_export import (
    FollowRow,
    collect_follows,
    export_follows,
    public_url,
    render_csv,
    render_opml,
)
from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, identity
from shelfhold.infrastructure.xml.safe_parse import safe_parse_xml
from tests.auth_env import run_cli
from tests.l51_env import Live, live

NASTY = "Dr. \"Stone\" & <Co>, 'vol'; é 石\nligne 2"


def _follow(api: Live) -> None:
    with api.env.inst.engine.begin() as connection:
        connection.execute(
            sa.insert(acquisition.series_follow).values(
                edition_id=api.series.edition_id, work_id=api.series.work_id
            )
        )


def _set_url(api: Live, url: str) -> None:
    with api.env.inst.engine.begin() as connection:
        connection.execute(sa.update(acquisition.source_item).values(url=url))


def _mark_read(api: Live, unit_id: str) -> None:
    """A reader finished the Unit: `reading_progress` says `read` (a one-Unit carrier)."""
    user_id, carrier = new_ulid(), new_ulid()
    with api.env.inst.engine.begin() as connection:
        connection.execute(
            sa.insert(identity.user).values(id=user_id, username=f"reader-{user_id}", role="member")
        )
        connection.execute(
            sa.text(
                "INSERT INTO unit_coverage (carrier_type, carrier_id, unit_id, position, origin) "
                "VALUES ('content', :c, :u, 1, 'manual')"
            ),
            {"c": carrier, "u": unit_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO reading_position (id, user_id, carrier_type, carrier_id, "
                "content_hash, locator, percent, client_ts, server_ts) VALUES "
                "(:id, :user, 'content', :c, :hash, '{}', 1.0, :ts, :ts)"
            ),
            {
                "id": new_ulid(), "user": user_id, "c": carrier, "hash": "sha256:" + "0" * 64,
                "ts": "2026-01-01T00:00:00+00:00",
            },
        )  # fmt: skip


def _opml_outlines(text: str) -> list[dict[str, str]]:
    root = safe_parse_xml(text.encode())
    assert root.tag == "opml"
    assert root.get("version") == "2.0"
    return [dict(o.attrib) for o in root.iter("outline")]


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


@pytest.mark.req("EF-INT-027")
def test_follows_export_opml_csv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=("1", "2")) as api:
        _follow(api)
        _set_url(api, "https://example.org/series/dr-stone")
        capsys.readouterr()

        assert run_cli(monkeypatch, settings, "follow", "export", "--format", "opml") == 0
        outlines = _opml_outlines(capsys.readouterr().out)
        assert outlines == [
            {
                "text": "Dr Stone",
                "type": "link",
                "url": "https://example.org/series/dr-stone",
                "source": "suwayomi:1",
                "lastNumber": "",
            }
        ]

        assert run_cli(monkeypatch, settings, "follow", "export", "--format", "csv") == 0
        assert _csv_rows(capsys.readouterr().out) == [
            ["title", "source", "url", "last_number"],
            ["Dr Stone", "suwayomi:1", "https://example.org/series/dr-stone", ""],
        ]

        # the default format is OPML
        assert run_cli(monkeypatch, settings, "follow", "export") == 0
        assert _opml_outlines(capsys.readouterr().out)[0]["text"] == "Dr Stone"


@pytest.mark.req("EF-INT-027")
def test_follows_export_last_number_read(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The last number read is the highest read Unit, not the highest known one; before any
    reading it is empty, never a guess."""
    with live(monkeypatch, settings, tmp_path, chapters=()) as api:
        units = {n: api.env.add_chapter(api.series, n) for n in ("1", "2", "10", "11")}
        _follow(api)
        engine = api.env.inst.engine
        assert [r.last_number for r in collect_follows(engine)] == [""]  # nothing read
        _mark_read(api, units["2"])
        _mark_read(api, units["10"])
        # numeric order, not text order ("2" > "10" as text); chapter 11 is known, unread
        assert [r.last_number for r in collect_follows(engine)] == ["10"]


@pytest.mark.req("EF-INT-027")
def test_follows_export_holds_no_source_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=("1",)) as api:
        _follow(api)
        _set_url(
            api,
            "https://reader:hunter2@example.org:8443/s/1?id=42&api_key=KEYVALUE"
            "&access_token=TOKENVALUE&sessionid=SESSVALUE#frag",
        )
        capsys.readouterr()
        outputs = []
        for fmt in ("opml", "csv"):
            assert run_cli(monkeypatch, settings, "follow", "export", "--format", fmt) == 0
            outputs.append(capsys.readouterr().out)
    for out in outputs:
        assert "https://example.org:8443/s/1?id=42" in out
        for leaked in ("hunter2", "reader", "KEYVALUE", "TOKENVALUE", "SESSVALUE", "frag"):
            assert leaked not in out


@pytest.mark.req("EF-INT-027")
def test_follows_export_best_binding_and_no_unfollowed_series(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=("1",)) as api:
        other = api.env.new_series("Another")  # bound, but not followed
        assert other.work_id
        _follow(api)
        with api.env.inst.engine.begin() as connection:
            connection.execute(
                sa.update(acquisition.source_binding)
                .where(acquisition.source_binding.c.id == api.series.binding_id)
                .values(active=False, disabled_reason="manual")
            )
        rows = collect_follows(api.env.inst.engine)
        assert [(r.title, r.source) for r in rows] == [("Dr Stone", "")]  # no active binding


@pytest.mark.req("EF-INT-027")
def test_follows_export_empty_is_still_valid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with live(monkeypatch, settings, tmp_path, chapters=("1",)) as api:
        assert api.series.edition_id  # a series exists, but none is followed
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "follow", "export", "--format", "opml") == 0
        assert _opml_outlines(capsys.readouterr().out) == []
        assert run_cli(monkeypatch, settings, "follow", "export", "--format", "csv") == 0
        assert _csv_rows(capsys.readouterr().out) == [["title", "source", "url", "last_number"]]


@pytest.mark.req("EF-INT-027")
def test_follows_export_refuses_bad_invocations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    # no database yet: nothing is created, the command says what to do
    assert run_cli(monkeypatch, settings, "follow", "export") == 1
    assert "migrate" in capsys.readouterr().err
    assert not settings.database_path.exists()
    with live(monkeypatch, settings, tmp_path, chapters=()):
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "follow", "export", "someseries") == 2
        assert run_cli(monkeypatch, settings, "follow", "suwayomi:1") == 2  # a series is needed
        assert "série" in capsys.readouterr().err
        with pytest.raises(SystemExit):  # argparse refuses an unknown format
            run_cli(monkeypatch, settings, "follow", "export", "--format", "pdf")
    with pytest.raises(ValueError):
        export_follows(sa.create_engine("sqlite://"), "pdf")


@pytest.mark.req("EF-INT-027")
def test_follows_export_opml_escapes_xml() -> None:
    rows = [
        FollowRow(NASTY, 'a"b<c>&d', "https://example.org/?a=1&b=2", "1.5"),
        FollowRow("ctl\x00\x08\x1b\x0b", "s", "", ""),  # XML 1.0 cannot carry these at all
    ]
    text = render_opml(rows)
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
    first, second = _opml_outlines(text)  # still well formed
    assert first["text"] == NASTY
    assert first["source"] == 'a"b<c>&d'
    assert first["url"] == "https://example.org/?a=1&b=2"
    assert first["lastNumber"] == "1.5"
    assert second["text"] == "ctl"
    assert "<Co>" not in text  # raw markup from a title never reaches the document
    assert "&lt;Co&gt;" in text


@pytest.mark.req("EF-INT-027")
def test_follows_export_csv_escapes_commas_quotes_and_formulas() -> None:
    rows = [
        FollowRow('Stone, "Dr", vol 1\nsuite', "src,1", "https://example.org/?a=1,2", "3"),
        FollowRow('=HYPERLINK("http://x")', "+cmd", "-1", ""),
        FollowRow("@SUM(A1)", "\tTab", "", ""),
    ]
    text = render_csv(rows)
    parsed = _csv_rows(text)  # a strict reader gets the cells back
    assert parsed[1] == [
        'Stone, "Dr", vol 1\nsuite', "src,1", "https://example.org/?a=1,2", "3",
    ]  # fmt: skip
    assert '"Stone, ""Dr"", vol 1\nsuite"' in text  # quoted and doubled, by the book
    # a spreadsheet would run these as formulas: they stay text
    assert parsed[2] == ['\'=HYPERLINK("http://x")', "'+cmd", "'-1", ""]
    assert parsed[3] == ["'@SUM(A1)", "'\tTab", "", ""]
    assert render_csv([]) == "title,source,url,last_number\n"
    assert _opml_outlines(render_opml([])) == []


@pytest.mark.req("EF-INT-027")
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("not a url", ""),
        ("javascript:alert(1)", ""),
        ("file:///etc/passwd", ""),
        ("ftp://example.org/x", ""),
        ("https://example.org:99999/x", ""),  # invalid port
        ("https://u:p@example.org/x", "https://example.org/x"),
        ("https://example.org/x?token=a&page=2", "https://example.org/x?page=2"),
        (
            "https://example.org/x?X-Amz-Signature=a&Key-Pair-Id=b&id=1",
            "https://example.org/x?id=1",
        ),
        ("https://example.org/x?apikey=a&PASSWORD=b&auth=c&jwt=d", "https://example.org/x"),
        ("https://example.org/x#access_token=a", "https://example.org/x"),
        ("http://[::1]:8080/x", "http://[::1]:8080/x"),
        ("  https://example.org/a b ", "https://example.org/a b"),
    ],
)
def test_follows_export_public_url(raw: str | None, expected: str) -> None:
    assert public_url(raw) == expected
