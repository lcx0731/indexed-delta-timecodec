from bisect import bisect_left
from itertools import combinations_with_replacement
import random
import struct
import zlib

import pytest

from timecodec import FormatError, TimestampIndex, encode
from timecodec.codec import MAX_I64, MIN_I64, MAX_U64, decode_uvarint, encode_uvarint


def wire(records, size=4, *, count=None):
    """Independent format fixture builder: records are (first,last,count,payload)."""
    total = sum(record[2] for record in records) if count is None else count
    header = struct.pack("<8sIIQ", b"TIDX0001", size, len(records), total)
    directory, payloads, offset = [], [], 28 + 36 * len(records)
    for first, last, items, payload in records:
        directory.append(struct.pack("<qqQIII", first, last, offset, len(payload), items,
                                     zlib.crc32(payload)))
        payloads.append(payload)
        offset += len(payload)
    metadata = header + b"".join(directory)
    return metadata + struct.pack("<I", zlib.crc32(metadata)) + b"".join(payloads)


@pytest.mark.parametrize("value,expected", [
    (0, b"\x00"), (1, b"\x01"), (127, b"\x7f"), (128, b"\x80\x01"),
    (150, b"\x96\x01"), (16383, b"\xff\x7f"), (16384, b"\x80\x80\x01"),
    (MAX_U64, b"\xff" * 9 + b"\x01"),
])
def test_known_varint_bytes(value, expected):
    assert encode_uvarint(value) == expected
    assert decode_uvarint(expected) == (value, len(expected))
    assert decode_uvarint(b"prefix" + expected + b"suffix", 6) == (value, 6 + len(expected))


@pytest.mark.parametrize("payload", [b"", b"\x80", b"\x80\x00", b"\x81\x00",
                                      b"\xff" * 9 + b"\x02", b"\x80" * 11])
def test_invalid_varints(payload):
    with pytest.raises(FormatError):
        decode_uvarint(payload)


@pytest.mark.parametrize("value", [-1, 1 << 64, True, 1.0, "1"])
def test_bad_varint_inputs(value):
    with pytest.raises(ValueError):
        encode_uvarint(value)


