"""The two volumes of ENF-OPS-012: /data (hot, local) and /store (kept copies)."""

from shelfhold.config import CONFIG_FILE_NAME, CONFIG_SCHEMA_VERSION, Settings


def ensure_volumes(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.store_dir.mkdir(parents=True, exist_ok=True)
    config = settings.data_dir / CONFIG_FILE_NAME
    if not config.exists():
        config.write_text(f'schema_version = "{CONFIG_SCHEMA_VERSION}"\n', encoding="utf-8")
