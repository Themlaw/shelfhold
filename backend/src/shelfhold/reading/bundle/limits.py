"""The bounds of the block (ADR-0053 §8): one place, so a test can lower them."""

from dataclasses import dataclass

from shelfhold.reading.domain.bundle_zip import MAX_PART_BYTES

MIB = 1024 * 1024


@dataclass(frozen=True)
class BundleLimits:
    part_bytes: int = 500 * MIB  # a part is cut above this, on a chapter boundary
    members_per_part: int = 256  # one open descriptor per member for the whole transfer
    chunk_bytes: int = 64 * 1024  # what is read, checked and sent at a time: memory is this
    per_token: int = 1  # simultaneous blocks for one token
    per_instance: int = 2  # and for the whole instance
    min_rate_bps: int = 32 * 1024  # below this over `rate_window_s`, the reader is cut
    rate_window_s: float = 60.0
    keep_versions: int = 3  # per (Edition, part, profile)
    keep_days: int = 90  # plus any version used this recently
    max_entries_per_source: int = 5_000  # entries of one chapter's archive
    max_page_bytes: int = 128 * MIB
    visibility_every_bytes: int = MIB  # the profile's scope is read again at least this often

    def __post_init__(self) -> None:
        if not 0 < self.part_bytes < MAX_PART_BYTES:
            raise ValueError("a part is under 2^31 bytes")
        if self.chunk_bytes < 1 or self.members_per_part < 1:
            raise ValueError("chunk and members are positive")
