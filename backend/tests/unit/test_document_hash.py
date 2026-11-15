"""ENF-QUAL-005: KOReader's partial MD5 and Kavita's variant, against vectors this code did not make.

Where the expected values come from (frozen below, never recomputed by the module under test):

* KOReader: `util.partialMD5` of koreader/koreader, `frontend/util.lua` lines 1094-1112, file last
  changed by commit 78d586b420835e85cacf4fc72f1a7e7a40741afd (master 457a9684503d at the time).
  The function text was cut out of that file verbatim and run under LuaJIT 2.1 (`bit.lshift`
  included, through `lupa`), with `md5` bridged to `hashlib` and `io` left as Lua's own. The run
  also logged every `lshift` result: the offsets below are what it observed.
* Kavita: `KoreaderHelper.HashContents` of Kareadita/Kavita, `Kavita.Services/Helpers/KoreaderHelper.cs`
  lines 38-70 (develop e844b64f2d64, file last changed by c62b20f54bec; the same loop sits at lines
  52-54 of the v0.9.1.4 tag, 879b8b73eb41). No .NET runtime was at hand: the vectors come from a
  line-by-line port written from that source, with C# `int << int` semantics (count masked to
  5 bits, 32-bit wrap) and `ToUpper()`, kept apart from this repository.

What the two sources say, and what a first reading would get wrong:

* The first offset is 0, not 256. `lshift(1024, 2*i)` with `i = -1` is `lshift(1024, -2)`: the
  shift count is taken modulo 32 (30), and 1024 << 30 is 2**40, which is 0 on 32 bits. KOReader
  and Kavita (`step << 2 * i`, `i = -1`) agree. The offsets are 0, 1 Ki, 4 Ki, ... 4**i Ki.
* KOReader loops `i = -1 .. 10` (12 samples, the last at 1 Gi). Kavita loops `i = -1 .. 9`
  (`i < 10`: 11 samples, the last at 256 Mi): a file whose bytes reach 1 Gi gets two hashes.
* Both stop at the first sample that reads no byte, and hash a short last sample as it is.
* KOReader's digest is lowercase hex, Kavita's uppercase.

The synthetic files are deterministic (`pattern`): whole below 64 KiB, and above that only the
windows at the offsets are written and the rest is a hole (`truncate`), so the 3 GiB file costs a few
KiB. `file_id` is the SHA-256 of the size and of every possible window: it proves the reference
run and this test read the same bytes.
"""

import hashlib
import io
import struct
from pathlib import Path
from typing import BinaryIO

import pytest

from shelfhold.reading.domain.document_hash import (
    KAVITA_OFFSETS,
    KOREADER_OFFSETS,
    SAMPLE_SIZE,
    kavita_hash,
    koreader_partial_md5,
)

pytestmark = pytest.mark.req("ENF-QUAL-005")

GIB = 1 << 30

# What the LuaJIT run logged for `lshift(step, 2*i)`, i = -1 .. 10.
OBSERVED_KOREADER_OFFSETS = (
    0, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864, 268435456, GIB,
)  # fmt: skip

