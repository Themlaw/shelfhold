"""What the full detachment needs to give a Kavita library its settings back (M3 L30).

The application layer does not know Kavita: it is handed a function (`RestoreSettings`) that
takes the client of the target, the reference of the library and what it was before the
adoption, and answers with what was done. The Kavita side is `targets/kavita/settings_restore.py`.
"""

from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["RestoreSettings", "SettingsRestored"]


@dataclass(frozen=True)
class SettingsRestored:
    library_ref: str
    exclude_patterns_restored: bool  # something was written (False: already as it should be)
    patterns_now: tuple[str, ...]
    library_type_before: str | None
    library_type_now: str
    type_unchanged: bool  # True when no "before" was recorded: nothing to compare


RestoreSettings = Callable[
    [Any, str, Mapping[str, Any] | None], Coroutine[Any, Any, SettingsRestored]
]
