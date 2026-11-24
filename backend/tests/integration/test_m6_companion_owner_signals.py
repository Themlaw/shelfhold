"""The seccomp fallback neutralises (returns 0, does nothing) the commands that make the kernel
signal for a plugin (`F_SETOWN`, `F_SETOWN_EX`, `FIOSETOWN`, `SIOCSPGRP`), and the plugin's
environment and ceilings."""

from __future__ import annotations

import struct

import pytest

from shelfhold.plugin_host.adapters.rpc import stdio
from shelfhold.plugin_host.runner import confine

pytestmark = pytest.mark.req("EF-SRC-016", "ENF-SEC-009")

AUDIT = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
CALLS = {"x86_64": {"fcntl": 72, "ioctl": 16}, "aarch64": {"fcntl": 25, "ioctl": 29}}
ALLOW, ZERO = 0x7FFF0000, 0x00050000  # ZERO: the call returns 0 and does nothing


def verdict(program: bytes, arch: str, nr: int, arg1: int) -> int:
    code = [struct.unpack("=HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
    acc, pc = 0, 0
    while True:
        op, jt, jf, k = code[pc]
        if op == 0x20:
            acc = {0: nr, 4: AUDIT[arch], 16: 0, 24: arg1}[k]
        elif op == 0x15:
            pc += jt if acc == k else jf
        elif op == 0x35:
            pc += jt if acc >= k else jf
        elif op == 0x06:
            return k
        pc += 1


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_the_fallback_refuses_the_owner_commands_and_only_those(arch: str) -> None:
    program = confine.build_program(arch, 4321)
    for command in (8, 15):  # F_SETOWN, F_SETOWN_EX
        assert verdict(program, arch, CALLS[arch]["fcntl"], command) == ZERO, command
    for command in (0x8901, 0x8902):  # FIOSETOWN, SIOCSPGRP
        assert verdict(program, arch, CALLS[arch]["ioctl"], command) == ZERO, command
    for command in (1, 3, 4, 9, 14, 1025):  # F_GETFD, F_GETFL, F_SETFL, F_GETOWN, F_SETSIG...
        assert verdict(program, arch, CALLS[arch]["fcntl"], command) == ALLOW, command
    for command in (0x5401, 0x541B, 0x8903):  # TCGETS, FIONREAD, a neighbour
        assert verdict(program, arch, CALLS[arch]["ioctl"], command) == ALLOW, command
    # where Landlock scopes the signals nothing of this is in the program
    free = confine.build_program(arch)
    assert verdict(free, arch, CALLS[arch]["fcntl"], 8) == ALLOW
    assert verdict(free, arch, CALLS[arch]["ioctl"], 0x8901) == ALLOW


def test_the_environment_names_a_home_and_a_user_and_the_ceiling_leaves_room() -> None:
    env = stdio.closed_environment({})
    assert env["HOME"] and env["USER"] and env["LOGNAME"]
    # under the container's `pids_limit` (128), with room for the companion
    assert stdio.MAX_TASKS <= 64
