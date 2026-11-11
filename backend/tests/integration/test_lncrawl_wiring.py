"""M2 L25: the composition builds the lncrawl link from the settings of the source (L07) and the
follow job drives it; an instance that is not configured, or whose address is refused, is never
asked anything; the credential is injected by the host and never leaves the sealed settings."""

from typing import ClassVar

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import acquisition
from shelfhold.plugin_host.adapters.meta.lncrawl import LncrawlService
from shelfhold.plugin_host.settings import SourceSettings, parse_settings_schema
from shelfhold.runtime_jobs import follow_handler
from tests.fakes.lncrawl import API_KEY, BASE_URL
from tests.l41_env import context, instance

pytestmark = pytest.mark.req("EF-SRC-010", "EF-SRC-033")


class _Admin:
    @staticmethod
    def actor_id() -> str | None:
        return None

    @staticmethod
    def session_id() -> str | None:
        return None

    @staticmethod
    def auth_time() -> None:
        return None


class _Pools:
    slots: ClassVar[None] = None

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, kind: str, **_: object) -> None:
        self.submitted.append(kind)


def configure(inst, *, url: str = BASE_URL, key: str | None = API_KEY) -> None:  # type: ignore[no-untyped-def]
    schema = parse_settings_schema(LncrawlService("").manifest.settings_schema)
    store = SourceSettings(inst.engine, inst.comp.vault)
    store.set_values("lncrawl", schema, {"base_url": url}, actor=_Admin())  # type: ignore[arg-type]
    if key is not None:
        store.set_secret("lncrawl", schema, "api_key", key, actor=_Admin())  # type: ignore[arg-type]


def test_an_instance_not_configured_is_never_asked(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with instance(settings, tmp_path) as inst:
        assert inst.comp.lncrawl() is None
        follow_handler(inst.comp, _Pools())(context("follow.cycle"))  # type: ignore[arg-type]
        assert inst.lncrawl.requests == []


def test_the_follow_job_drives_the_configured_instance_with_the_sealed_key(
    settings: Settings, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    with instance(settings, tmp_path) as inst:
        inst.configure()
        configure(inst)
        pools = _Pools()
        follow_handler(inst.comp, pools)(context("follow.cycle"))  # type: ignore[arg-type]
        assert inst.lncrawl.requests, "the cycle really spoke to the instance"
        assert {r.headers.get("x-api-key") for r in inst.lncrawl.requests} == {API_KEY}
        assert inst.lncrawl.violations == []
        with inst.engine.connect() as connection:  # the key lives sealed, never in clear
            settings_row = connection.execute(
                sa.select(acquisition.source_config.c.settings).where(
                    acquisition.source_config.c.plugin_id == "lncrawl"
                )
            ).scalar_one()
        assert API_KEY not in str(settings_row) and "api_key" not in settings_row


@pytest.mark.parametrize(
    "url",
    ["http://other.invalid:8080", "file:///etc/passwd", "http://user:pw@lncrawl.lan:8080", "x"],
)
def test_an_address_the_rules_refuse_is_never_reached(
    settings: Settings, tmp_path, url: str
) -> None:  # type: ignore[no-untyped-def]
    with instance(settings, tmp_path) as inst:
        inst.configure()
        configure(inst, url=url)
        assert inst.comp.lncrawl() is None or inst.lncrawl.requests == []
        follow_handler(inst.comp, _Pools())(context("follow.cycle"))  # type: ignore[arg-type]
        assert inst.lncrawl.requests == []


def test_a_missing_key_is_refused_by_the_instance_and_leaks_nothing(
    settings: Settings, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    with instance(settings, tmp_path) as inst:
        inst.configure()
        configure(inst, key=None)
        follow_handler(inst.comp, _Pools())(context("follow.cycle"))  # type: ignore[arg-type]
        assert {r.headers.get("x-api-key") for r in inst.lncrawl.requests} <= {None}
