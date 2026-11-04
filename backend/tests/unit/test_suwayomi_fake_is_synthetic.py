"""ENF-LEG-013: the simulated Suwayomi and its states hold nothing from a third party."""

import json
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[1]
FILES = [
    *(TESTS / "fakes" / "suwayomi").glob("*.py"),
    *(TESTS / "fixtures" / "suwayomi_states").glob("*.json"),
]
HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
SYNTHETIC_HOSTS = re.compile(r"(\.lan|\.invalid|\.example|^example\.(com|org|net)|^localhost)$")


@pytest.mark.req("ENF-LEG-013")
def test_fake_suwayomi_is_synthetic() -> None:
    assert FILES
    for path in FILES:
        for host in HOST.findall(path.read_text("utf-8")):
            assert SYNTHETIC_HOSTS.search(host), f"{path.name}: non-synthetic host {host}"
    state = json.loads((TESTS / "fixtures" / "suwayomi_states" / "library.json").read_text("utf-8"))
    for source in state["sources"]:
        assert source["displayName"].startswith("Synthetic ")
    for manga in state["mangas"]:
        assert manga["title"].startswith("Series ")
        assert manga["url"].startswith("/series-")
        for chapter in manga["chapters"]:
            assert chapter["cbz"].startswith("comic_a_")  # the repository's own synthetic CBZ
            assert (TESTS / "fixtures" / "synthetic" / chapter["cbz"]).is_file()
