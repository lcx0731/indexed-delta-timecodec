"""Deterministic size/query-work experiment; no wall-clock speed claims."""

import argparse
from bisect import bisect_left
import hashlib
import json
from pathlib import Path
import platform
import random
import statistics
import struct
import zlib

from .codec import MIN_I64, MAX_I64, TimestampIndex, encode


BLOCK_SIZES = (64, 256, 1024)


def make_series(n, seed):
    """Four artificial nondecreasing tick streams, with no calendar semantics."""
    rng = random.Random(seed)
    small, bursty = [-10_000], [-10_000]
    for i in range(1, n):
        small.append(small[-1] + rng.choice((0, 1, 1, 2, 3)))
        bursty.append(bursty[-1] + ((1 << 20) if i % 128 == 0 else rng.randrange(4)))
    return {
        "repeated_ticks": [17] * n,
        "small_gaps": small,
        "bursty_gaps": bursty,
        "wide_gaps": sorted(rng.getrandbits(64) + MIN_I64 for _ in range(n)),
    }


def _queries(values, width, count, seed):
    rng = random.Random(seed)
    intervals = []
    for _ in range(count):
        start = rng.randrange(len(values) - width + 1)
        # Duplicates at either boundary may include more than 'width' rows.
        intervals.append((values[start], values[start + width - 1] + 1))
    return intervals


def _workload(index, values, intervals):
    rows = []
    for lower, upper in intervals:
        expected = tuple(values[bisect_left(values, lower):bisect_left(values, upper)])
        materialized = index.range_values(lower, upper)
        counted = index.count_range(lower, upper)
        if materialized.values != expected or counted.count != len(expected):
            raise ArithmeticError("range result differs from uncompressed bisect reference")
        if counted.blocks_decoded > 2:
            raise ArithmeticError("count query decoded more than two boundary blocks")
        rows.append({
            "matched_rows": counted.count,
            "candidate_blocks": counted.candidate_blocks,
            "materialize_blocks_decoded": materialized.blocks_decoded,
            "materialize_payload_bytes_verified": materialized.payload_bytes_verified,
            "count_blocks_decoded": counted.blocks_decoded,
            "count_payload_bytes_verified": counted.payload_bytes_verified,
        })
    return {
        "queries": len(intervals),
        "mean": {key: statistics.mean(row[key] for row in rows) for key in rows[0]},
        "max_count_blocks_decoded": max(row["count_blocks_decoded"] for row in rows),
    }


