"""What the interfaces and the composition root may use from the library context."""

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
from shelfhold.library.domain.failures import ReceivedTooLargeError, SafeguardFailedError
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
from shelfhold.library.replica import independent_copies

__all__ = [
    "CONTENT_HASH_BY",
    "DEFAULT_MAX_RECEIVED_BYTES",
    "UNIT_JSON_VERSION",
    "ConfirmationRequiredError",
    "DeletionAuthorization",
    "DeletionRefusedError",
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
    "confirmation_phrase",
    "content_hash",
    "independent_copies",
    "irreplaceability",
    "is_rendered_sidecar",
    "meta_path",
    "meta_rel",
    "receive_cbz",
    "require_deletion_allowed",
    "safe_to_clean",
]
