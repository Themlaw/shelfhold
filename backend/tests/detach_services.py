"""The wiring of the two L30 services over an `ExportEnv` (split from `detach_env` so that the
module the tests import stays small)."""

from shelfhold.export.application.locks import ResourceLocks
from tests.detach_env import DetachEnv, Revoker
from tests.export_env import ExportEnv
from tests.kavita_export_env import Wired


def build_services(env: ExportEnv, wired: Wired) -> DetachEnv:
    from shelfhold.export.application.detach import FullDetacher, RootRef
    from shelfhold.export.application.intents import IntentJournal
    from shelfhold.export.application.remove_destination import RemoveDestination
    from shelfhold.export.targets.kavita.settings_restore import restore_library_settings

    library_roots: set[str] = set()  # the roots Shelfhold's own library lives on (a test fills it)
    journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)

    def roots(local: str) -> RootRef | None:
        for root_id, base in (
            (env.lib.store_root, env.lib.store),
            (env.lib.library_root, env.lib.library),
        ):
            if local == str(base) or local.startswith(f"{base}/"):
                return RootRef(root_id, str(base))
        return None

    locks = ResourceLocks(env.engine, clock=env.clock)
    remover = RemoveDestination(
        env.engine, env.lib.fsops, journal, clock=env.clock, roots=roots, locks=locks,
        request_scan=env.scans.append,
    )  # fmt: skip

    detacher = FullDetacher(
        env.engine, env.lib.fsops, journal, clock=env.clock, roots=roots,
        restore=restore_library_settings, library_roots=lambda: library_roots,
        locks=locks, store_path=str(env.lib.store),
    )  # fmt: skip
    return DetachEnv(env, wired, journal, remover, detacher, Revoker(wired.fake), library_roots)
