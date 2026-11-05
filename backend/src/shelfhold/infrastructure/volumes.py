"""The two volumes of ENF-OPS-012: /data (hot, local) and /store (kept copies)."""

import os
from pathlib import Path, PurePosixPath

from shelfhold.config import CONFIG_FILE_NAME, CONFIG_SCHEMA_VERSION, Settings
from shelfhold.infrastructure.secrets.atomic import write_private


def ensure_volumes(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.store_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "tmp").mkdir(exist_ok=True)  # staging of received files (ENF-SEC-002)
    config = settings.data_dir / CONFIG_FILE_NAME
    if not config.exists():
        temporary = config.with_name(config.name + ".tmp")
        temporary.write_text(f'schema_version = "{CONFIG_SCHEMA_VERSION}"\n', encoding="utf-8")
        os.replace(temporary, config)


def write_data_file(data_dir: Path, rel: str, data: bytes) -> None:
    """Atomically (re)write a regenerable file under /data, e.g. `meta/units/01/<ulid>.json`.

    `/data` is not a confined library root: the content comes from the database, the path from an
    identifier the caller validated. Still, `rel` must be relative with plain names, and no folder
    on the way may be a link (a planted link would send the write elsewhere). Owner-only.
    """
    parts = PurePosixPath(rel).parts
    if not parts or PurePosixPath(rel).is_absolute() or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"not a plain relative path: {rel!r}")
    folder = data_dir
    for part in parts[:-1]:
        folder = folder / part
        if folder.is_symlink():
            raise ValueError(f"{folder} is a link")
    folder.mkdir(parents=True, exist_ok=True)
    write_private(data_dir.joinpath(*parts), data, overwrite=True)
