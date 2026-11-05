"""EF-INT-024: configuration without UI or SQL: `config get|set|apply`, `target add`, `source add`,
`shelf create` (L38). Network ports are recorders; the database and the vault are real."""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.domain.bindings import ShelfBelowEighteenError
from shelfhold.export.targets.kavita import load_admin_key
from shelfhold.headless import (
    AdminRiskNotAccepted,
    ConfigFileError,
    InvalidConfigValue,
    ReadOnlyConfigKey,
    SourceAuthModeUnsupported,
    UnknownConfigKey,
    parse_desired,
)
from shelfhold.infrastructure.logging import configure_logging
from tests.headless_env import (
    KAVITA_KEY,
    KAVITA_PASSWORD,
    KAVITA_URL,
    SUWAYOMI_PASSWORD,
    SUWAYOMI_URL,
    Headless,
    headless,
    write_secret,
)

pytestmark = pytest.mark.req("EF-INT-024")

FILE = """\
schema_version = "1.0"
public_url = "https://shelf.example.org"

[source]
check_mode = "always_on"

[[targets]]
kind = "kavita"
name = "kavita"
url = "{kavita}"
user = "shelfhold"
password_file = "{password}"
accept_admin_risk = true
path_map = [{{ local = "/library/manga", remote = "/manga" }}]

[[sources]]
kind = "suwayomi"
url = "{suwayomi}"
secret_file = "{secret}"

[[shelves]]
name = "Manga"
max_age = "18+"
target = "kavita"
subpath = "manga"
"""


def _declared(directory: Path, *, public_url: str = "https://shelf.example.org") -> Path:
    password = write_secret(directory, "kavita.pw", KAVITA_PASSWORD + "\n")
    secret = write_secret(directory, "suwayomi.secret", f"admin:{SUWAYOMI_PASSWORD}\n")
    text = FILE.format(
        kavita=KAVITA_URL, suwayomi=SUWAYOMI_URL, password=password, secret=secret
    ).replace("https://shelf.example.org", public_url)
    path = directory / "shelfhold.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _rows(env: Headless) -> dict[str, int]:
    return {t: env.count(t) for t in ("app_setting", "export_target", "shelf", "shelf_destination")}


def test_config_apply_declarative_idempotent_dry_run_diff(
    settings: Settings, tmp_path: Path
) -> None:
    file = _declared(tmp_path)
    with headless(settings) as env:
        before = _rows(env)
        desired = parse_desired(file.read_text("utf-8"), base_dir=tmp_path)

        # --dry-run: the differences, and not one write, one probe, one key exchange
        dry = env.admin.apply(desired, dry_run=True)
        assert not dry.is_noop
        assert {(c.kind, c.subject, c.action) for c in dry.changes} >= {
            ("config", "public_url", "create"),
            ("config", "source.check_mode", "create"),
            ("target", "kavita", "create"),
            ("source", "suwayomi", "create"),
            ("shelf", "Manga", "create"),
        }
        shown = dry.render()
        assert "public_url" in shown and "https://shelf.example.org" in shown
        assert KAVITA_PASSWORD not in shown and SUWAYOMI_PASSWORD not in shown
        assert _rows(env) == before and env.kavita.calls == [] and env.suwayomi.calls == []

        # apply: everything exists, the key was exchanged once
        done = env.admin.apply(desired)
        assert not done.is_noop
        assert len(env.kavita.calls) == 1 and len(env.suwayomi.calls) == 1
        assert env.count("export_target") == 1 and env.count("shelf") == 1
        assert env.count("shelf_destination") == 1
        with env.engine.connect() as connection:
            [target] = connection.execute(sa.text("SELECT id, path_map FROM export_target")).all()
            assert env.admin.get("public_url").value == "https://shelf.example.org"
            assert load_admin_key(env.vault, connection, target.id).reveal() == KAVITA_KEY
        after = _rows(env)

        # idempotent: the same file again changes nothing and exchanges nothing
        again = env.admin.apply(desired)
        assert again.is_noop and all(c.action == "unchanged" for c in again.changes)
        assert _rows(env) == after and len(env.kavita.calls) == 1

        # a one-line edit shows as exactly one difference, and only that one
        edited = parse_desired(
            _declared(tmp_path, public_url="https://other.example.org").read_text("utf-8"),
            base_dir=tmp_path,
        )
        plan = env.admin.apply(edited, dry_run=True)
        [change] = [c for c in plan.changes if c.action != "unchanged"]
        assert (change.kind, change.subject, change.action) == ("config", "public_url", "update")
        assert (change.before, change.after) == (
            "https://shelf.example.org",
            "https://other.example.org",
        )
        assert env.admin.get("public_url").value == "https://shelf.example.org"  # still not written


