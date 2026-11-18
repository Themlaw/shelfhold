"""M5 L13 (EF-LEC-006): the settings of the reader — priority per type, the portée asked.

`reader_settings` holds the choices of one profile. The priority is **per type** (EF-LEC-006): a
direction pinned to a series beats the profile's own mode (« série > mode »), a comfort setting of
one device beats the default (« appareil > défaut »), and the portée — « pour cet appareil » ou
« partout » — is part of the write. The settings of a profile stay in that profile (a child's
too), and no setting widens what parental control hides: the doors that serve bytes keep checking
visibility, whatever a row says.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.reading.application.settings import (
    DeviceScopeError,
    InvalidSettingError,
    ReaderSettings,
)
from tests.auth_env import CSRF_HEADER
from tests.integration.test_l05a_content_route import World, _shape, _world
from tests.integration.test_m5_l06_publication import (
    ABSENT,
    ServerCutArchives,
    book,
    pages_of,
    publication,
    unit_of,
)
from tests.reading_env import reading_env

DEVICE = "web-lecture"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> World:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


# -- the priority is per type (service, over the migrated database) ----------------------------


@pytest.mark.req("EF-LEC-006")
def test_settings_priority_per_type(tmp_path: Any) -> None:
    """A direction pinned to a series beats the profile's mode; a comfort setting of one device
    beats the default. The series without its own row reads the mode, the device without its own
    row reads the default."""
    with reading_env(tmp_path) as env:
        reader = env.user("lectrice")
        service = ReaderSettings(env.engine)
        series, other = env.seed.works["ok"], env.seed.works["x"]
        service.set(user_id=reader, kind="direction", key="default", value="ltr",
                    scope="everywhere")
        service.set(user_id=reader, kind="direction", key=series, value="rtl", scope="everywhere")
        service.set(user_id=reader, kind="comfort", key="fit", value="contain", scope="everywhere")
        service.set(user_id=reader, kind="comfort", key="fit", value="width",
                    scope="device", device_id=DEVICE)
        # the direction: the series wins over the mode, the other series reads the mode
        assert service.resolve(user_id=reader, series_id=series).direction == "rtl"
        assert service.resolve(user_id=reader, series_id=other).direction == "ltr"
        assert service.resolve(user_id=reader).direction == "ltr"
        # the comfort: the device wins over the default, another device reads the default
        assert service.resolve(user_id=reader, device_id=DEVICE).comfort == {"fit": "width"}
        assert service.resolve(user_id=reader, device_id="web-autre").comfort == {"fit": "contain"}
        assert service.resolve(user_id=reader).comfort == {"fit": "contain"}
        # nothing chosen: the publication's own direction wins, no comfort is forced
        fresh = env.user("nouvelle")
        assert service.resolve(user_id=fresh, series_id=series).direction is None
        assert service.resolve(user_id=fresh, device_id=DEVICE).comfort == {}


@pytest.mark.req("EF-LEC-006")
def test_the_portee_is_part_of_the_write(tmp_path: Any) -> None:
    """« pour cet appareil » names the device, « partout » names none, and writing the same place
    again replaces the value instead of piling rows up."""
    with reading_env(tmp_path) as env:
        reader = env.user("lectrice")
        service = ReaderSettings(env.engine)
        with pytest.raises(DeviceScopeError):
            service.set(user_id=reader, kind="comfort", key="fit", value="width", scope="device")
        with pytest.raises(DeviceScopeError):
            service.set(user_id=reader, kind="comfort", key="fit", value="width",
                        scope="everywhere", device_id=DEVICE)
        service.set(user_id=reader, kind="comfort", key="fit", value="width",
                    scope="device", device_id=DEVICE)
        service.set(user_id=reader, kind="comfort", key="fit", value="contain",
                    scope="device", device_id=DEVICE)
        assert service.resolve(user_id=reader, device_id=DEVICE).comfort == {"fit": "contain"}
        with env.engine.connect() as connection:
            rows = int(
                connection.execute(
                    sa.text("SELECT count(*) FROM reader_settings WHERE user_id = :u"),
                    {"u": reader},
                ).scalar_one()
            )
        assert rows == 1  # one place, one value: the write replaced it


@pytest.mark.req("EF-LEC-006")
def test_only_known_choices_are_taken(tmp_path: Any) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("lectrice")
        service = ReaderSettings(env.engine)
        for kind, key, value in (
            ("theme", "default", "dark"),  # no such type of setting
            ("direction", "default", "upwards"),  # no such direction
            ("direction", "not-a-series", "rtl"),  # a series is named by its identifier
            ("comfort", "margin", "wide"),  # no such comfort setting
            ("comfort", "fit", "sideways"),  # no such value for it
        ):
            with pytest.raises(InvalidSettingError):
                service.set(user_id=reader, kind=kind, key=key, value=value, scope="everywhere")


@pytest.mark.req("EF-LEC-006", "EF-USR-006")
def test_settings_stay_in_their_profile(tmp_path: Any) -> None:
    """One profile never reads another's choices, and a child's stay in hers."""
    with reading_env(tmp_path) as env:
        adult, child = env.user("adulte"), env.user("enfant", role="child")
        service = ReaderSettings(env.engine)
        series = env.seed.works["ok"]
        service.set(user_id=adult, kind="direction", key=series, value="rtl", scope="everywhere")
        service.set(user_id=child, kind="direction", key=series, value="ltr", scope="everywhere")
        service.set(user_id=child, kind="comfort", key="fit", value="width",
                    scope="device", device_id=DEVICE)
        assert service.resolve(user_id=adult, series_id=series).direction == "rtl"
        assert service.resolve(user_id=child, series_id=series).direction == "ltr"
        assert service.resolve(user_id=child, device_id=DEVICE).comfort == {"fit": "width"}
        assert service.resolve(user_id=adult, device_id=DEVICE).comfort == {}


# -- the routes ---------------------------------------------------------------------------------


def settings_of(client: Any, **params: Any) -> Any:
    return client.get("/api/reader/settings", params=params)


def put_setting(world: World, client: Any, body: dict[str, Any]) -> Any:
    return client.put(
        "/api/reader/settings", json=body, headers={CSRF_HEADER: world.env.csrf(client)}
    )


@pytest.mark.req("EF-LEC-006")
def test_the_manifest_names_the_series_the_settings_anchor(world: World) -> None:
    content = book(world, pages_of(30))
    manifest = publication(world.admin, "content", content).json()
    assert manifest["series_id"] == world.seed.works["ok"]


@pytest.mark.req("EF-LEC-006")
def test_settings_routes_answer_the_profile_that_asks(world: World) -> None:
    """`GET` resolves the effective settings, `PUT` writes one choice with its portée, and the
    answer says what is effective now. A session carries both; a request without one is refused."""
    series = world.seed.works["ok"]
    anonymous = world.env.other_client("10.0.0.9")  # no session of anyone
    assert settings_of(anonymous).status_code == 401  # not signed in
    assert settings_of(world.admin, series_id=series).json() == {
        "direction": None,
        "comfort": {},
    }
    got = put_setting(
        world,
        world.admin,
        {"kind": "direction", "key": series, "value": "rtl", "scope": "everywhere"},
    )
    assert got.status_code == 200
    assert got.json()["direction"] == "rtl"
    got = put_setting(
        world,
        world.admin,
        {"kind": "comfort", "key": "fit", "value": "width", "scope": "device",
         "device_id": DEVICE},
    )
    assert got.status_code == 200
    assert settings_of(world.admin, series_id=series, device_id=DEVICE).json() == {
        "direction": "rtl",
        "comfort": {"fit": "width"},
    }
    # the same profile on another device reads the default, and another profile reads nothing
    assert settings_of(world.admin, series_id=series, device_id="web-autre").json() == {
        "direction": "rtl",
        "comfort": {},
    }
    assert settings_of(world.mum, series_id=series, device_id=DEVICE).json() == {
        "direction": None,
        "comfort": {},
    }


@pytest.mark.req("EF-LEC-006")
def test_a_write_without_the_csrf_token_is_refused(world: World) -> None:
    got = world.admin.put(
        "/api/reader/settings",
        json={"kind": "comfort", "key": "fit", "value": "width", "scope": "everywhere"},
    )
    assert got.status_code == 403


@pytest.mark.req("EF-LEC-006")
def test_unknown_choices_are_a_typed_refusal(world: World) -> None:
    for body in (
        {"kind": "theme", "key": "default", "value": "dark", "scope": "everywhere"},
        {"kind": "direction", "key": "default", "value": "upwards", "scope": "everywhere"},
        {"kind": "comfort", "key": "fit", "value": "width", "scope": "device"},
    ):
        got = put_setting(world, world.admin, body)
        assert got.status_code == 422, body
        assert got.json()["code"] == "validation_failed"


@pytest.mark.req("EF-LEC-006", "EF-USR-006")
def test_no_setting_widens_what_parental_control_hides(world: World) -> None:
    """A child may keep her choices where she may read; a row anchored to a Work above her
    ceiling changes nothing: the doors that serve bytes keep answering her like a thing that does
    not exist."""
    hidden = book(world, pages_of(30), edition="twelve")
    unit = unit_of(world, hidden)
    absent = publication(world.kid, "content", ABSENT)
    got = put_setting(
        world,
        world.kid,
        {"kind": "direction", "key": world.seed.works["twelve"], "value": "rtl",
         "scope": "everywhere"},
    )
    assert got.status_code == 200  # her own choice, in her own profile
    assert _shape(publication(world.kid, "content", hidden)) == _shape(absent)
    assert _shape(publication(world.kid, "unit", unit)) == _shape(absent)
    assert "/content/" not in publication(world.kid, "content", hidden).text
