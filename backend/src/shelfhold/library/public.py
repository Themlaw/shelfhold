"""What the interfaces and the composition root may use from the library context."""

from shelfhold.library.adapters.export_source import (
    ExportSource,
    export_sources,
    record_destination_copy,
)
from shelfhold.library.adapters.meta_projection import (
    MetaProjection,
    RepairReport,
    meta_path,
    meta_rel,
)
from shelfhold.library.adapters.receive import Received, receive_cbz
from shelfhold.library.adapters.safeguard import Safeguard, SafeguardResult
from shelfhold.library.adapters.storage import (
    DEFAULT_MAX_RECEIVED_BYTES,
    Ingested,
    PassthroughStore,
    RootLocator,
)
from shelfhold.library.adapters.units import (
    DeletionAuthorization,
    DeletionRefusedError,
    Irreplaceability,
    irreplaceability,
    require_deletion_allowed,
    safe_to_clean,
)
from shelfhold.library.domain.content_hash import (
    CONTENT_HASH_BY,
    content_hash,
    is_rendered_sidecar,
)
from shelfhold.library.domain.failures import (
    ReceivedTooLargeError,
    SafeguardFailedError,
    VariantAlreadyStoredError,
)
from shelfhold.library.domain.irreplaceable import (
    ConfirmationRequiredError,
    confirmation_phrase,
)
from shelfhold.library.domain.unit_json import (
    UNIT_JSON_VERSION,
    NewerFormatError,
    UnitJson,
    UnitJsonCorruptError,
)
from shelfhold.library.replica import (
    destination_identities,
    has_readable_copy,
    independent_copies,
    median_unit_bytes,
    readable_unit_ids,
    record_replica,
    refresh_destination_identity,
)

__all__ = [
    "CONTENT_HASH_BY",
    "DEFAULT_MAX_RECEIVED_BYTES",
    "UNIT_JSON_VERSION",
    "ConfirmationRequiredError",
    "DeletionAuthorization",
    "DeletionRefusedError",
    "ExportSource",
    "Ingested",
    "Irreplaceability",
    "MetaProjection",
    "NewerFormatError",
    "PassthroughStore",
    "Received",
    "ReceivedTooLargeError",
    "RepairReport",
    "RootLocator",
    "Safeguard",
    "SafeguardFailedError",
    "SafeguardResult",
    "UnitJson",
    "UnitJsonCorruptError",
    "VariantAlreadyStoredError",
    "confirmation_phrase",
    "content_hash",
    "destination_identities",
    "export_sources",
    "has_readable_copy",
    "independent_copies",
    "irreplaceability",
    "is_rendered_sidecar",
    "median_unit_bytes",
    "meta_path",
    "meta_rel",
    "readable_unit_ids",
    "receive_cbz",
    "record_destination_copy",
    "record_replica",
    "refresh_destination_identity",
    "require_deletion_allowed",
    "safe_to_clean",
]
