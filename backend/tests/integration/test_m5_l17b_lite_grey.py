"""M5 L17b: the covers of `/lite` are greyscale (EF-BIB-020), and nothing decides by User-Agent
(EF-LEC-010, ENF-COMP-002).

A grey cover is a variant of the same thumbnail: same size, same hash, a name of its own on disk,
the same visibility check before anything is read or made. The attacks: a child asking for a
hidden cover (one answer with an absent one), a profile asking for another's, no cookie, the web
session or an API key of the same profile, and the colour thumbnail that must stay in colour.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

from shelfhold.config import Settings
from tests.integration.test_l48_opds import ABSENT_ULID, FakeBundles, World, make_world
from tests.integration.test_m5_l17a_lite import browser, signed_in
from tests.thumbnail_env import thumbnail_files

pytestmark = pytest.mark.req("EF-BIB-020", "EF-LEC-010", "ENF-COMP-002")

ROOT = Path(__file__).parents[3]
ABSENT_DIGEST = "ab" * 32
KOBO = "Mozilla/5.0 (Linux; U; Android 2.0; en-us;) AppleWebKit/538.1 (KHTML, like Gecko) Kobo"
KINDLE = "Mozilla/5.0 (X11; U; Linux armv7l like Android; en-us) AppleWebKit/531.2+ Kindle/3.0"
CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings, FakeBundles()) as made:
        yield made


def pixels(data: bytes) -> list[tuple[int, int, int]]:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    return [image.getpixel((x, y)) for x in (0, image.width // 2, image.width - 1)  # type: ignore[misc]
            for y in (0, image.height // 2, image.height - 1)]  # fmt: skip


def is_grey(data: bytes) -> bool:
    return all(max(p) - min(p) <= 2 for p in pixels(data))


def colour(world: World) -> bytes:
    """The same thumbnail by the web route, with the web session of the same profile."""
    got = world.kid_session.get(f"/api/v1/thumbnails/{world.covers['ok']}/160")
    assert got.status_code == 200
    return got.content


def test_lite_greyscale_thumbnails(world: World) -> None:
    """The cover of `/lite` is grey where the same thumbnail of the SPA is in colour."""
    lite = signed_in(world, world.mum, "10.0.2.1")
    for size in (160, 320, 640):
        got = lite.get(f"/lite/covers/{world.covers['ok']}/{size}")
        assert got.status_code == 200 and got.headers["content-type"] == "image/webp"
        assert is_grey(got.content), size
    assert not is_grey(colour(world))


def test_lite_grey_thumbnail_is_kept_apart_from_the_colour_one(world: World) -> None:
    lite = signed_in(world, world.mum, "10.0.2.2")
    first = lite.get(f"/lite/covers/{world.covers['ok']}/160")
    again = lite.get(f"/lite/covers/{world.covers['ok']}/160")
    assert first.content == again.content and first.headers["cache-control"] == "no-store"
    names = [p.name for p in thumbnail_files(world.env.settings.data_dir)]
    assert any("grey" in n for n in names), names
    assert not is_grey(  # the colour file, made after the grey one, is still colour
        colour(world)
    )


def test_lite_grey_thumbnail_visibility_child_idor_absent(world: World) -> None:
    kid = signed_in(world, world.kid, "10.0.2.3")
    absent = kid.get(f"/lite/covers/{ABSENT_DIGEST}/160")
    assert absent.status_code == 404
    for key in ("above", "mixed_new"):
        hidden = kid.get(f"/lite/covers/{world.covers[key]}/160")
        assert (hidden.status_code, hidden.text) == (absent.status_code, absent.text), key
        assert "etag" not in hidden.headers
    before = len(thumbnail_files(world.env.settings.data_dir))
    kid.get(f"/lite/covers/{world.covers['above']}/160")
    assert len(thumbnail_files(world.env.settings.data_dir)) == before  # nothing made for it
    # the adult sees it, grey, and the child cannot reuse its tag
    mum = signed_in(world, world.mum, "10.0.2.4")
    seen = mum.get(f"/lite/covers/{world.covers['above']}/160")
    assert seen.status_code == 200 and is_grey(seen.content)
    tag = seen.headers["etag"]
    replay = kid.get(f"/lite/covers/{world.covers['above']}/160", headers={"If-None-Match": tag})
    assert replay.status_code == 404


def test_lite_grey_thumbnail_needs_the_lite_cookie(world: World) -> None:
    path = f"/lite/covers/{world.covers['ok']}/160"
    anonymous = browser(world, "10.0.2.5").get(path, follow_redirects=False)
    assert anonymous.status_code == 303 and "sign-in" in anonymous.headers["location"]
    session = world.kid_session.get(path, follow_redirects=False)
    assert session.status_code == 303
    keyed = browser(world, "10.0.2.6").get(
        path, headers={"Authorization": world.kid.headers["Authorization"]}, follow_redirects=False
    )
    assert keyed.status_code in (303, 403)
    assert ABSENT_ULID not in anonymous.text


def test_lite_pages_point_at_the_lite_cover_route(world: World) -> None:
    lite = signed_in(world, world.mum, "10.0.2.7")
    images = re.findall(r'<img[^>]+src="([^"]+)"', lite.get("/lite/works").text)
    assert images and all(i.startswith("/lite/covers/") for i in images)


# -- EF-LEC-010: no User-Agent ------------------------------------------------------------------


def test_no_user_agent_sniffing(world: World) -> None:
    """What `/lite` and the SPA serve is the same for a Kobo, a Kindle and a desktop browser."""
    lite = signed_in(world, world.mum, "10.0.2.8")
    for path in ("/lite/works", "/lite/series", f"/lite/covers/{world.covers['ok']}/160"):
        answers = {
            ua: lite.get(path, headers={"User-Agent": ua}) for ua in (KOBO, KINDLE, CHROME)
        }
        assert len({(r.status_code, r.content) for r in answers.values()}) == 1, path
    spa = {
        ua: browser(world, "10.0.2.9").get("/", headers={"User-Agent": ua}, follow_redirects=False)
        for ua in (KOBO, KINDLE, CHROME)
    }
    assert len({(r.status_code, r.content, r.headers.get("location")) for r in spa.values()}) == 1


def test_no_user_agent_sniffing_in_the_serving_code() -> None:
    """Nothing that serves the SPA or `/lite`, and nothing of the SPA itself, reads the UA."""
    sniff = re.compile(r"user[-_ ]?agent|navigator\.(platform|vendor|appVersion)", re.IGNORECASE)
    scanned = [
        *(
            p for p in (ROOT / "backend/src/shelfhold/interfaces/http_api/lite").rglob("*")
            if p.suffix in {".py", ".html", ".css", ".txt"}
        ),
        ROOT / "backend/src/shelfhold/interfaces/http_api/spa.py",
        ROOT / "backend/src/shelfhold/interfaces/http_api/thumbnails.py",
        ROOT / "frontend/src/app.html",
    ]
    for folder in ("routes", "lib", "reader"):
        scanned += [
            p for p in (ROOT / "frontend/src" / folder).rglob("*")
            if p.suffix in {".ts", ".svelte", ".js", ".css"} and ".test." not in p.name
        ]  # fmt: skip
    hits = [str(p) for p in scanned if p.is_file() and sniff.search(p.read_text("utf-8"))]
    assert hits == []