def test_target_add_kavita_requires_accept_admin_risk(settings: Settings) -> None:
    with headless(settings) as env:
        with pytest.raises(AdminRiskNotAccepted) as refused:
            env.admin.target_add_kavita(
                name="kavita", url=KAVITA_URL, username="shelfhold", password=KAVITA_PASSWORD,
                accept_admin_risk=False,
            )  # fmt: skip
        assert refused.value.code == "target.admin_risk_not_accepted"
        assert "administrator" in str(refused.value)  # the warning is the refusal
        assert env.kavita.calls == [] and env.count("export_target") == 0  # nothing happened

        added = env.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="shelfhold", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
        )  # fmt: skip
        [call] = env.kavita.calls
        assert call["key_name"] == "shelfhold" and 0 < int(call["expires_days"]) <= 365  # type: ignore[call-overload]
        with env.engine.connect() as connection:
            assert load_admin_key(env.vault, connection, added.id).reveal() == KAVITA_KEY
        for secret in (KAVITA_KEY, KAVITA_PASSWORD):  # sealed: the password is never stored
            assert secret not in env.dump()
        # the same target again is no second exchange
        assert env.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="shelfhold", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
        ).id == added.id  # fmt: skip
        assert len(env.kavita.calls) == 1


def test_config_get_set_list_validate_and_default(settings: Settings) -> None:
    with headless(settings) as env:
        assert env.admin.get("source.check_mode").value == "always_on"  # the documented default
        assert env.admin.get("source.check_mode").source == "default"
        env.admin.set("source.check_mode", "piloted")
        assert env.admin.get("source.check_mode").value == "piloted"
        assert env.admin.get("source.check_mode").source == "db"
        with pytest.raises(InvalidConfigValue):
            env.admin.set("source.check_mode", "sometimes")
        with pytest.raises(InvalidConfigValue):
            env.admin.set("public_url", "https://user:pw@shelf.example.org/?q=1")
        with pytest.raises(ReadOnlyConfigKey):  # ENF-OPS-014: the window is fixed in M1a
            env.admin.set("maintenance.window", "01:00-02:00")
        with pytest.raises(UnknownConfigKey):
            env.admin.get("no.such.key")
        names = {row.key for row in env.admin.list()}
        assert {"public_url", "source.check_mode", "maintenance.window", "notify.urls"} <= names


def test_config_path_map_and_notify_urls_are_settable(settings: Settings, tmp_path: Path) -> None:
    with headless(settings) as env:
        env.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="u", password="p" * 12,
            accept_admin_risk=True,
        )  # fmt: skip
        env.admin.set("path_map.kavita", '[{"local": "/library/a", "remote": "/a"}]')
        assert env.admin.get("path_map.kavita").value == [
            {"local": "/library/a", "remote": "/a", "mode": "rw"}
        ]
        with pytest.raises(InvalidConfigValue):
            env.admin.set("path_map.kavita", '[{"local": "relative", "remote": "/a"}]')
        # a notification URL carries a token: sealed, and only its name is ever shown
        env.admin.set("notify.urls", "ops=ntfy://ntfy.example.org/secret-topic-0038")
        shown = env.admin.get("notify.urls")
        assert shown.value == ["ops"]
        assert "secret-topic-0038" not in env.dump()
        with pytest.raises(InvalidConfigValue):
            env.admin.set("notify.urls", "bad=http://example.org/hook")