# (size, file_id, KOReader hex, Kavita hex), from the reference runs described above.
VECTORS: tuple[tuple[int, str, str, str], ...] = (
    (0, "af5570f5a1810b7af78caf4bc70a660f0df51e42baf91d4de5b2328de0e83dfc",
     "d41d8cd98f00b204e9800998ecf8427e",
     "D41D8CD98F00B204E9800998ECF8427E"),
    (1, "3314dcb12687aeca2ac27babeef7470523d9d36fdaa2d2169ecbede5f94abd53",
     "8bb6c17838643f9691cc6a4de6c51709",
     "8BB6C17838643F9691CC6A4DE6C51709"),
    (255, "5bb70f732baf186e94b800cd2c2b3f05e1ade6c02e9041f171ae4eb22a7cfae2",
     "f2ffaa9486a97476e990d32a47e9b6b4",
     "F2FFAA9486A97476E990D32A47E9B6B4"),
    (256, "26d3308dc2bb93119ed2252ae27c59248c0db002ead2c0b59cbc8e66301d688e",
     "2fdd38c4f9f472c9b903e9d7aacd1525",
     "2FDD38C4F9F472C9B903E9D7AACD1525"),
    (257, "02370c219a11e6cc4847edc5b5d398d2613f7d1e29725b47687f63234601703a",
     "03c1a5a6d0e61a73fb6dff177a99bc71",
     "03C1A5A6D0E61A73FB6DFF177A99BC71"),
    (1023, "d360d9eef8bd826c3bce0063b0f52a160f730cb1962c8950600718e419d6acdd",
     "961141e39586397c636523252730f5d2",
     "961141E39586397C636523252730F5D2"),
    (1024, "bf39dd9573f2d4907fe994681c4c676be05fb444d29db96815e38521bd1b07e9",
     "8fa8ebf39da79329d074e547e5e1f132",
     "8FA8EBF39DA79329D074E547E5E1F132"),
    (1279, "1ddf0ab345ab57b0f7a2a35b42dffe58e854667c86ed574118ea7bdccfc11a72",
     "a58a12b2ae7deaef3cc5c9f427d1f186",
     "A58A12B2AE7DEAEF3CC5C9F427D1F186"),
    (1280, "e7d5ed853339a434177001d41851cba56bb7390c0b813e5edd807180665a1977",
     "96f16befef52f0865f0db2e0adbff3bb",
     "96F16BEFEF52F0865F0DB2E0ADBFF3BB"),
    (1281, "1bb03a971d96ebb40d8766178acdf4a54f4e97d42ef165fed471ce1101a2e3a1",
     "adedf3b44229cfadabc5f4880a670b30",
     "ADEDF3B44229CFADABC5F4880A670B30"),
    (1989, "8b35096e55ccc827c888cb898e3c7c31ff2dd8dec02851c0ed22dc0355807ef3",
     "da47748fd970d77a9b2adb67af11e069",
     "DA47748FD970D77A9B2ADB67AF11E069"),
    (4095, "d0b6234ddbc32b0f4c5c6907a918b00cfe14188c32180daa586724fe3702205a",
     "5b38422081d40acc173af6afa6e065ff",
     "5B38422081D40ACC173AF6AFA6E065FF"),
    (4096, "83823a865287a4932fb4fba7c9f709e5dbfe524880b13869ba1110a0921d4849",
     "5b38422081d40acc173af6afa6e065ff",
     "5B38422081D40ACC173AF6AFA6E065FF"),
    (4097, "74c7e4a2e3d114f36b08a5c54a52f84d7f127aec4a135b412d2071fe569bfdca",
     "6f19fd39d3f523df177b3bc55da5769e",
     "6F19FD39D3F523DF177B3BC55DA5769E"),
    (5119, "c0db3e5c80df661b9252e9d9c706eada83f6a92d7232091ed9d820ba6cc2869b",
     "daa818a484bdb55c71d3d3919fca522f",
     "DAA818A484BDB55C71D3D3919FCA522F"),
    (5120, "e626f6c77327a18cb06dc135e99dcd87af3ccc40ca4b3e87919a18be950bcb1f",
     "ef08d98e993d65f29a81682e53b6c971",
     "EF08D98E993D65F29A81682E53B6C971"),
    (5121, "faf80655eb8ec60023872bf6d4dfff0d04d60406c56abccecb45f770c2c8f0a1",
     "ef08d98e993d65f29a81682e53b6c971",
     "EF08D98E993D65F29A81682E53B6C971"),
    (16383, "73ae382358bd733fb74074770c10a217352643078b72f0b546a844b80c81bc9d",
     "ef08d98e993d65f29a81682e53b6c971",
     "EF08D98E993D65F29A81682E53B6C971"),
    (16384, "4f39269cb2dea0436e464e2c34525493f1dd9767a24c2e4005e56ac9d0e3add5",
     "ef08d98e993d65f29a81682e53b6c971",
     "EF08D98E993D65F29A81682E53B6C971"),
    (16385, "8cb1b28bcb6d24e0df6af186f3ad8e98d732739cf176d98f3312635a707116ae",
     "01bf6c8e08e0e2d02859afab2f1f0640",
     "01BF6C8E08E0E2D02859AFAB2F1F0640"),
    (17407, "346ea82f0b29305cf47760207c15cd2af132f1b6674127587f1b13af0c6c22c4",
     "421fc71c0aa7f9fa90a1461502459f16",
     "421FC71C0AA7F9FA90A1461502459F16"),
    (17408, "a4f3fc82fa7b372c0f36df4578c80633f4180b15c41606c1029dcd2409e39624",
     "1a7a72ba6b71a492b9e1c817ab5510ff",
     "1A7A72BA6B71A492B9E1C817AB5510FF"),
    (65535, "c09b050924a5035cdbafc6e6b897668ec2c0b1872ed0f4efd157a7793f4f7981",
     "1a7a72ba6b71a492b9e1c817ab5510ff",
     "1A7A72BA6B71A492B9E1C817AB5510FF"),
    (65536, "5272e215705f918f7060c33a5b94b8b9731da026aa449b89f5ea8c1b95b56cd4",
     "1a7a72ba6b71a492b9e1c817ab5510ff",
     "1A7A72BA6B71A492B9E1C817AB5510FF"),
    (65537, "2b22020c9bb56287ddecdb0da80b1fd866510e4e12d75ce66e849103bec76c61",
     "4c2816e4bcdfefa906b3655746af069d",
     "4C2816E4BCDFEFA906B3655746AF069D"),
    (66559, "30ac9462658d0173e45de53d39ba837957f7a296ba9bdee0380e4db0db5ca917",
     "2ef0c52ae22660ba6bb39f659e525aa9",
     "2EF0C52AE22660BA6BB39F659E525AA9"),
    (66560, "093fdbbddc363fb013b716aa0d3fbd1efd012944ad8867de70872d2e5e8487e8",
     "10a4e19a84ba53963439b7a2731ebf34",
     "10A4E19A84BA53963439B7A2731EBF34"),
    (262144, "08e7f3ccac866d5ed13ff0c3f67d8962a90033ac11358472366d1f8d53c44cd9",
     "10a4e19a84ba53963439b7a2731ebf34",
     "10A4E19A84BA53963439B7A2731EBF34"),
    (262145, "f8b6d235323ee12dff1f7363c586502e25ffd5edfe8a50c9f49caa194ec20c95",
     "a37eb91ff735357bf2c816578837e906",
     "A37EB91FF735357BF2C816578837E906"),
    (1048576, "875a0a85051e341300f5beb977129b8aa16b5b37687e43ddf4911b848b5c8998",
     "6f7bc730c141ebc54bbc738da193cf9b",
     "6F7BC730C141EBC54BBC738DA193CF9B"),
    (1048577, "2596e0bc35174af61e30b2cea79855285ae866144a899a8cf0ae53feaaa3bf0a",
     "a4b5c22421818052ce77ad64afef9578",
     "A4B5C22421818052CE77AD64AFEF9578"),
    (16777216, "07f3d798595bb39a0c80fddc3d24db4f99a5873e8bfdfa4f0f48286398269cf0",
     "87c06b689036100e04b8305f3e259877",
     "87C06B689036100E04B8305F3E259877"),
    (16777217, "be675265899183de65f7c1fe24c47752f681ca32f01b2c7246d9194c11c7f85d",
     "6200c6bbfec3442a2ce95bca4e008c2f",
     "6200C6BBFEC3442A2CE95BCA4E008C2F"),
    (67108864, "5e096263caa39834ff7c8ece64a6cd8314a909144f0b83cb067c0746c10e9bb3",
     "c143ae1c65fd246e8bc2a1580e9f1e0a",
     "C143AE1C65FD246E8BC2A1580E9F1E0A"),
    (67108865, "1f632b1d5fb29c5d04a37d5104c35d93e7b9dc3cc6f35cc21c95173c4a12eac7",
     "55f9558a1e4a5afc1be32eb42a62c1a6",
     "55F9558A1E4A5AFC1BE32EB42A62C1A6"),
    (268435456, "1fe577757f3ad71714e87a3f4e901430abd78b3ed7c5709c8611bbcdb23859ec",
     "2250b0c66336c8a668be31ff4b3e81c3",
     "2250B0C66336C8A668BE31FF4B3E81C3"),
    (268435457, "97f49e490ffec42c73cbbbb6c14cf47a7db6a497e72079329c2da9b80db9d39e",
     "e742716e32965ec09eedc7ab98dca2f1",
     "E742716E32965EC09EEDC7AB98DCA2F1"),
    (1073741823, "1f5879fe9d5b1effb89be6c0f172d7db8c80776c8fa4e03ee35cc6ad5daf0f85",
     "13ede3d2b40c54651581a79d4c74a1a7",
     "13EDE3D2B40C54651581A79D4C74A1A7"),
    (1073741824, "b50a52d985556b63a352e2fb52e27b408d1e1e205fc230eb5ec0eddbee72dfc1",
     "13ede3d2b40c54651581a79d4c74a1a7",
     "13EDE3D2B40C54651581A79D4C74A1A7"),
    (1073741825, "2c4bfd8f88bdee60ce53615183abd4fff3926b6287158a5dda790b451d88c87c",
     "6c869d3277a808dda054dd0f634d3027",
     "13EDE3D2B40C54651581A79D4C74A1A7"),
    (1073742847, "133423940ae1f9b2f230d4c6ac6e428db936ef4c15d55fffd470480bfe2a6bed",
     "8e0dd1ef70d784dfdbae5a57cc6073b7",
     "13EDE3D2B40C54651581A79D4C74A1A7"),
    (1073742848, "72befc945638d989766a99a039bef20aa5a74f1559bbc0bc56f80d768a3130d7",
     "67d256b23481e0dd22fff5afcf564d5a",
     "13EDE3D2B40C54651581A79D4C74A1A7"),
    (1073746824, "4c802149e95ebdbed4ce1e10d9605cc024c12c81bf93aa8e6a439cf76cfb41f1",
     "67d256b23481e0dd22fff5afcf564d5a",
     "13EDE3D2B40C54651581A79D4C74A1A7"),
    (3221225472, "b9032a471be33532a92e891e321be0ba36b2feef9a8bad4069822fc60715ba14",
     "67d256b23481e0dd22fff5afcf564d5a",
     "13EDE3D2B40C54651581A79D4C74A1A7"),
)  # fmt: skip


