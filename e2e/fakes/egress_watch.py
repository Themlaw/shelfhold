"""Records every packet leaving the network namespace towards an address outside the allowed
subnet (ENF-PRIV-001), and every DNS query for a name that is not a compose service.

Prints `EGRESS ...` lines. Needs CAP_NET_RAW; runs in the container that owns the namespace so
that nothing is missed during start-up. Test double, not shipped in the image.

Usage: egress_watch.py <allowed-subnet|auto> [allowed-dns-name ...]
"""

import fcntl
import ipaddress
import socket
import struct
import sys
from pathlib import Path

ETH_P_ALL = 0x0003
PACKET_OUTGOING = 4
READY = Path("/tmp/watch-ready")  # noqa: S108
EMBEDDED_DNS = ipaddress.ip_address("127.0.0.11")


def _qname(payload: bytes) -> str:
    labels, position = [], 12  # DNS header is 12 bytes
    while position < len(payload) and payload[position]:
        size = payload[position]
        labels.append(payload[position + 1 : position + 1 + size].decode("ascii", "replace"))
        position += size + 1
    return ".".join(labels).lower()


def inspect(frame: bytes, allowed: ipaddress.IPv4Network, dns_ok: set[str]) -> str | None:
    """Return a description of the packet when it is an egress attempt, else None."""
    ethertype = struct.unpack("!H", frame[12:14])[0]
    if ethertype == 0x0800 and len(frame) >= 34:
        header_len = (frame[14] & 0x0F) * 4
        proto = frame[23]
        dst = ipaddress.ip_address(frame[30:34])
        transport = frame[14 + header_len :]
        port = (
            struct.unpack("!H", transport[2:4])[0]
            if proto in (6, 17) and len(transport) >= 4
            else 0
        )
        if dst == EMBEDDED_DNS and proto == 17:  # the daemon DNATs :53 to a random port
            name = _qname(transport[8:])
            return None if name in dns_ok else f"EGRESS dns name={name}"
        if (
            dst.is_loopback
            or dst in allowed
            or dst.is_multicast
            or dst == allowed.broadcast_address
        ):
            return None
        return f"EGRESS proto={proto} dst={dst}:{port}"
    if ethertype == 0x86DD and len(frame) >= 54:
        dst6 = ipaddress.ip_address(frame[38:54])
        if dst6.is_loopback or dst6.is_multicast or dst6.is_link_local:
            return None
        return f"EGRESS ipv6 dst={dst6}"
    return None


def own_subnet(interface: str = "eth0") -> ipaddress.IPv4Network:
    """Subnet of the container's own network (SIOCGIFADDR / SIOCGIFNETMASK)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        request = struct.pack("256s", interface.encode())
        address = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x8915, request)[20:24])
        netmask = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x891B, request)[20:24])
    return ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)


def main(subnet: str, dns_names: list[str]) -> None:
    allowed = own_subnet() if subnet == "auto" else ipaddress.IPv4Network(subnet)
    dns_ok = set(dns_names)
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    READY.write_text("ready", encoding="utf-8")
    print("WATCH-READY", flush=True)
    seen: set[str] = set()
    while True:
        frame, address = sock.recvfrom(65535)
        if address[2] != PACKET_OUTGOING or len(frame) < 14:
            continue
        finding = inspect(frame, allowed, dns_ok)
        if finding and finding not in seen:
            seen.add(finding)
            print(finding, flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