def test_source_add_suwayomi_reads_auth_mode_and_refuses_ui_login(settings: Settings) -> None:
    with headless(settings) as env:
        env.suwayomi.mode = "ui_login"
        with pytest.raises(SourceAuthModeUnsupported) as refused:
            env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=None)
        assert refused.value.code == "source.auth_mode_unsupported"
        assert env.count("source_config") == 0

        env.suwayomi.mode = "basic_auth"
        added = env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=f"admin:{SUWAYOMI_PASSWORD}")
        assert added.auth_mode == "basic_auth"
        assert env.suwayomi.calls[-1]["username"] == "admin"
        assert SUWAYOMI_PASSWORD not in env.dump()  # sealed in `source_config`
        with env.engine.connect() as connection:
            opened = env.vault.open_row(connection, "source_config", "suwayomi")
        assert SUWAYOMI_PASSWORD.encode() in opened
        with pytest.raises(InvalidConfigValue):
            env.admin.source_add_suwayomi(url="ftp://suwayomi", secret=None)


def test_shelf_create_adult_only_binds_to_the_target(settings: Settings) -> None:
    with headless(settings) as env:
        env.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="u", password="p" * 12,
            accept_admin_risk=True,
        )  # fmt: skip
        env.admin.set("path_map.kavita", '[{"local": "/library/manga", "remote": "/manga"}]')
        with pytest.raises(ShelfBelowEighteenError):
            env.admin.shelf_create(name="Kids", max_age="12", target="kavita", subpath="kids")
        assert env.count("shelf") == 0
        created = env.admin.shelf_create(name="Manga", max_age="X", target="kavita", subpath="m")
        assert created.shelf_id and created.destination_id
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT s.max_age_rating, d.root_subpath FROM shelf s "
                        "JOIN shelf_destination d ON d.shelf_id = s.id")
            ).one()  # fmt: skip
        assert tuple(row) == (99, "m")
        # idempotent by name
        assert env.admin.shelf_create(
            name="Manga", max_age="X", target="kavita", subpath="m"
        ).shelf_id == created.shelf_id  # fmt: skip
        assert env.count("shelf") == 1


def test_apply_file_errors_are_reported_without_secrets(settings: Settings, tmp_path: Path) -> None:
    with headless(settings) as env:
        with pytest.raises(ConfigFileError):
            parse_desired("this is = not [toml", base_dir=tmp_path)
        with pytest.raises(ConfigFileError):  # a newer major schema
            parse_desired('schema_version = "2.0"', base_dir=tmp_path)
        with pytest.raises(ConfigFileError):  # a key nobody knows is an error, not ignored
            parse_desired('unknown_key = "x"', base_dir=tmp_path)
        with pytest.raises(ConfigFileError):  # the secret is a file, never inline
            parse_desired(
                '[[targets]]\nkind = "kavita"\nname = "k"\nurl = "http://k"\nuser = "u"\n'
                'password = "inline"\naccept_admin_risk = true\n',
                base_dir=tmp_path,
            )
        risky = parse_desired(
            '[[targets]]\nkind = "kavita"\nname = "k"\nurl = "http://k:1"\nuser = "u"\n'
            f'password_file = "{write_secret(tmp_path, "pw", "pw-pw-pw-pw-pw")}"\n',
            base_dir=tmp_path,
        )
        with pytest.raises(AdminRiskNotAccepted):  # applying a file is no way around the flag
            env.admin.apply(risky)
        assert env.count("export_target") == 0


def test_secret_values_never_reach_the_logs(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="DEBUG", log_format="json")
    with headless(settings) as env:
        env.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="u", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
        )  # fmt: skip
        env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=f"a:{SUWAYOMI_PASSWORD}")
    text = capsys.readouterr().out
    assert "headless.target_added" in text  # something was logged
    for secret in (KAVITA_PASSWORD, KAVITA_KEY, SUWAYOMI_PASSWORD):
        assert secret not in text
