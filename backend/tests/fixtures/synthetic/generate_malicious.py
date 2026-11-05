"""Booby-trapped fixtures, built by code on demand (ENF-SEC-002, ENF-SEC-014, EF-BIB-051).

Nothing here is downloaded or committed as a binary: every trap is produced from a few lines, so
the corpus is reviewable and deterministic in intent. Every archive builder returns bytes; XML
builders return bytes too. `write_all(out_dir)` dumps the whole corpus for manual inspection:
python generate_malicious.py [OUT_DIR]
"""

from __future__ import annotations

import struct
import sys
import warnings
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

FIXED_TIME = (2020, 1, 1, 0, 0, 0)

# --- Magic-byte samples ------------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
GIF_MAGIC = b"GIF89a"
WEBP_MAGIC = b"RIFF\x24\x00\x00\x00WEBPVP8 "
AVIF_MAGIC = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1"
JXL_CODESTREAM_MAGIC = b"\xff\x0a"
JXL_CONTAINER_MAGIC = b"\x00\x00\x00\x0cJXL \r\n\x87\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def tiny_png() -> bytes:
    """A valid 1x1 PNG."""
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x10\x20\x30")
    return PNG_MAGIC + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


SVG_WITH_SCRIPT = (
    b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
    b"<script>alert(1)</script></svg>"
)
HTML_WITH_SCRIPT = b"<!doctype html><html><body><script>alert(1)</script></body></html>"
COMICINFO = b"<ComicInfo><Series>Synthetic</Series><Number>1</Number></ComicInfo>"

# --- XML ---------------------------------------------------------------------------------------


def xxe_file() -> bytes:
    return (
        b'<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>\n<r>&x;</r>'
    )


def xxe_http() -> bytes:
    return (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE r [<!ENTITY x SYSTEM "http://127.0.0.1:9/leak">]>\n<r>&x;</r>'
    )


def external_dtd() -> bytes:
    return b'<?xml version="1.0"?>\n<!DOCTYPE r SYSTEM "http://127.0.0.1:9/evil.dtd">\n<r/>'


def parameter_entity() -> bytes:
    return (
        b'<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY % p SYSTEM "http://127.0.0.1:9/p">%p;]>\n'
        b"<r/>"
    )


def billion_laughs(levels: int = 9, fanout: int = 10) -> bytes:
    lines = ['<?xml version="1.0"?>', "<!DOCTYPE lolz [", '<!ENTITY l0 "lol">']
    for i in range(1, levels):
        lines.append(f'<!ENTITY l{i} "' + f"&l{i - 1};" * fanout + '">')
    lines += ["]>", f"<lolz>&l{levels - 1};</lolz>"]
    return "\n".join(lines).encode()


def quadratic_blowup(size: int = 20_000, uses: int = 2_000) -> bytes:
    return (
        f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "{"a" * size}">]><r>' + "&a;" * uses + "</r>"
    ).encode()


def internal_entity() -> bytes:
    return b'<!DOCTYPE r [<!ENTITY e "v">]><r>&e;</r>'


def deeply_nested(depth: int) -> bytes:
    return b"<a>" * depth + b"</a>" * depth


def wide(children: int) -> bytes:
    return b"<r>" + b"<c/>" * children + b"</r>"


def oversized(size: int) -> bytes:
    return b"<r>" + b"x" * size + b"</r>"


def malformed() -> bytes:
    return b"<r><unclosed></r>"


XML_CORPUS = {
    "xxe_file": xxe_file,
    "xxe_http": xxe_http,
    "external_dtd": external_dtd,
    "parameter_entity": parameter_entity,
    "billion_laughs": billion_laughs,
    "quadratic_blowup": quadratic_blowup,
    "internal_entity": internal_entity,
}

# --- ZIP ---------------------------------------------------------------------------------------


@dataclass
class Member:
    name: str
    data: bytes = b""
    deflate: bool = True
    external_attr: int = 0o644 << 16
    encrypted: bool = False


