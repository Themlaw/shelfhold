"""EF-INT-024: configuration without UI or SQL: `config get|set|apply`, `target add`, `source add`,
`shelf create` (L38). Network ports are recorders; the database and the vault are real."""

import os
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.root_bootstrap import RootPolicy
from shelfhold.export.domain.bindings import ShelfBelowEighteenError
from shelfhold.export.domain.reconcile import Thresholds, thresholds_from
from shelfhold.export.targets.kavita import load_admin_key
from shelfhold.headless import (
    AdminRiskNotAccepted,
    ConfigFileError,
    HeadlessAdmin,
    InvalidConfigValue,
    ReadOnlyConfigKey,
    SourceAuthModeUnsupported,
    UnknownConfigKey,
    parse_desired,
    wiring,
)
from shelfhold.headless.errors import DestinationNotWritable
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.plugin_host.adapters.meta.suwayomi.probe import HttpSuwayomiProbe
from tests.auth_env import run_cli
from tests.export_env import export_env
from tests.fakes.suwayomi.server import FakeSuwayomi, FakeSuwayomiFetcher
from tests.headless_env import (
    KAVITA_KEY,
    KAVITA_PASSWORD,
    KAVITA_URL,
    NOW,
    SUWAYOMI_PASSWORD,
    SUWAYOMI_URL,
    FakeKavitaExchange,
    FakeSuwayomiProbe,
    Headless,
    headless,
    write_secret,
)
from tests.integration.test_foreign_writer_reconcile import exported

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


@pytest.mark.req("EF-INT-024", "EF-SRC-031")
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


# -- L47: remediation of the audit of M1a -----------------------------------------------------


def _audit(settings: Settings, action: str) -> list[tuple[str | None, str | None]]:
    connection = sqlite3.connect(settings.database_path)
    try:
        return list(
            connection.execute("SELECT actor_id, target FROM audit WHERE action = ?", (action,))
        )
    finally:
        connection.close()


@pytest.mark.req("ENF-SEC-013")
def test_target_add_accept_admin_risk_writes_audit_row(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with headless(settings) as env:
        monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
        password = write_secret(tmp_path, "pw", KAVITA_PASSWORD + "\n")
        base = ["target", "add", "kavita", "--url", KAVITA_URL, "--user", "shelfhold",
                "--password-file", str(password)]  # fmt: skip
        assert run_cli(monkeypatch, settings, *base) != 0  # the warning stops the command
        assert _audit(settings, "target.admin_risk_accepted") == []
        assert run_cli(monkeypatch, settings, *base, "--accept-admin-risk") == 0
        [(actor, target)] = _audit(settings, "target.admin_risk_accepted")
        assert target is not None and target.startswith("kavita")
        assert f"uid={os.getuid()}" in target  # the host user who ran the offline command
        assert actor is None
        # the same target again exchanges nothing and accepts nothing new
        assert run_cli(monkeypatch, settings, *base, "--accept-admin-risk") == 0
        assert len(_audit(settings, "target.admin_risk_accepted")) == 1
        capsys.readouterr()
        assert KAVITA_PASSWORD not in env.dump()


def _library(tmp_path: Path) -> Path:
    library = tmp_path / "library"
    for name in ("manga", "books", "comics"):
        (library / name).mkdir(parents=True)
    return library


@pytest.mark.req("EF-EXP-003")
def test_target_add_without_path_map_uses_default_rw_roots(
    settings: Settings, tmp_path: Path
) -> None:
    library = _library(tmp_path)
    roots = RootPolicy(library, settings.data_dir, settings.store_dir)
    with headless(settings, roots=roots) as env:
        env.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="shelfhold", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
        )  # fmt: skip
        assert env.admin.get("path_map.kavita").value == [
            {"local": str(library / name), "remote": f"/{name}", "mode": "rw"}
            for name in ("manga", "books", "comics")
        ]
        with env.engine.connect() as connection:
            registered = {
                row.path_entry: row.mode
                for row in connection.execute(sa.text("SELECT path_entry, mode FROM physical_root"))
            }
        for name in ("manga", "books", "comics"):
            assert registered[str(library / name)] == "rw"
            assert (library / name / ".shelfhold-mount-id").is_file()  # stamped by the real path


