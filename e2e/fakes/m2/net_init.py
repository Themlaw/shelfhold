"""Gives the namespace of the stack the public-looking address the simulated sources answer on,
on the loopback only (SIOCSIFADDR on an alias of `lo`): packets for it never leave the namespace.
Needs CAP_NET_ADMIN, runs once, as root, and exits."""

import fcntl
import socket
import struct

ADDRESS = "93.184.216.34"
SIOCSIFADDR, SIOCSIFNETMASK, SIOCSIFFLAGS, SIOCGIFFLAGS = 0x8916, 0x891C, 0x8914, 0x8913
IFF_UP = 0x1


def sockaddr(address: str) -> bytes:
    return struct.pack(
        "16sH2s4s8s", b"", socket.AF_INET, b"\x00\x00", socket.inet_aton(address), b""
    )[16:]


def main() -> None:
    name = b"lo:0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        fcntl.ioctl(sock, SIOCSIFADDR, struct.pack("16s", name) + sockaddr(ADDRESS))
        fcntl.ioctl(sock, SIOCSIFNETMASK, struct.pack("16s", name) + sockaddr("255.255.255.255"))
        flags = struct.unpack(
            "16sH", fcntl.ioctl(sock, SIOCGIFFLAGS, struct.pack("16sH", name, 0))
        )[1]
        fcntl.ioctl(sock, SIOCSIFFLAGS, struct.pack("16sH", name, flags | IFF_UP))
    print("NET-INIT", ADDRESS, "on lo", flush=True)


if __name__ == "__main__":
    main()
