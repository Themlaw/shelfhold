"""The subprocess that reads a backup (EF-INT-008, ENF-SEC-002): the same model as the image worker.

Run as `python -I -m shelfhold.platform.import_tachibk.worker '<limits json>'`: one request frame on
stdin (the file), one reply frame on stdout, then exit. It enters the sandbox of the image worker
(no network, no new privileges, bounded memory, CPU time, files and processes) before it reads a
byte of the file. A refusal is a reply, never a crash: `result.refused` carries the closed word and
the code; a success carries the decoded backup as the JSON payload.
"""

from __future__ import annotations

import json
import sys

from shelfhold.infrastructure.imaging.protocol import encode_frame, read_frame
from shelfhold.infrastructure.imaging.worker import _sandbox
from shelfhold.platform.import_tachibk.decode import (
    INVALID,
    Limits,
    TachibkRefusedError,
    decode_backup,
)

MAX_REQUEST_BYTES = 17 * 1024 * 1024  # a little over the compressed bound: refused by its word


def reply_for(stdin: object, state: dict[str, object]) -> bytes:
    try:
        header, data = read_frame(stdin, MAX_REQUEST_BYTES)  # type: ignore[arg-type]
        limits = Limits.from_dict(dict(header.get("args", {}).get("limits", {})))
        backup = decode_backup(data, limits)
        payload = json.dumps(backup.to_dict(), separators=(",", ":")).encode()
        return encode_frame(
            {"ok": True, "result": {"refused": None}, "sandbox": state.get("isolation")}, payload
        )
    except TachibkRefusedError as refused:
        result = {"refused": refused.reason, "code": refused.code}
        return encode_frame({"ok": True, "result": result, "sandbox": state.get("isolation")})
    except MemoryError:
        result = {"refused": "too_expensive", "code": INVALID}
        return encode_frame({"ok": True, "result": result, "sandbox": state.get("isolation")})


def main(argv: list[str]) -> int:  # pragma: no cover - the sandbox; run by the subprocess tests
    out = sys.stdout.buffer
    stdin = sys.stdin.buffer
    sys.stdout = sys.stderr  # nothing a library prints can corrupt the reply frame
    state = _sandbox(json.loads(argv[1]))
    out.write(reply_for(stdin, state))
    out.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point of the subprocess
    raise SystemExit(main(sys.argv))