@pytest.mark.req("EF-UX-002")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes: 0500 refuses nothing")
def test_shelf_create_on_0500_folder_reports_cause_and_compose_block(
    settings: Settings, tmp_path: Path
) -> None:
    library = _library(tmp_path)
    roots = RootPolicy(library, settings.data_dir, settings.store_dir)
    boot_check(settings, announce=False)  # the first start creates the keyring, as in production
    with headless(settings, roots=roots, real_linker=True) as env:
        env.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="shelfhold", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
        )  # fmt: skip
        (library / "manga").chmod(0o500)  # a real folder Shelfhold can read but not write in
        try:
            with pytest.raises(DestinationNotWritable) as refused:
                env.admin.shelf_create(name="Adult", max_age="18+", target="kavita", subpath="K")
        finally:
            (library / "manga").chmod(0o700)
        assert refused.value.code == "target.permission_denied"
        message = str(refused.value)
        assert "EACCES" in message and str(library / "manga") in message  # the cause, the place
        assert "0500" in message and str(os.getuid()) in message  # owner, mode, who runs
        assert "setfacl" in message  # the host command
        assert f'user: "{os.getuid()}:{os.getgid()}"' in message  # the exact compose block
        assert "services:" in message and "0:0" not in message  # never root in the block
        assert env.count("shelf") == 0 and env.count("shelf_destination") == 0  # nothing left


@pytest.mark.req("EF-SRC-031")
def test_ui_login_mode_detected_and_refused_m1a(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    server = FakeSuwayomi(auth_mode="ui_login")
    probe = HttpSuwayomiProbe(lambda _url: FakeSuwayomiFetcher(server, SUWAYOMI_URL))  # type: ignore[arg-type,return-value]
    with headless(settings, suwayomi=probe) as env:
        with pytest.raises(SourceAuthModeUnsupported) as refused:
            env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=None)
        assert refused.value.code == "source.auth_mode_unsupported"
        assert "multi-comptes" in str(refused.value)  # the dedicated message, not a generic one
        assert "ui_login" in str(refused.value)
        assert env.count("source_config") == 0
        monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
        assert (
            run_cli(monkeypatch, settings, "source", "add", "suwayomi", "--url", SUWAYOMI_URL) == 1
        )
        err = capsys.readouterr().err
        assert "multi-comptes" in err and "source.auth_mode_unsupported" in err


@pytest.mark.req("EF-SRC-025")
def test_config_set_piloted_warns_inactive_until_m1b(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    with headless(settings) as env:
        monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
        assert (
            run_cli(monkeypatch, settings, "config", "set", "source.check_mode", "always_on") == 0
        )
        assert "M1b" not in capsys.readouterr().err  # the supported mode needs no warning
        assert run_cli(monkeypatch, settings, "config", "set", "source.check_mode", "piloted") == 0
        err = capsys.readouterr().err
        assert "inactif" in err and "M1b" in err  # accepted, and said not to act yet
        assert env.admin.get("source.check_mode").value == "piloted"  # still stored: M1b reads it
        assert run_cli(monkeypatch, settings, "config", "get", "source.check_mode") == 0
        assert "M1b" in capsys.readouterr().err  # the warning stays where the value is read


@pytest.mark.req("EF-EXP-029")
@pytest.mark.parametrize(
    ("settings_set", "blocked"),
    [
        ({}, None),  # 4 of 12 under the default floor of 10
        ({"export.missing_floor": "3"}, "missing_ratio"),  # 4 >= 3 and 33 % > 5 %
        ({"export.missing_floor": "3", "export.missing_ratio": "0.5"}, None),  # 33 % < 50 %
        (  # more than 3 files, whatever the share
            {
                "export.missing_floor": "2",
                "export.missing_ceiling": "3",
                "export.missing_ratio": "0.5",
            },
            "missing_ratio",
        ),
    ],
)
def test_missing_thresholds_read_from_app_setting(
    settings_set: dict[str, str], blocked: str | None
) -> None:
    with export_env() as env:
        reconcile, _series, files = exported(env, 12)
        admin = HeadlessAdmin(
            env.engine,
            SecretVault(Keyring.generate(NOW)),
            clock=env.clock,
            kavita=FakeKavitaExchange(),
            suwayomi=FakeSuwayomiProbe(),
        )
        for key, value in settings_set.items():
            admin.set(key, value)  # `config set`, then one real pass
        for path in files[:4]:
            path.unlink()
        assert reconcile.reconcile().blocked == blocked


@pytest.mark.req("EF-EXP-029")
def test_missing_thresholds_are_validated() -> None:
    with export_env() as env:
        admin = HeadlessAdmin(
            env.engine,
            SecretVault(Keyring.generate(NOW)),
            clock=env.clock,
            kavita=FakeKavitaExchange(),
            suwayomi=FakeSuwayomiProbe(),
        )
        assert admin.get("export.missing_floor").value == 10  # the documented defaults
        assert admin.get("export.missing_ceiling").value == 50
        for key, bad in (("export.missing_floor", "0"), ("export.missing_ratio", "1.5"),
                         ("export.missing_ceiling", "many")):  # fmt: skip
            with pytest.raises(InvalidConfigValue):
                admin.set(key, bad)


@pytest.mark.req("EF-EXP-029")
def test_missing_thresholds_cannot_switch_the_guard_off() -> None:
    with export_env() as env:
        reconcile, _series, files = exported(env, 12)
        admin = HeadlessAdmin(
            env.engine,
            SecretVault(Keyring.generate(NOW)),
            clock=env.clock,
            kavita=FakeKavitaExchange(),
            suwayomi=FakeSuwayomiProbe(),
        )
        for key, bad in (
            ("export.missing_ratio", "1"),  # every file would have to be gone
            ("export.missing_ratio", "0.51"),
            ("export.missing_ceiling", "1001"),
            ("export.missing_floor", "999999999"),
            ("export.missing_floor", "51"),  # above the ceiling of 50
        ):
            with pytest.raises(InvalidConfigValue):
                admin.set(key, bad)
        admin.set("export.missing_ratio", "0.5")  # the widest share allowed still guards
        admin.set("export.missing_floor", "1")
        for path in files:
            path.unlink()  # an unplugged disk: all 12 gone
        assert reconcile.reconcile().blocked == "missing_ratio"
        with pytest.raises(InvalidConfigValue):
            admin.set("export.missing_ceiling", "0")


@pytest.mark.req("EF-EXP-029")
def test_thresholds_from_a_hand_edited_row_fall_back_to_the_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="WARNING", log_format="json")
    default = Thresholds()
    for edited in (
        {"export.missing_ratio": "1"},
        {"export.missing_floor": "999999999", "export.missing_ceiling": "999999999"},
        {"export.missing_floor": "60"},
    ):
        assert thresholds_from(edited) == default
    assert thresholds_from({"export.missing_floor": "5"}) == Thresholds(floor=5)
    assert capsys.readouterr().out.count("reconcile.thresholds_ignored") == 3


