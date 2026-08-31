from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings, strategies as st

from barnabus.errors import InjectedFailure
from barnabus.pipeline import run_pipeline
from barnabus.pipeline import _service_day_query

from conftest import build_synthetic_data_root, event, make_runtime_paths


def _artifact_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
        if path.is_file()
    }


def _service_day_result(cents: list[int], order: list[int]) -> tuple[object, ...]:
    connection = duckdb.connect()
    try:
        connection.execute(
            """
            CREATE TABLE canonical_events (
                event_ts_local TIMESTAMP,
                site VARCHAR,
                service_code VARCHAR,
                case_id VARCHAR,
                cost_cents BIGINT,
                ingest_lag_microseconds BIGINT,
                ingest_ts_utc TIMESTAMP,
                is_workflow_event BOOLEAN
            )
            """
        )
        rows = [
            (
                datetime(2026, 1, 1, 12, 0),
                "A",
                "A-CARD",
                f"C{index:09d}",
                cents[index],
                0,
                datetime(2026, 1, 1, 12, 0),
                True,
            )
            for index in order
        ]
        connection.executemany("INSERT INTO canonical_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return connection.execute(_service_day_query()).fetchone()
    finally:
        connection.close()


@settings(max_examples=30, deadline=None)
@given(
    cents=st.lists(st.integers(min_value=0, max_value=10_000_000), min_size=1, max_size=80),
    partition_count=st.integers(min_value=1, max_value=8),
)
def test_exact_cent_aggregate_is_independent_of_partition_and_row_order(
    cents: list[int], partition_count: int
) -> None:
    natural_order = list(range(len(cents)))
    buckets = [natural_order[offset::partition_count] for offset in range(partition_count)]
    partitioned_reverse_order = [
        index for bucket in reversed(buckets) for index in reversed(bucket)
    ]
    natural = _service_day_result(cents, natural_order)
    repartitioned = _service_day_result(cents, partitioned_reverse_order)
    assert natural == repartitioned
    assert natural[5] == sum(cents)


def test_full_and_incremental_replays_are_byte_identical(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    paths, full = synthetic_run
    incremental = run_pipeline(
        paths, mode="incremental", threads=2, memory_limit="512MB"
    )
    assert incremental.artifact_set_id == full.artifact_set_id
    assert incremental.reused_artifact_set is True
    assert _artifact_bytes(incremental.artifact_directory) == _artifact_bytes(
        full.artifact_directory
    )


def test_partial_failure_keeps_current_unpublished_and_resumes_identically(tmp_path: Path) -> None:
    data_root = build_synthetic_data_root(tmp_path / "data")
    resumed_paths = make_runtime_paths(tmp_path / "resumed", data_root)
    with pytest.raises(InjectedFailure, match="durable partition checkpoints"):
        run_pipeline(
            resumed_paths,
            mode="full",
            fail_after_partitions=1,
            threads=2,
            memory_limit="512MB",
        )
    assert not (resumed_paths.output_root / "CURRENT").exists()
    assert (resumed_paths.work_root / "partition-state.json").is_file()

    resumed = run_pipeline(
        resumed_paths, mode="incremental", threads=2, memory_limit="512MB"
    )
    clean_paths = make_runtime_paths(tmp_path / "clean", data_root)
    clean = run_pipeline(clean_paths, mode="full", threads=2, memory_limit="512MB")

    assert resumed.artifact_set_id == clean.artifact_set_id
    assert _artifact_bytes(resumed.artifact_directory) == _artifact_bytes(
        clean.artifact_directory
    )


def test_arbitrary_historical_backfill_matches_a_clean_full_refresh(tmp_path: Path) -> None:
    data_root = build_synthetic_data_root(tmp_path / "data")
    incremental_paths = make_runtime_paths(tmp_path / "i", data_root)
    before = run_pipeline(
        incremental_paths, mode="full", threads=2, memory_limit="512MB"
    )

    january_path = data_root / "events" / "ingest_month=2026-01.parquet"
    existing = pq.ParquetFile(january_path).read()
    backfill = event(
        "0000000000000007",
        "4",
        "referral_created",
        "2026-01-15 08:00:00",
        "2026-01-20 08:00:00",
    )
    appended = pa.Table.from_pylist(
        [{field.name: backfill.get(field.name) for field in existing.schema}],
        schema=existing.schema,
    )
    pq.write_table(pa.concat_tables([existing, appended]), january_path)

    after = run_pipeline(
        incremental_paths, mode="incremental", threads=2, memory_limit="512MB"
    )
    clean_paths = make_runtime_paths(tmp_path / "c", data_root)
    clean = run_pipeline(clean_paths, mode="full", threads=2, memory_limit="512MB")

    assert after.artifact_set_id != before.artifact_set_id
    assert after.artifact_set_id == clean.artifact_set_id
    assert after.raw_rows == before.raw_rows + 1
    assert _artifact_bytes(after.artifact_directory) == _artifact_bytes(
        clean.artifact_directory
    )
