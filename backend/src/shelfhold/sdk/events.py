# SPDX-License-Identifier: MIT
"""Events of a plugin live in its own space, `plugin.<key>.<name>` (SDK 1.3, ENF-OBS-003).

A plugin names an event by its short `<name>`; the host builds the type and refuses anything that
would speak for the core (`job.failed`) or for another plugin (`plugin.<other>.x`). The key is
normalised as PEP 503 does (`foo_bar`, `Foo.Bar` and `foo-bar` are one plugin, one space).
"""

import re
from typing import Final

KEY: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
NAME: Final = re.compile(r"[a-z0-9_]+")
EVENT_TYPE: Final = re.compile(r"plugin\.[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9_]+")


class PluginEventError(ValueError):
    """An event type outside the space of the plugin that raised it."""


def normalise_key(key: str) -> str:
    return re.sub(r"[-_.]+", "-", key).lower()


def plugin_event_type(key: str, name: str) -> str:
    space = normalise_key(key)
    if not KEY.fullmatch(space):
        raise PluginEventError("a plugin key has no event space unless it is letters and digits")
    if not NAME.fullmatch(name):
        raise PluginEventError("an event name is [a-z0-9_]+")
    return f"plugin.{space}.{name}"


def check_plugin_event(key: str, event_type: str) -> None:
    """Raise unless `event_type` is `plugin.<normalised key>.<name>`."""
    prefix = plugin_event_type(key, "x")[:-1]
    if not (event_type.startswith(prefix) and NAME.fullmatch(event_type[len(prefix) :])):
        raise PluginEventError("an event of a plugin is `plugin.<key>.<name>`, its own key only")


__all__ = [
    "EVENT_TYPE",
    "PluginEventError",
    "check_plugin_event",
    "normalise_key",
    "plugin_event_type",
]
