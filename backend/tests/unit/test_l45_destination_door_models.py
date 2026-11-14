"""The bodies of the destination doors never show a secret or a typed phrase (M3 L45, EF-EXP-031).

The password of the Kavita account travels in the body of the detachment: like every other body
that carries one, its model hides it from `repr` (a log line, a trace) and the phrase typed
names a folder, so it stays out as well.
"""

import pytest

from shelfhold.interfaces.http_api.destinations import DetachIn, RemovalIn

pytestmark = pytest.mark.req("EF-EXP-031")


def test_the_detach_body_hides_the_password_and_the_phrase_from_repr() -> None:
    body = DetachIn(typed="DÉTACHER Kavita", credential="synthetic-kavita-pw")
    assert "synthetic-kavita-pw" not in repr(body) and "Kavita" not in repr(body)


def test_the_removal_body_hides_the_phrase_from_repr() -> None:
    body = RemovalIn(mode="keep_files", typed="RETIRER manga/")
    assert "manga" not in repr(body)