def zip_bytes(members: Iterable[Member | tuple[str, bytes]]) -> bytes:
    buf = BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names are the point of some traps
        with zipfile.ZipFile(buf, "w") as z:
            for m in members:
                m = Member(*m) if isinstance(m, tuple) else m
                info = zipfile.ZipInfo(m.name, FIXED_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED if m.deflate else zipfile.ZIP_STORED
                info.external_attr = m.external_attr
                z.writestr(info, m.data)
                if m.encrypted:  # writestr resets the flags, so mark the central record after
                    info.flag_bits |= 0x1
    return buf.getvalue()


def good_cbz(pages: int = 3) -> bytes:
    members: list[Member | tuple[str, bytes]] = [
        (f"{i + 1:03d}.png", tiny_png()) for i in range(pages)
    ]
    members.append(("ComicInfo.xml", COMICINFO))
    return zip_bytes(members)


def zip_bomb(uncompressed: int = 8 * 1024 * 1024) -> bytes:
    """One entry of zeros: a few KiB on disk, a huge ratio."""
    return zip_bytes([Member("zeros.bin", b"\x00" * uncompressed)])


def many_entries(count: int) -> bytes:
    return zip_bytes([Member(f"{i:05d}.png", b"", deflate=False) for i in range(count)])


def overlapping_entries() -> bytes:
    """Two central-directory records for the same local data (an overlap bomb)."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("a.bin", FIXED_TIME), b"\x00" * 100_000, zipfile.ZIP_DEFLATED)
        z.writestr(zipfile.ZipInfo("b.bin", FIXED_TIME), b"\x00" * 100_000, zipfile.ZIP_DEFLATED)
        infos = z.infolist()
        infos[1].header_offset = infos[0].header_offset
    return buf.getvalue()


def traversal(name: str) -> bytes:
    return zip_bytes([("001.png", tiny_png()), (name, tiny_png())])


TRAVERSAL_NAMES = {
    "dotdot": "../evil.png",
    "nested_dotdot": "pages/../../evil.png",
    "absolute": "/etc/evil.png",
    "backslash_dotdot": "..\\evil.png",
    "windows_backslash": "pages\\001.png",
    "drive_letter": "C:evil.png",
    "drive_absolute": "C:/evil.png",
    "unc": "//server/share/evil.png",
    "right_to_left_override": "001\u202egnp.exe",
    "zero_width": "001\u200b.png",
    "bidi_isolate": "\u2066001.png",
    "control_char": "001\n.png",
}


# --- Raw ZIP records, for traps zipfile refuses to write -----------------------------------------


def raw_local(name: bytes, data: bytes, extra: bytes = b"") -> bytes:
    header = struct.pack(
        "<4s5H3L2H",
        b"PK\x03\x04", 20, 0, 0, 0, 0, zlib.crc32(data),
        len(data), len(data), len(name), len(extra),
    )  # fmt: skip
    return header + name + extra + data


def raw_central(name: bytes, data: bytes, offset: int, attr: int = 0) -> bytes:
    fixed = struct.pack(
        "<4s6H3L5H2L",
        b"PK\x01\x02",
        20,
        20,
        0,
        0,
        0,
        0,
        zlib.crc32(data),
        len(data),
        len(data),
        len(name),
        0,
        0,
        0,
        0,
        attr,
        offset,
    )
    return fixed + name


def raw_eocd(count: int, cd_size: int, cd_offset: int) -> bytes:
    return struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, count, count, cd_size, cd_offset, 0)


def hidden_local_entry() -> bytes:
    """A local header the central directory does not list (seen by streaming readers only)."""
    hidden = raw_local(b"001.svg", SVG_WITH_SCRIPT)
    page = raw_local(b"002.png", tiny_png())
    directory = raw_central(b"002.png", tiny_png(), len(hidden))
    return hidden + page + directory + raw_eocd(1, len(directory), len(hidden) + len(page))


def trailing_hidden_data() -> bytes:
    """Bytes between the last entry and the central directory."""
    page = raw_local(b"001.png", tiny_png())
    junk = raw_local(b"002.svg", SVG_WITH_SCRIPT)
    directory = raw_central(b"001.png", tiny_png(), 0)
    return page + junk + directory + raw_eocd(1, len(directory), len(page) + len(junk))


def nul_in_name() -> bytes:
    """`002.png\\0.svg` as stored; zipfile would show `002.png`."""
    name = b"002.png\x00.svg"
    local = raw_local(name, tiny_png())
    directory = raw_central(name, tiny_png(), 0)
    return local + directory + raw_eocd(1, len(directory), len(local))


def directory_with_data() -> bytes:
    """An entry named like a directory that carries a regular file's bytes."""
    name = b"index.html/"
    page = raw_local(b"002.png", tiny_png())
    local = raw_local(name, HTML_WITH_SCRIPT)
    directory = raw_central(name, HTML_WITH_SCRIPT, 0, 0o100644 << 16) + raw_central(
        b"002.png", tiny_png(), len(local)
    )
    return local + page + directory + raw_eocd(2, len(directory), len(local) + len(page))


def many_central_records(count: int) -> bytes:
    """A small file whose directory declares `count` records (every one points at offset 0)."""
    local = raw_local(b"a", b"")
    directory = b"".join(raw_central(b"%x" % i, b"", 0) for i in range(count))
    return local + directory + raw_eocd(min(count, 0xFFFF), len(directory), len(local))


def zip64_claiming(count: int, directory_size: int) -> bytes:
    """Tiny file whose ZIP64 end records claim `count` entries and a huge directory."""
    local = raw_local(b"a", b"")
    record = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, directory_size, len(local)
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, len(local), 1)
    eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return local + record + locator + eocd


def utf7_xml() -> bytes:
    return b'<?xml version="1.0" encoding="UTF-7"?>+ADw-a/+AD4-'


def many_attributes(count: int) -> bytes:
    return b"<a " + b" ".join(b"a%d=''" % i for i in range(count)) + b"/>"


