import json
import subprocess
import sys

import pytest

from timecodec import TimestampIndex
from timecodec.experiment import run_experiment, write_outputs


def test_repeatability_and_work_bounds():
    result = run_experiment(n=300, queries=8, seed=22)
    assert result == run_experiment(n=300, queries=8, seed=22)
    for dataset in result["results"]:
        for config in dataset["configs"]:
            assert config["encoded_bytes"] == config["metadata_bytes"] + config["payload_bytes"]
            for workload in config["workloads"].values():
                assert workload["max_count_blocks_decoded"] <= 2
    repeated = result["results"][0]
    for config in repeated["configs"]:
        assert config["workloads"]["narrow"]["mean"]["matched_rows"] == 300
        assert config["workloads"]["broad"]["mean"]["count_blocks_decoded"] == 0


def test_artifacts_match_returned_data(tmp_path):
    result = run_experiment(n=300, queries=5, seed=18)
    write_outputs(result, tmp_path)
    assert json.loads((tmp_path / "summary.json").read_text()) == result
    assert TimestampIndex((tmp_path / "sample.tidx").read_bytes()).decode() == (-4, -4, 124, 16380)
    for name in ("storage-cost.png", "query-work.png"):
        assert (tmp_path / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert (tmp_path / name).stat().st_size > 10_000


@pytest.mark.parametrize("n,queries,seed", [(3, 1, 0), (True, 1, 0), (5, 0, 0),
                                          (5, True, 0), (5, 1, -1), (5, 1, False)])
def test_invalid_experiment_arguments(n, queries, seed):
    with pytest.raises(ValueError):
        run_experiment(n, queries, seed)


def test_cli_end_to_end(tmp_path):
    result = subprocess.run([sys.executable, "-m", "timecodec.experiment", "--rows", "8",
                             "--queries", "1", "--output", str(tmp_path)],
                            text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["rows_per_dataset"] == 8
    assert (tmp_path / "summary.json").exists()
