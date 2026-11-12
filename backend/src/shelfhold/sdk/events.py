# SPDX-License-Identifier: MIT
"""Events of a plugin live in its own space, `plugin.<key>.<name>` (SDK 1.3, ENF-OBS-003).

A plugin names an event by its short `<name>`; the host builds the type and refuses anything that
would speak for the core (`job.failed`) or for another plugin (`plugin.<other>.x`).
"""

import re
from typing import Final

KEY: Final = re.compile(r"[a-z0-9_-]+")
NAME: Final = re.compile(r"[a-z0-9_]+")


class PluginEventError(ValueError):
    """An event type outside the space of the plugin that raised it."""


def plugin_event_type(key: str, name: str) -> str:
    if not KEY.fullmatch(key):
        raise PluginEventError("a plugin key has no event space unless it is [a-z0-9_-]+")
    if not NAME.fullmatch(name):
        raise PluginEventError("an event name is [a-z0-9_]+")
    return f"plugin.{key}.{name}"


def check_plugin_event(key: str, event_type: str) -> None:
    """Raise unless `event_type` is `plugin.<key>.<name>`."""
    prefix = plugin_event_type(key, "x")[:-1]
    if not (event_type.startswith(prefix) and NAME.fullmatch(event_type[len(prefix) :])):
        raise PluginEventError("an event of a plugin is `plugin.<key>.<name>`, its own key only")


__all__ = ["PluginEventError", "check_plugin_event", "plugin_event_type"]