def encrypted_entry() -> bytes:
    return zip_bytes([Member("001.png", tiny_png(), encrypted=True)])


def symlink_entry() -> bytes:
    return zip_bytes([Member("link.png", b"/etc/passwd", external_attr=(0o120777 << 16))])


def device_entry() -> bytes:
    return zip_bytes([Member("dev.png", b"", external_attr=(0o020666 << 16))])


def duplicate_names() -> bytes:
    return zip_bytes([("001.png", tiny_png()), ("001.png", tiny_png())])


def case_duplicate_names() -> bytes:
    return zip_bytes([("001.png", tiny_png()), ("001.PNG", tiny_png())])


def unicode_duplicate_names() -> bytes:
    return zip_bytes([("\u00e9.png", tiny_png()), ("e\u0301.png", tiny_png())])


def exotic_compression() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("001.png", FIXED_TIME), tiny_png(), zipfile.ZIP_LZMA)
    return buf.getvalue()


def zip_with_prefix(prefix: bytes = b"<html><script>1</script></html>") -> bytes:
    """A valid archive behind junk: zipfile opens it, the magic bytes must not."""
    return prefix + good_cbz()


def rar_bytes() -> bytes:
    return b"Rar!\x1a\x07\x01\x00" + b"\x00" * 64


def sevenzip_bytes() -> bytes:
    return b"7z\xbc\xaf\x27\x1c\x00\x04" + b"\x00" * 64


def cbz_with(*members: tuple[str, bytes]) -> bytes:
    return zip_bytes([*members, ("ComicInfo.xml", COMICINFO)])


# --- Images (ENF-SEC-003) ----------------------------------------------------------------------


def _png_ihdr(width: int, height: int, color_type: int = 0) -> bytes:
    return _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))


def png_solid(width: int, height: int) -> bytes:
    """A valid 8-bit grey PNG of the given size, all zero: huge pixel counts, tiny file.

    The rows are fed to zlib one by one, so a 100 Mpx image never exists in memory here.
    """
    packer = zlib.compressobj(9)
    row = b"\x00" * (width + 1)
    idat = b"".join(packer.compress(row) for _ in range(height)) + packer.flush()
    return PNG_MAGIC + _png_ihdr(width, height) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def png_header_only(width: int, height: int) -> bytes:
    """A PNG that claims a size and carries no pixel data: a size lie, caught at the header."""
    return PNG_MAGIC + _png_ihdr(width, height) + _chunk(b"IEND", b"")


EPS_TEXT = (
    b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n0 0 moveto 10 10 lineto stroke\nshowpage\n"
)
EPS_DOS_BINARY = (
    b"\xc5\xd0\xd3\xc6" + struct.pack("<5I", 30, len(EPS_TEXT), 0, 0, 0xFFFF) + EPS_TEXT
)


def unlisted_images() -> dict[str, bytes]:
    """Real or plausible files of formats Pillow can open and Shelfhold never accepts."""
    bmp = (
        b"BM"
        + struct.pack("<IHHI", 58, 0, 0, 54)
        + struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 2835, 2835, 0, 0)
        + b"\x00\x00\xff\x00"
    )
    tiff = b"II*\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    ico = (
        b"\x00\x00\x01\x00\x01\x00\x01\x01\x00\x00\x01\x00\x18\x00"
        + struct.pack("<II", 4, 22)
        + bmp[14:]
    )
    return {
        "eps": EPS_TEXT,
        "eps_dos": EPS_DOS_BINARY,
        "bmp": bmp,
        "tiff": tiff,
        "ppm": b"P6\n1 1\n255\n\xff\x00\x00",
        "ico": ico,
        "pdf": b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n",
        "psd": b"8BPS\x00\x01" + b"\x00" * 40,
        "svg": SVG_WITH_SCRIPT,
        "html": HTML_WITH_SCRIPT,
        "garbage": b"\x00\x01\x02\x03" * 64,
        "empty": b"",
    }


def write_all(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, build in XML_CORPUS.items():
        (out_dir / f"{name}.xml").write_bytes(build())
    (out_dir / "zip_bomb.cbz").write_bytes(zip_bomb())
    (out_dir / "overlap.cbz").write_bytes(overlapping_entries())
    (out_dir / "symlink.cbz").write_bytes(symlink_entry())
    (out_dir / "encrypted.cbz").write_bytes(encrypted_entry())
    (out_dir / "duplicates.cbz").write_bytes(duplicate_names())
    (out_dir / "svg.cbz").write_bytes(cbz_with(("001.svg", SVG_WITH_SCRIPT)))
    (out_dir / "bomb_90mpx.png").write_bytes(png_header_only(9500, 9500))
    for key, data in unlisted_images().items():
        (out_dir / f"unlisted_{key}.bin").write_bytes(data)
    for key, name in TRAVERSAL_NAMES.items():
        (out_dir / f"traversal_{key}.cbz").write_bytes(traversal(name))


if __name__ == "__main__":
    write_all(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("malicious"))
