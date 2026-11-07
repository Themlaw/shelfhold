"""The one place where a socket peer becomes the `local_cli` credential (EF-USR-024, I47)."""

from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.local_socket import Peer


def principal_for_peer(peer: Peer) -> Principal:
    """Who the transport already vetted (server uid or root, inside the PID namespace unless the
    operator said otherwise). The audit actor is `local_cli:<uid>`."""
    return Principal(
        user_id=str(peer.uid),
        username=f"uid:{peer.uid}",
        role="admin",
        credential=Credential.LOCAL_CLI,
        can_rate=True,  # the host is the administrator (EF-USR-024)
    )