def run_experiment(n=20_000, queries=100, seed=20261021):
    if type(n) is not int or n < 4:
        raise ValueError("n must be an integer >= 4")
    if type(queries) is not int or queries < 1:
        raise ValueError("queries must be a positive integer")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    results = []
    for dataset_id, (name, values) in enumerate(make_series(n, seed).items()):
        raw = struct.pack(f"<{n}q", *values)
        compressed = zlib.compress(raw, level=6)
        if struct.unpack(f"<{n}q", zlib.decompress(compressed)) != tuple(values):
            raise ArithmeticError("zlib baseline failed roundtrip")
        query_sets = {
            "narrow": _queries(values, min(100, n), queries, seed + 100 + dataset_id),
            "broad": _queries(values, n // 2, queries, seed + 200 + dataset_id),
        }
        configs = []
        for size in BLOCK_SIZES:
            encoded = encode(values, size)
            index = TimestampIndex(encoded)
            if index.decode() != tuple(values):
                raise ArithmeticError("codec failed complete roundtrip")
            configs.append({
                "block_size": size,
                "encoded_bytes": len(encoded),
                "metadata_bytes": index.metadata_bytes,
                "payload_bytes": len(encoded) - index.metadata_bytes,
                "encoded_sha256": hashlib.sha256(encoded).hexdigest(),
                "workloads": {key: _workload(index, values, intervals)
                              for key, intervals in query_sets.items()},
            })
        results.append({
            "dataset": name, "rows": n, "raw_int64_bytes": len(raw),
            "monolithic_zlib_level6_bytes": len(compressed), "configs": configs,
        })
    examples = []
    for name, values in (("empty", []), ("singleton", [0]),
                          ("full_int64_span", [MIN_I64, MAX_I64])):
        encoded = encode(values, 256)
        if TimestampIndex(encoded).decode() != tuple(values):
            raise ArithmeticError("edge case roundtrip failed")
        examples.append({"name": name, "values": values, "raw_int64_bytes": 8 * len(values),
                         "encoded_bytes": len(encoded)})
    return {
        "data": "Seeded synthetic sorted integer ticks; no external or personal data.",
        "seed": seed, "python": platform.python_version(), "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
        "rows_per_dataset": n, "queries_per_workload": queries,
        "work_definition": "Payload bytes CRC-checked/decoded after metadata is already parsed; not disk I/O or time.",
        "results": results, "edge_cases": examples,
    }


def write_outputs(result, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n",
                                         encoding="utf-8")
    sample = [-4, -4, 124, 16380]
    (output / "sample.tidx").write_bytes(encode(sample, 4))

    data = result["results"]
    fig, ax = plt.subplots(figsize=(10.5, 5.2), layout="constrained")
    colors = ("#8db1c4", "#376b8a", "#1d4057", "#bf7044")
    for j in range(4):
        sizes = [d["configs"][j]["encoded_bytes"] if j < 3 else d["monolithic_zlib_level6_bytes"]
                 for d in data]
        ax.bar([i + (j - 1.5) * .19 for i in range(len(data))],
               [s / d["rows"] for s, d in zip(sizes, data)], .18, color=colors[j],
               label=f"Indexed delta, B={BLOCK_SIZES[j]}" if j < 3 else "Monolithic zlib (level 6)")
    ax.axhline(8, color="#333333", linestyle="--", linewidth=1, label="Raw int64: 8 bytes/row")
    ax.set(xticks=range(len(data)), xticklabels=[d["dataset"].replace("_", "\n") for d in data],
           ylabel="Total encoded bytes / row", title="Storage cost includes the complete index and checksums")
    largest = max([8.0] + [c["encoded_bytes"] / d["rows"] for d in data for c in d["configs"]]
                  + [d["monolithic_zlib_level6_bytes"] / d["rows"] for d in data])
    ax.set_ylim(0, largest * 1.3)
    ax.legend(ncols=2, fontsize=9, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)
    fig.savefig(output / "storage-cost.png", dpi=170)
    plt.close(fig)

    fig, axes = plt.subplots(2, 2, figsize=(11, 7), layout="constrained")
    for ax, dataset in zip(axes.flat, data):
        for workload, key, label, color, marker in (
            ("narrow", "materialize_payload_bytes_verified", "Narrow: return values", "#7fa7be", "o"),
            ("broad", "materialize_payload_bytes_verified", "Broad: return values", "#376b8a", "s"),
            ("broad", "count_payload_bytes_verified", "Broad: count only", "#bf7044", "D"),
        ):
            ax.plot(BLOCK_SIZES, [c["workloads"][workload]["mean"][key] for c in dataset["configs"]],
                    color=color, marker=marker, label=label)
        ax.set(xscale="log", yscale="symlog", xlabel="Rows per block B", ylabel="Mean payload bytes verified",
               title=dataset["dataset"].replace("_", " "), xticks=BLOCK_SIZES,
               xticklabels=[str(b) for b in BLOCK_SIZES])
        ax.set_ylim(bottom=0)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=.2)
    axes[0, 0].legend(fontsize=8, loc="center left")
    fig.suptitle("Post-open query work; zero means no payload bytes checked, not full validation\n"
                 "Narrow targets 100 rows, broad targets half the series; ties can expand both", fontsize=11)
    fig.savefig(output / "query-work.png", dpi=170)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("results"))
    parser.add_argument("--rows", type=int, default=20_000)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args()
    try:
        result = run_experiment(args.rows, args.queries, args.seed)
    except ValueError as exc:
        parser.error(str(exc))
    write_outputs(result, args.output)
    print(json.dumps({"output": str(args.output), "datasets": len(result["results"]),
                      "rows_per_dataset": args.rows, "queries_per_workload": args.queries}, indent=2))


if __name__ == "__main__":
    main()
