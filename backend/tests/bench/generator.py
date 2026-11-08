"""The reference profile of the benches (§7.0 of the spec): a synthetic library that is always the
same one.

2 000 Works, 60 000 Units, 3 M pages, 2 TB, 500 followed series, 6 users. Two sizes of the same
profile: `FULL_PROFILE` (the 2 TB, measured before a release, outside the CI) and `CI_PROFILE`
(the same rows, sparse files of a few GiB, so neither a big disk nor a network is needed).

Deterministic: every identifier and title comes from `random.Random(seed)` and the position of the
row, never from the clock or the OS, so two runs write the same catalog (`Manifest.digest`).
Nothing here reaches the network.

    python -m tests.bench.generator --profile ci --files ./library
    python -m tests.bench.generator --reference   # manifest for .github/bench-reference.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.ids import ALPHABET
from shelfhold.infrastructure.schema import acquisition, catalog, identity

SEED = 20_261_108
_EPOCH_MS = 1_793_000_000_000  # fixed: an identifier must not depend on when it was generated
_WORDS = ("aube", "brume", "cobalt", "delta", "ecume", "fjord", "givre", "houle", "iris", "jade")
_BATCH = 10_000


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    works: int
    units_per_work: int
    pages_per_unit: int
    unit_bytes: int  # apparent size of a Unit's archive
    followed: int
    users: int

    @property
    def units(self) -> int:
        return self.works * self.units_per_work

    @property
    def pages(self) -> int:
        return self.units * self.pages_per_unit

    @property
    def total_bytes(self) -> int:
        return self.units * self.unit_bytes


TERABYTE = 1000**4
FULL_PROFILE = Profile("full", 2_000, 30, 50, 2 * TERABYTE // 60_000, 500, 6)
CI_PROFILE = Profile("ci", 2_000, 30, 50, 48 * 1024, 500, 6)  # 2.7 GiB apparent, sparse


@dataclass(frozen=True, slots=True)
class Manifest:
    profile: str
    seed: int
    works: int
    units: int
    pages: int
    total_bytes: int
    followed: int
    users: int
    digest: str  # sha256 of every generated identifier and title, in order


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


class _Ids:
    """Identifiers shaped like ULIDs (time part fixed and counting up, random part seeded), so
    they sort in generation order and are valid wherever the schema checks the shape."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)  # noqa: S311 - a reproducible profile, not a secret
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return _encode(_EPOCH_MS + self._n, 10) + _encode(self._rng.getrandbits(80), 16)


def _title(rng: random.Random, n: int) -> str:
    return f"{rng.choice(_WORDS).capitalize()} {rng.choice(_WORDS)} {n:04d}"


def unit_path(work: int, unit: int) -> str:
    """Where the archive of Unit `unit` of Work `work` lives under the library root."""
    return f"work-{work:04d}/ch-{unit:03d}.cbz"


def generate_catalog(
    engine: Engine, profile: Profile = CI_PROFILE, *, seed: int = SEED
) -> Manifest:
    """Works, Editions, Units, followed series and users, in bulk, in one transaction."""
    ids = _Ids(seed)
    rng = random.Random(seed + 1)  # noqa: S311 - titles of a reproducible profile
    digest = hashlib.sha256()

    def note(*parts: str) -> None:
        digest.update("\x1f".join(parts).encode() + b"\x1e")

    with engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.work_kind)
            .values(key="manga", label_fr="manga", default_media="images")
            .prefix_with("OR IGNORE")
        )
        works: list[dict[str, object]] = []
        for n in range(profile.works):
            title = _title(rng, n)
            works.append(
                {"id": ids.next(), "kind": "manga", "title": title, "sort_title": title.lower()}
            )
            note(str(works[-1]["id"]), title)
        connection.execute(sa.insert(catalog.work), works)
        editions = [
            {
                "id": ids.next(),
                "work_id": w["id"],
                "lang": "fr",
                "translation_type": "official",
                "media": "images",
            }
            for w in works
        ]
        connection.execute(sa.insert(catalog.edition), editions)
        for e in editions:
            note(str(e["id"]))
        for start in range(0, len(editions), _BATCH // profile.units_per_work):
            units = []
            for e in editions[start : start + _BATCH // profile.units_per_work]:
                for n in range(1, profile.units_per_work + 1):
                    units.append(
                        {
                            "id": ids.next(),
                            "edition_id": e["id"],
                            "number_raw": str(n),
                            "number_norm": str(n),
                            "number_sort": float(n),
                            "track": "main",
                            "kind": "chapter",
                        }
                    )
                    note(str(units[-1]["id"]))
            connection.execute(sa.insert(catalog.unit), units)
        connection.execute(
            sa.insert(acquisition.series_follow),
            [
                {"edition_id": e["id"], "work_id": e["work_id"]}
                for e in editions[: profile.followed]
            ],
        )
        connection.execute(
            sa.insert(identity.user),
            [
                {
                    "id": ids.next(),
                    "username": f"user{n}",
                    "role": "admin" if n == 0 else "member",
                    "can_rate": True,
                }
                for n in range(profile.users)
            ],
        )
    return Manifest(
        profile.name, seed, profile.works, profile.units, profile.pages, profile.total_bytes,
        profile.followed, profile.users, digest.hexdigest(),
    )  # fmt: skip


def iter_unit_paths(profile: Profile) -> Iterator[str]:
    for work in range(profile.works):
        for unit in range(1, profile.units_per_work + 1):
            yield unit_path(work, unit)


def generate_files(root: Path, profile: Profile = CI_PROFILE, *, limit: int | None = None) -> int:
    """Sparse archives under `root`: the apparent size of the profile, almost no blocks. Returns
    how many files were written (`limit` caps it, for a quick check)."""
    written = 0
    for relative in iter_unit_paths(profile):
        if limit is not None and written >= limit:
            break
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.truncate(profile.unit_bytes)
        written += 1
    return written


def reference_manifest(profile: Profile = CI_PROFILE) -> dict[str, object]:
    """What `.github/bench-reference.json` records of the profile, from a scratch database."""
    import tempfile

    from shelfhold.config import load_settings
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.migrate import migrate

    with tempfile.TemporaryDirectory(prefix="shelfhold-ref-") as raw:
        root = Path(raw)
        settings = load_settings(
            {"SHELFHOLD_DATA_DIR": str(root / "data"), "SHELFHOLD_STORE_DIR": str(root / "store")}
        ).values
        migrate(settings)
        engine = create_db_engine(settings.database_path)
        try:
            manifest = generate_catalog(engine, profile)
        finally:
            engine.dispose()
    return asdict(manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile", choices=("ci", "full"), default="ci")
    parser.add_argument("--files", type=Path, help="write the sparse archives under this folder")
    parser.add_argument("--reference", action="store_true", help="print the reference manifest")
    args = parser.parse_args(argv)
    profile = CI_PROFILE if args.profile == "ci" else FULL_PROFILE
    if args.reference:
        print(json.dumps(reference_manifest(profile), indent=2))
    if args.files is not None:
        count = generate_files(args.files, profile)
        print(f"{count} sparse files, {profile.unit_bytes} bytes each", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