def _pattern(position: int) -> int:
    return (position * 167 + (position >> 10) * 13 + 5) & 0xFF


def _windows() -> list[int]:
    return [0] + [1024 << (2 * i) for i in range(11)]


def synthetic_file(path: Path, size: int) -> Path:
    with path.open("wb") as handle:
        handle.truncate(size)
        if size <= 1 << 16:
            handle.write(bytes(_pattern(p) for p in range(size)))
        else:
            for offset in _windows():
                if offset < size:
                    count = min(1024, size - offset)
                    handle.seek(offset)
                    handle.write(bytes(_pattern(offset + k) for k in range(count)))
    return path


def file_id(path: Path, size: int) -> str:
    digest = hashlib.sha256(struct.pack(">Q", size))
    with path.open("rb") as handle:
        for offset in _windows():
            handle.seek(offset)
            digest.update(handle.read(1024))
    return digest.hexdigest()


def _label(size: int) -> str:
    return f"{size}" if size < 1 << 20 else f"{size / (1 << 20):g}MiB"


def test_sample_offsets_follow_the_reference_trace() -> None:
    assert SAMPLE_SIZE == 1024
    assert tuple(KOREADER_OFFSETS) == OBSERVED_KOREADER_OFFSETS  # 12 samples, the first at 0
    assert tuple(KAVITA_OFFSETS) == OBSERVED_KOREADER_OFFSETS[:-1]  # 11 samples, none at 1 Gi