@pytest.mark.req("EF-EXP-003")
def test_default_path_map_never_turns_a_read_only_declaration_writable(
    settings: Settings, tmp_path: Path
) -> None:
    library = _library(tmp_path)
    roots = RootPolicy(library, settings.data_dir, settings.store_dir)
    manga = str(library / "manga")
    with headless(settings, roots=roots) as env:
        env.admin.target_add_kavita(
            name="b", url="http://b.internal:5000", username="u", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
        )  # fmt: skip
        with pytest.raises(InvalidConfigValue) as refused:  # already a stamped, writable root
            env.admin.target_add_kavita(
                name="a", url="http://a.internal:5000", username="u", password=KAVITA_PASSWORD,
                accept_admin_risk=True,
                path_map=[{"local": manga, "remote": "/ro", "mode": "adopt_ro"}],
            )  # fmt: skip
        assert "already a writable root" in str(refused.value)
        assert env.count("export_target") == 1
        with env.engine.connect() as connection:
            modes = {
                r[0]: r[1]
                for r in connection.execute(sa.text("SELECT path_entry, mode FROM physical_root"))
            }
        assert modes[manga] == "rw"


@pytest.mark.req("EF-EXP-003")
def test_default_path_map_skips_folders_another_target_names(
    settings: Settings, tmp_path: Path
) -> None:
    library = _library(tmp_path)
    roots = RootPolicy(library, settings.data_dir, settings.store_dir)
    manga = library / "manga"
    with headless(settings, roots=roots) as env:
        env.admin.target_add_kavita(
            name="a", url="http://a.internal:5000", username="u", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
            path_map=[{"local": str(manga), "remote": "/ro", "mode": "adopt_ro"}],
        )  # fmt: skip
        env.admin.target_add_kavita(
            name="b", url="http://b.internal:5000", username="u", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
        )  # fmt: skip
        assert [e["local"] for e in env.admin.get("path_map.b").value] == [  # type: ignore[union-attr]
            str(library / "books"),
            str(library / "comics"),
        ]
        assert not (manga / ".shelfhold-mount-id").exists()  # no sentinel in a read-only mount
        with env.engine.connect() as connection:
            modes = {
                r[0]: r[1]
                for r in connection.execute(sa.text("SELECT path_entry, mode FROM physical_root"))
            }
        assert modes[str(manga)] == "adopt_ro"


@pytest.mark.req("EF-EXP-003")
def test_an_empty_path_map_is_kept_empty_not_defaulted(settings: Settings, tmp_path: Path) -> None:
    roots = RootPolicy(_library(tmp_path), settings.data_dir, settings.store_dir)
    with headless(settings, roots=roots) as env:
        env.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="u", password=KAVITA_PASSWORD,
            accept_admin_risk=True, path_map=[],
        )  # fmt: skip
        assert env.admin.get("path_map.kavita").value == []
