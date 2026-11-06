"""L55 (EF-BIB-051 (6)): what an event says about a refused archive is a closed list."""

import pytest

from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.events.bus import PAYLOAD_ENUMS
from shelfhold.infrastructure.content_trust import REASON_KINDS, UntrustedEntryError

pytestmark = pytest.mark.req("EF-BIB-051")


def test_the_event_enum_is_the_closed_list_of_reason_kinds() -> None:
    assert PAYLOAD_ENUMS["content.untrusted_entry"]["reason"] == REASON_KINDS


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("not_allowlisted", "not_allowlisted"),
        ("magic_mismatch", "magic_mismatch"),
        ("xml_entity_declared", "unsafe_xml"),
        ("encrypted_entry", "unsafe_archive"),
        ("anything the archive says", "unsafe_archive"),
    ],
)
def test_a_reason_is_reduced_to_a_member_of_the_closed_list(reason: str, kind: str) -> None:
    error = UntrustedEntryError(reason, "an entry name that must never reach an event")
    assert error.kind == kind and kind in REASON_KINDS


def test_a_refused_archive_is_neither_retried_nor_counted() -> None:
    error = UntrustedEntryError("not_allowlisted", "x.svg")
    assert disposition_of(classify(error)) is Disposition.REFUSE