@pytest.mark.parametrize(
    ("size", "expected_file", "koreader_hex", "_kavita"), VECTORS, ids=[_label(v[0]) for v in VECTORS]
)
def test_koreader_partial_md5_vectors(
    tmp_path: Path, size: int, expected_file: str, koreader_hex: str, _kavita: str
) -> None:
    path = synthetic_file(tmp_path / "f.bin", size)
    assert file_id(path, size) == expected_file  # the same bytes the reference hashed
    assert koreader_partial_md5(path) == koreader_hex
    with path.open("rb") as stream:
        stream.seek(7)  # a stream is sampled by absolute offsets, wherever it stands
        assert koreader_partial_md5(stream) == koreader_hex


@pytest.mark.parametrize(
    ("size", "expected_file", "koreader_hex", "kavita_hex"), VECTORS, ids=[_label(v[0]) for v in VECTORS]
)
def test_kavita_hash_compat_uppercase_and_11_samples_divergence_over_1gib(
    tmp_path: Path, size: int, expected_file: str, koreader_hex: str, kavita_hex: str
) -> None:
    path = synthetic_file(tmp_path / "f.bin", size)
    assert file_id(path, size) == expected_file
    got = kavita_hash(path)
    assert got == kavita_hex
    assert got == got.upper()  # uppercase, unlike KOReader's lowercase
    if size > GIB:  # a byte at 1 Gi: KOReader hashes a 12th sample, Kavita never reads it
        assert got.lower() != koreader_hex
    else:
        assert got.lower() == koreader_hex


