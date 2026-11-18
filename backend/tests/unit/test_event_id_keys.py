"""J-FLUX-2: `publish` refuses a `*_id` key that `event_refs` would not read as a content, unless it
names something that is not content (a closed list): a future event that names a Work under
another key would otherwise never be filtered for a child."""

import pytest

from shelfhold.events.bus import EventPayloadError, check_payload
from shelfhold.events.id_keys import CONTENT_ID_SUFFIXES, NON_CONTENT_ID_KEYS
from shelfhold.infrastructure.ids import new_ulid

ULID = new_ulid()


@pytest.mark.req("EF-USR-006")
@pytest.mark.parametrize("key", ["chapter_id", "volume_id", "title_id", "book_id"])
def test_an_id_key_event_refs_cannot_read_is_refused(key: str) -> None:
    with pytest.raises(EventPayloadError, match="closed list"):
        check_payload({key: ULID})


@pytest.mark.req("EF-USR-006")
@pytest.mark.parametrize("key", ["work_id", "from_work_id", "edition_id", "unit_id", "series_id"])
def test_a_content_key_event_refs_reads_passes(key: str) -> None:
    check_payload({key: ULID})


@pytest.mark.req("EF-USR-006")
def test_the_non_content_keys_never_end_like_a_content_key() -> None:
    assert not [k for k in NON_CONTENT_ID_KEYS if k.endswith(CONTENT_ID_SUFFIXES)]
