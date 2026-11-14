"""M3 L48, contradicteur: red PoCs, see `docs/decisions/M3.md` (« L48 : contradicteur »).

The end of the card says « le QR ne contient aucun jeton ni clé ». `device_entry_points` strips
the userinfo, the query and the fragment, but keeps the path whole: Kavita's own OPDS and kosync
addresses carry the reader's key in the path (`/api/opds/<apiKey>`, `/api/koreader/<apiKey>`), and
a `;param` stays in the path for `urlsplit`."""

from pathlib import Path

import pytest

from shelfhold.export.domain.kavita_instance import device_entry_points

pytestmark = pytest.mark.req("EF-UX-004")

KEY = "synthetic" + "-0" * 12  # built, not a literal: no secret-looking value in the tree
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "address",
    [
        f"https://kavita.example/api/opds/{KEY}",  # the OPDS address Kavita shows, pasted as is
        f"https://kavita.example/api/koreader/{KEY}",  # the kosync address Kavita shows
        f"https://kavita.example/kavita;apiKey={KEY}",  # a path parameter, kept by urlsplit
    ],
)
def test_the_qr_never_carries_a_key_held_in_the_path(address: str) -> None:
    found = device_entry_points(address)
    assert found is None or not any(KEY in value for value in found.values()), found


def test_an_address_with_a_port_out_of_range_is_refused_not_raised() -> None:
    # « Anything that is not a web address gives `None` »: `parts.port` raises ValueError, and one
    # such row would turn the whole « Connecter un appareil » screen into a 500.
    assert device_entry_points("https://kavita.example:99999") is None


@pytest.mark.parametrize("href", ["/corbeille", "/appareils", "/a-classer"])
def test_the_l48_screens_are_reachable_from_the_admin_settings(href: str) -> None:
    # Réglages links /sante, /sources, /activite/file for the admin; the three L48 screens are
    # linked from nowhere: an admin can only reach them by typing the address.
    pages = [
        p
        for p in (ROOT / "frontend" / "src").rglob("*.svelte")
        if not str(p.relative_to(ROOT)).startswith(f"frontend/src/routes{href}/")
    ]
    assert any(f'href="{href}"' in p.read_text(encoding="utf-8") for p in pages), href