def test_the_divergence_starts_with_the_first_byte_past_1gib() -> None:
    by_size = {v[0]: v for v in VECTORS}
    assert by_size[GIB][2] == by_size[GIB][3].lower()  # 1 Gi exactly: the 12th read is empty
    assert by_size[GIB + 1][2] != by_size[GIB + 1][3].lower()  # one byte at 1 Gi is enough


class _Spy(io.RawIOBase):
    """A positionable stream that logs what is read, and may answer short."""

    def __init__(self, inner: BinaryIO, *, chunk: int | None = None) -> None:
        self.inner, self.chunk = inner, chunk
        self.reads: list[int] = []

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.inner.seek(offset, whence)

    def tell(self) -> int:
        return self.inner.tell()

    def readinto(self, buffer: bytearray) -> int:  # type: ignore[override]
        want = len(buffer) if self.chunk is None else min(len(buffer), self.chunk)
        data = self.inner.read(want)
        buffer[: len(data)] = data
        self.reads.append(len(data))
        return len(data)


def test_only_the_samples_are_read_never_the_whole_file(tmp_path: Path) -> None:
    size = 3 * GIB
    path = synthetic_file(tmp_path / "f.bin", size)
    with path.open("rb", buffering=0) as raw:
        spy = _Spy(raw)
        koreader_partial_md5(spy)
    assert 0 < sum(spy.reads) <= 12 * SAMPLE_SIZE
    assert max(spy.reads) <= SAMPLE_SIZE


def test_a_short_read_in_the_middle_of_a_sample_does_not_end_it(tmp_path: Path) -> None:
    by_size = {v[0]: v for v in VECTORS}
    size = 17408  # four samples, the last one 1 KiB exactly
    path = synthetic_file(tmp_path / "f.bin", size)
    with path.open("rb", buffering=0) as raw:
        assert koreader_partial_md5(_Spy(raw, chunk=100)) == by_size[size][2]
        raw.seek(0)
        assert kavita_hash(_Spy(raw, chunk=100)) == by_size[size][3]


def test_a_path_is_a_path_not_a_content(tmp_path: Path) -> None:
    path = synthetic_file(tmp_path / "f.bin", 1024)
    with pytest.raises(TypeError):
        koreader_partial_md5(str(path))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        kavita_hash(b"some bytes")  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        koreader_partial_md5(tmp_path / "missing.bin")