def test_varint_random_values_against_positional_base128_oracle():
    rng = random.Random(101)
    for _ in range(1000):
        value = rng.getrandbits(64)
        encoded = encode_uvarint(value)
        reference = sum((byte % 128) * 128 ** i for i, byte in enumerate(encoded))
        assert reference == value
        assert len(encoded) == max(1, (value.bit_length() + 6) // 7)
        assert decode_uvarint(encoded)[0] == value


def test_independent_wire_fixture_and_exact_payload():
    expected = wire([(-4, 16380, 4, b"\x00\x80\x01\x80\x7f")])
    assert encode([-4, -4, 124, 16380], 4) == expected
    assert TimestampIndex(expected).decode() == (-4, -4, 124, 16380)
    assert len(expected) == 69


def test_full_signed_range_needs_unsigned_64_delta():
    encoded = encode([MIN_I64, MAX_I64], 2)
    index = TimestampIndex(encoded)
    assert index.decode() == (MIN_I64, MAX_I64)
    assert encoded[index.metadata_bytes:] == b"\xff" * 9 + b"\x01"
    assert index.range_values(MIN_I64, MAX_I64 + 1).values == (MIN_I64, MAX_I64)
    assert index.count_range(MAX_I64, MAX_I64 + 1).count == 1


def test_empty_and_singleton_files():
    empty = TimestampIndex(encode([], 256))
    assert len(empty.data) == empty.metadata_bytes == 28
    assert empty.decode() == () and empty.count == 0
    assert empty.range_values(-100, 100).values == ()
    single = TimestampIndex(encode([MIN_I64], 1))
    assert len(single.data) == single.metadata_bytes == 64
    assert single.decode() == (MIN_I64,)
    assert single.count_range(MIN_I64, 0).count == 1
    single.validate_all()


@pytest.mark.parametrize("values,size", [
    ([2, 1], 2), ([MIN_I64 - 1], 2), ([MAX_I64 + 1], 2),
    ([1.0], 2), ([True], 2), (["1"], 2), ([None], 2),
    ([], 0), ([], -1), ([], 65537), ([], 1.5), ([], True),
])
def test_encode_rejects_invalid_inputs(values, size):
    with pytest.raises(ValueError):
        encode(values, size)


def test_bytearray_mutation_cannot_change_an_open_index():
    data = bytearray(encode([1, 2, 3]))
    index = TimestampIndex(data)
    data[:] = b"\x00" * len(data)
    assert index.decode() == (1, 2, 3)


def test_all_truncations_and_single_bit_corruptions_fail_full_validation():
    encoded = encode([-4, -4, 124, 16380, 16381], 4)
    for length in range(len(encoded)):
        with pytest.raises(FormatError):
            TimestampIndex(encoded[:length]).validate_all()
    for byte in range(len(encoded)):
        for bit in range(8):
            changed = bytearray(encoded)
            changed[byte] ^= 1 << bit
            with pytest.raises(FormatError):
                TimestampIndex(changed).validate_all()


@pytest.mark.parametrize("records,size", [
    ([(0, 0, 2, b"\x80\x00")], 2),  # noncanonical zero, valid CRC
    ([(0, 0, 2, b"\x80")], 2),      # truncated varint
    ([(MAX_I64, MAX_I64, 2, b"\x01")], 2),  # reconstructed int64 overflow
    ([(0, 2, 2, b"\x01")], 2),      # wrong last checkpoint
    ([(0, 1, 2, b"\x01\x01")], 2), # extra delta
    ([(0, 0, 1, b"\x00")], 1),      # singleton with payload
    ([(0, 1, 1, b"")], 1),           # singleton endpoints disagree
    ([(2, 2, 1, b""), (1, 1, 1, b"")], 1), # unordered directory
    ([(0, 1, 2, b"\x01"), (2, 2, 1, b"")], 3), # incorrect full-block count
])
def test_recomputed_checksums_do_not_bypass_structural_checks(records, size):
    with pytest.raises(FormatError):
        TimestampIndex(wire(records, size)).validate_all()


def test_metadata_only_count_does_not_claim_to_validate_skipped_payload():
    changed = bytearray(encode(list(range(12)), 4))
    original = TimestampIndex(changed)
    changed[original.blocks[1].offset] ^= 1
    index = TimestampIndex(changed)  # directory and its CRC are still valid
    summary = index.count_range(-100, 100)
    assert summary.count == 12 and summary.blocks_decoded == 0
    assert summary.payload_bytes_verified == 0
    assert index.range_values(0, 2).values == (0, 1)  # a different block
    with pytest.raises(FormatError, match="payload checksum"):
        index.range_values(4, 8)
    with pytest.raises(FormatError):
        index.validate_all()


def test_duplicates_across_block_boundaries_are_not_deduplicated():
    values = [-1] * 3 + [0] * 15 + [1] * 4
    index = TimestampIndex(encode(values, 4))
    assert index.range_values(0, 1).values == (0,) * 15
    counted = index.count_range(0, 1)
    assert counted.count == 15 and counted.blocks_decoded <= 2
    assert index.count_range(0, 0).count == 0
    assert index.range_values(1, 2).values == (1,) * 4


def test_all_small_sorted_multisets_against_direct_predicates():
    # 252 inputs, 3 block sizes, 28 ranges: 21,168 range/count query pairs.
    for n in range(6):
        for values in combinations_with_replacement(range(-2, 3), n):
            for size in (1, 2, 4):
                index = TimestampIndex(encode(values, size))
                assert index.decode() == values
                for lower, upper in combinations_with_replacement(range(-3, 4), 2):
                    expected = tuple(value for value in values if lower <= value < upper)
                    assert index.range_values(lower, upper).values == expected
                    count = index.count_range(lower, upper)
                    assert count.count == len(expected)
                    assert count.blocks_decoded <= 2


def test_randomized_roundtrips_and_bisect_oracle():
    rng = random.Random(20261021)
    for _ in range(400):
        values = sorted(rng.getrandbits(64) + MIN_I64 for _ in range(rng.randrange(80)))
        if values:
            values += [values[0]] * 3
            values.sort()
        size = rng.choice([1, 2, 7, 32, 256])
        index = TimestampIndex(encode(values, size))
        assert index.decode() == tuple(values)
        assert index.count == len(values)
        for _ in range(10):
            lower, upper = sorted([rng.getrandbits(64) + MIN_I64 for _ in range(2)])
            expected = values[bisect_left(values, lower):bisect_left(values, upper)]
            result = index.range_values(lower, upper)
            count = index.count_range(lower, upper)
            assert result.values == tuple(expected)
            assert count.count == result.count == len(expected)
            assert count.blocks_decoded <= 2


@pytest.mark.parametrize("lower,upper", [(1, 0), (True, 1), (0, 1.1), (None, 1), (0, "1")])
def test_invalid_query_bounds(lower, upper):
    index = TimestampIndex(encode([0, 1]))
    for query in (index.range_values, index.count_range):
        with pytest.raises(ValueError):
            query(lower, upper)


@pytest.mark.parametrize("bad", [None, 100, "TIDX0001", b"bad", b"TIDX0002" + bytes(20)])
def test_invalid_index_inputs(bad):
    with pytest.raises(FormatError):
        TimestampIndex(bad)


def test_random_bytes_rejected_without_unbounded_directory_allocation():
    rng = random.Random(9)
    for _ in range(300):
        data = rng.randbytes(rng.randrange(300))
        with pytest.raises(FormatError):
            TimestampIndex(data)
    huge = struct.pack("<8sIIQ", b"TIDX0001", 1, (1 << 32) - 1, (1 << 32) - 1) + bytes(4)
    with pytest.raises(FormatError, match="truncated directory"):
        TimestampIndex(huge)


def test_no_gaps_overlaps_or_trailing_payloads():
    data = bytearray(encode([0, 1], 2))
    # Change the payload offset in the sole directory entry and repair metadata CRC.
    struct.pack_into("<Q", data, 24 + 16, 65)
    struct.pack_into("<I", data, 60, zlib.crc32(data[:60]))
    with pytest.raises(FormatError, match="noncontiguous"):
        TimestampIndex(data)
    with pytest.raises(FormatError, match="trailing"):
        TimestampIndex(encode([0, 1]) + b"extra")
