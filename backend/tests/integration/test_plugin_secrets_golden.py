"""M2 L07 golden (EF-SRC-013, EF-SRC-035, ENF-SEC-005): a source secret or cookie, once loaded,
is looked for everywhere it could leak, in every form, and found nowhere.

The value is registered by `register_secret_value` the moment it is opened. Then: the log lines
that quote it, the L30 diagnostic bundle made from those lines and the database, the L06 HTTP
cache, a remote reference, an HTTP error and the database dump.
"""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
import sqlalchemy as sa
import structlog

from shelfhold.config import Settings
from shelfhold.headless.actor import Actor
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher.client import _carries_credential
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition
from shelfhold.infrastructure.secret_text import secret_forms
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.infrastructure.write_guard import SecretInWriteError
from shelfhold.interfaces.http_api.problems import ProblemError, problem_body
from shelfhold.plugin_host.settings import SourceSettings, parse_settings_schema
from tests.auth_env import db_dump, run_cli
from tests.diagnostic_env import Traps, leaks
from tests.redaction_support import forget_secret_values

pytestmark = pytest.mark.req("EF-SRC-013", "EF-SRC-035")

KEY = "acme"
SCHEMA = {"type": "object", "properties": {"api_key": {"type": "string", "writeOnly": True}}}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    forget_secret_values()
    yield
    forget_secret_values()
    structlog.reset_defaults()


def _configured(settings: Settings, value: str, cookie: str) -> SourceSettings:
    migrate(settings)
    vault = SecretVault(boot_check(settings, announce=False))
    engine = create_db_engine(settings.database_path)
    store = SourceSettings(engine, vault)
    schema = parse_settings_schema(SCHEMA)
    store.set_secret(KEY, schema, "api_key", value, actor=Actor("cli:uid1000"))
    store.set_cookies(KEY, {"sid": cookie}, actor=Actor("cli:uid1000"))
    return store


def test_a_loaded_source_secret_is_found_nowhere(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    value, cookie = secrets.token_hex(16), secrets.token_hex(16)
    store = _configured(settings, value, cookie)
    forget_secret_values()  # the server process: nothing known before it loads the source
    configure_logging(level="DEBUG", log_format="json")
    capsys.readouterr()
    loaded = store.load(KEY)
    header = f"sid={loaded.cookies['sid']}"
    traps = Traps(values={"api_key": value, "cookie": cookie, "cookie_header": header})

    # logs: the value, its URL forms and the cookie header, in a field, an extra and an error
    log = structlog.get_logger("shelfhold.test")
    log.warning("source.call", url=f"https://acme.example/a?k={value}", cookie=header)
    log.info("source.detail", note=f"Authorization: Bearer {value}", other={"x": value})
    try:
        raise RuntimeError(f"upstream said {value}")
    except RuntimeError:
        log.exception("source.failed")
    logged = capsys.readouterr()
    lines = logged.out + logged.err
    assert "source.call" in lines and "source.failed" in lines
    for category, needle in traps:
        assert not [f for f in secret_forms(needle) if f in lines], category

    # the L06 cache never files an answer whose URL carries it
    assert _carries_credential(httpx2.URL(f"https://acme.example/feed?q={value}"))
    assert _carries_credential(httpx2.URL(f"https://acme.example/{value}/feed"))

    # a remote reference that holds it is refused by the database itself (L04)
    with store.engine.begin() as connection, pytest.raises(SecretInWriteError):
        connection.execute(
            sa.insert(acquisition.source_item).values(
                id=new_ulid(), plugin_id=KEY, source_key=f"{KEY}:1",
                remote_id=f"/series/{value}",
            )
        )  # fmt: skip

    # an HTTP error that quotes it says nothing of it
    problem = ProblemError(
        "source.error", detail=f"upstream refused {value}", extensions={"url": f"/a?k={value}"}
    )
    rendered = json.dumps(
        problem_body(problem.spec, detail=problem.detail, extensions=problem.extensions)
    )
    assert "upstream refused" in rendered
    assert not [f for f in secret_forms(value) if f in rendered]

    # the database dump, then the L30 bundle made from it and from the log lines above
    dump = db_dump(settings)
    for category, needle in traps:
        assert not [f for f in secret_forms(needle) if f in dump], category
    log_path = tmp_path / "server.log"
    log_path.write_text(lines, encoding="utf-8")
    store.engine.dispose()
    out = tmp_path / "out" / "bundle.zip"
    out.parent.mkdir()
    forget_secret_values()  # the bundle runs in its own process: it must not rely on the registry
    code = run_cli(
        monkeypatch, settings, "diagnostic", "bundle", "--out", str(out),
        "--log-file", str(log_path),
    )  # fmt: skip
    assert code == 0
    assert leaks(out.read_bytes(), traps) == []
