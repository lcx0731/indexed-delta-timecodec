# TIDX0001 binary format

This is a project-specific educational format. All fixed-width integers are
little-endian. There is no padding, compression of the directory, or optional
extension area. Unknown magic/version bytes are rejected.

## Header: 24 bytes

| Offset | Bytes | Type | Field |
|---:|---:|---|---|
| 0 | 8 | ASCII bytes | Exactly `TIDX0001` |
| 8 | 4 | uint32 | Maximum rows per block `B`, from 1 through 65,536 |
| 12 | 4 | uint32 | Block count `K` |
| 16 | 8 | uint64 | Total row count `n` |

Require `K = ceil(n/B)`. Zero rows have zero blocks.

## Directory: K entries of 36 bytes

Entry `j` starts at `24 + 36*j`:

| Relative offset | Bytes | Type | Field |
|---:|---:|---|---|
| 0 | 8 | int64 | First timestamp |
| 8 | 8 | int64 | Last timestamp |
| 16 | 8 | uint64 | Absolute payload offset |
| 24 | 4 | uint32 | Payload length in bytes |
| 28 | 4 | uint32 | Row count `m` |
| 32 | 4 | uint32 | CRC32 of this block's payload |

Every block except the last has exactly `B` rows; the last has the remainder.
Block checkpoints satisfy `previous.last <= current.first <= current.last`.
Duplicate timestamps are valid and retain their multiplicity.

The directory is followed by a 4-byte uint32 CRC32 of **all preceding header
and directory bytes**. Payload data therefore starts at `28 + 36*K`.

Payload offsets are absolute and contiguous, in directory order. Zero-byte
singleton payloads may share an offset. No gaps, overlapping nonempty payloads,
out-of-bounds offsets, or trailing bytes are accepted.

## Payload: m−1 unsigned deltas

The first timestamp is already in the directory. For each subsequent row,
store its difference from the preceding timestamp as a base-128 varint:

- Seven low bits per byte are data, least-significant group first.
- Bit 7 is one if another byte follows; zero terminates the integer.
- Zero is one byte `00`. More than one byte ending in a zero data group is
  noncanonical and rejected.
- Values range from 0 to `2**64−1`, requiring at most ten bytes. A tenth byte
  must be `01`; any larger value or further continuation is invalid.

Differences are unsigned even though timestamps are signed. A jump from
`−2**63` to `2**63−1` is legal and uses the maximum uint64 delta. Reconstructed
timestamps must remain in int64. After exactly `m−1` deltas, the payload must be
exhausted and the final value must equal the directory's last checkpoint.

At open time, payload length is constrained to `[m−1, 10*(m−1)]`. A singleton
has identical endpoints, zero payload bytes, and payload CRC32 zero. The later
payload decoder applies the stronger canonical/count/endpoint checks.

## Independent worked fixture

With `B=4`, values `[-4, -4, 124, 16380]` have deltas `[0, 128, 16256]`:

```text
payload = 00 80 01 80 7f
header  = 24 bytes
entry   = 36 bytes: first=-4, last=16380, offset=64, length=5, count=4, payload CRC
meta CRC= 4 bytes
total   = 69 bytes
```

`tests/test_codec.py` independently assembles this fixture with `struct` and
compares every byte with the encoder. `results/sample.tidx` is generated from
the same four values and can be decoded by the documented API.

## Validation contract and security

Opening checks the header and directory, including their checksum. Decoding a
block checks its payload checksum and complete structure. An indexed query may
never inspect noncandidate blocks; a count query also skips fully covered
blocks. Only a full validation/decode traverses every payload.

CRC32 is a noncryptographic accidental-corruption check, not authentication.
Checksums can be recomputed and can collide. The format has no authenticity,
confidentiality, origin, or replay-protection claim. A successful structural
validation also cannot establish that timestamps correspond to real events.
