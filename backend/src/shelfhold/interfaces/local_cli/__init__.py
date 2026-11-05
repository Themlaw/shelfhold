"""The local CLI over the Unix socket: server-side methods and the client-side channel."""

from shelfhold.interfaces.local_cli.dispatch import METHODS, IdentityDispatcher
from shelfhold.interfaces.local_cli.gateway import IdentityCommands, KeyView, SocketIdentity
from shelfhold.interfaces.local_cli.principal import principal_for_peer

__all__ = [
    "METHODS",
    "IdentityCommands",
    "IdentityDispatcher",
    "KeyView",
    "SocketIdentity",
    "principal_for_peer",
]
