"""Indexed, canonical unsigned-delta varints over signed-int64 timestamps.

This format is intentionally small and documented, not a production replacement
for a columnar storage engine. CRC32 checks accidental corruption, not authenticity.
Opening validates metadata; payload validation is lazy unless validate_all is called.
"""

from bisect import bisect_left
from dataclasses import dataclass, field
import operator
import struct
import zlib

MIN_I64 = -(1 << 63)
MAX_I64 = (1 << 63) - 1
MAX_U64 = (1 << 64) - 1
MAX_BLOCK_SIZE = 65536
MAGIC = b"TIDX0001"
HEADER = struct.Struct("<8sIIQ")
ENTRY = struct.Struct("<qqQIII")
CRC = struct.Struct("<I")


class FormatError(ValueError):
    """Invalid or corrupted encoded input."""


def _integer(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, not a boolean")
    try:
        return operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def encode_uvarint(value):
    """Canonical little-endian base-128 encoding, with an unsigned-64 bound."""
    value = _integer(value, "varint")
    if not 0 <= value <= MAX_U64:
        raise ValueError("varint is outside uint64")
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def decode_uvarint(data, position=0):
    """Read one bounded, canonical varint; return (value, next_position)."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("varint data must be bytes or bytearray")
    position = _integer(position, "position")
    if position < 0:
        raise FormatError("negative varint offset")
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise FormatError("truncated varint")
        byte = data[position]
        position += 1
        if shift == 63 and byte > 1:
            raise FormatError("varint exceeds uint64 or ten bytes")
        value |= (byte & 127) << shift
        if not byte & 128:
            if shift and byte == 0:
                raise FormatError("noncanonical overlong varint")
            return value, position
    raise FormatError("unterminated varint")


@dataclass(frozen=True)
class Block:
    first: int
    last: int
    offset: int
    length: int
    count: int
    checksum: int


def encode(timestamps, block_size=256):
    """Encode nondecreasing signed-int64 values, preserving every duplicate.

    Input values are not sorted implicitly and floats are never rounded. Python
    integer subtraction safely handles the full 2**64 - 1 span of an int64 delta.
    Timestamp units and epoch are application-defined; they are not in the file.
    """
    block_size = _integer(block_size, "block_size")
    if not 1 <= block_size <= MAX_BLOCK_SIZE:
        raise ValueError(f"block_size must be between 1 and {MAX_BLOCK_SIZE}")
    values = [_integer(value, "timestamp") for value in timestamps]
    previous = MIN_I64
    for value in values:
        if not MIN_I64 <= value <= MAX_I64:
            raise ValueError("timestamp is outside signed int64")
        if value < previous:
            raise ValueError("timestamps must be nondecreasing")
        previous = value
    count = len(values)
    number = (count + block_size - 1) // block_size
    if count > MAX_U64 or number >= 1 << 32:
        raise ValueError("input exceeds format count limits")
    offset = HEADER.size + number * ENTRY.size + CRC.size
    directory, payloads = bytearray(), []
    for start in range(0, count, block_size):
        block = values[start:start + block_size]
        payload = b"".join(encode_uvarint(b - a) for a, b in zip(block, block[1:]))
        if offset + len(payload) > MAX_U64:
            raise ValueError("encoded data exceeds the uint64 offset limit")
        directory.extend(ENTRY.pack(block[0], block[-1], offset, len(payload), len(block),
                                    zlib.crc32(payload)))
        payloads.append(payload)
        offset += len(payload)
    metadata = HEADER.pack(MAGIC, block_size, number, count) + directory
    return metadata + CRC.pack(zlib.crc32(metadata)) + b"".join(payloads)


@dataclass(frozen=True)
class QueryResult:
    count: int
    values: tuple[int, ...] | None
    candidate_blocks: int
    blocks_decoded: int
    payload_bytes_verified: int


@dataclass(frozen=True)
class TimestampIndex:
    """An immutable in-memory index with lazy payload validation.

    Metadata is checked on open. A query checks CRC, decoding bounds, item count,
    and endpoint consistency only for blocks it decodes. count_range can use
    metadata alone for fully covered blocks. Call validate_all for a full scan.
    """

    data: bytes = field(repr=False)
    block_size: int = field(init=False)
    count: int = field(init=False)
    metadata_bytes: int = field(init=False)
    blocks: tuple[Block, ...] = field(init=False)
    _firsts: tuple[int, ...] = field(init=False, repr=False)
    _lasts: tuple[int, ...] = field(init=False, repr=False)
    _prefix: tuple[int, ...] = field(init=False, repr=False)

    def __post_init__(self):
        if not isinstance(self.data, (bytes, bytearray, memoryview)):
            raise FormatError("encoded data must be bytes-like")
        data = bytes(self.data)
        if len(data) < HEADER.size + CRC.size:
            raise FormatError("truncated header")
        magic, size, number, count = HEADER.unpack_from(data)
        if magic != MAGIC:
            raise FormatError("unsupported magic or format version")
        if not 1 <= size <= MAX_BLOCK_SIZE:
            raise FormatError("invalid block size")
        if number != (count + size - 1) // size:
            raise FormatError("inconsistent block count")
        metadata_end = HEADER.size + number * ENTRY.size
        payload_start = metadata_end + CRC.size
        if payload_start > len(data):
            raise FormatError("truncated directory")
        if zlib.crc32(data[:metadata_end]) != CRC.unpack_from(data, metadata_end)[0]:
            raise FormatError("metadata checksum mismatch")
        blocks, prefix = [], [0]
        expected_offset = payload_start
        previous_last = MIN_I64
        for i in range(number):
            block = Block(*ENTRY.unpack_from(data, HEADER.size + i * ENTRY.size))
            expected_count = min(size, count - i * size)
            if block.count != expected_count:
                raise FormatError("inconsistent per-block item count")
            if not previous_last <= block.first <= block.last:
                raise FormatError("unsorted block endpoints")
            if not block.count - 1 <= block.length <= 10 * (block.count - 1):
                raise FormatError("payload length cannot encode the claimed item count")
            if block.count == 1 and (block.first != block.last or block.checksum != 0):
                raise FormatError("inconsistent singleton block")
            if block.offset != expected_offset or block.offset + block.length > len(data):
                raise FormatError("noncontiguous or out-of-bounds payload")
            expected_offset += block.length
            previous_last = block.last
            prefix.append(prefix[-1] + block.count)
            blocks.append(block)
        if expected_offset != len(data):
            raise FormatError("unexpected trailing bytes")
        for name, value in (
            ("data", data), ("block_size", size), ("count", count),
            ("metadata_bytes", payload_start), ("blocks", tuple(blocks)),
            ("_firsts", tuple(b.first for b in blocks)),
            ("_lasts", tuple(b.last for b in blocks)), ("_prefix", tuple(prefix)),
        ):
            object.__setattr__(self, name, value)

    def _decode_block(self, index):
        block = self.blocks[index]
        payload = self.data[block.offset:block.offset + block.length]
        if zlib.crc32(payload) != block.checksum:
            raise FormatError(f"payload checksum mismatch in block {index}")
        values, position = [block.first], 0
        for _ in range(block.count - 1):
            delta, position = decode_uvarint(payload, position)
            value = values[-1] + delta
            if value > MAX_I64:
                raise FormatError("decoded timestamp exceeds int64")
            values.append(value)
        if position != len(payload) or values[-1] != block.last:
            raise FormatError("payload count or last timestamp disagrees with directory")
        return values

    def validate_all(self):
        """Validate every payload without retaining the full decoded series."""
        for i in range(len(self.blocks)):
            self._decode_block(i)

    def decode(self):
        """Validate all payloads and materialize the complete timestamp tuple."""
        return tuple(value for i in range(len(self.blocks)) for value in self._decode_block(i))

    def _candidates(self, lower, upper):
        lower, upper = _integer(lower, "lower"), _integer(upper, "upper")
        if lower > upper:
            raise ValueError("range must satisfy lower <= upper")
        if lower == upper:
            return lower, upper, 0, 0
        start = bisect_left(self._lasts, lower)
        stop = bisect_left(self._firsts, upper)
        return lower, upper, start, stop

    def range_values(self, lower, upper):
        """Return values in [lower, upper); verify every candidate payload."""
        lower, upper, start, stop = self._candidates(lower, upper)
        values, verified = [], 0
        for i in range(start, stop):
            block = self._decode_block(i)
            values.extend(block[bisect_left(block, lower):bisect_left(block, upper)])
            verified += self.blocks[i].length
        return QueryResult(len(values), tuple(values), stop - start, stop - start, verified)

    def count_range(self, lower, upper):
        """Count [lower, upper) using prefix counts and at most two block decodes.

        Fully contained blocks rely on checked metadata, not payload validation.
        Bounds may lie outside int64, e.g. upper=2**63 includes MAX_I64.
        """
        lower, upper, start, stop = self._candidates(lower, upper)
        if start == stop:
            return QueryResult(0, None, 0, 0, 0)
        count = self._prefix[stop] - self._prefix[start]
        decoded, verified = 0, 0
        for i in sorted({start, stop - 1}):
            record = self.blocks[i]
            if lower <= record.first and record.last < upper:
                continue
            values = self._decode_block(i)
            included = bisect_left(values, upper) - bisect_left(values, lower)
            count += included - record.count
            decoded += 1
            verified += record.length
        return QueryResult(count, None, stop - start, decoded, verified)
