from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from barnabus.errors import ContractViolation
from barnabus.pipeline import run_pipeline
from barnabus.sql import literal

from conftest import build_synthetic_data_root, event, make_runtime_paths


def _rewrite_rows(path: Path, rows: list[dict[str, object]], schema: pa.Schema) -> None:
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def _mutate_row(path: Path, row_index: int, **changes: object) -> None:
    table = pq.ParquetFile(path).read()
    rows = table.to_pylist()
    rows[row_index].update(changes)
    _rewrite_rows(path, rows, table.schema)


def _append_row(path: Path, row: dict[str, object]) -> None:
    table = pq.ParquetFile(path).read()
    projected = {field.name: row.get(field.name) for field in table.schema}
    _rewrite_rows(path, [*table.to_pylist(), projected], table.schema)


def _cached_rejection_reasons(work_root: Path) -> set[str]:
    cache_paths = sorted((work_root / "normalized-partitions").glob("*.parquet"))
    assert cache_paths, "a fatal partition must retain its normalized diagnostic cache"
    connection = duckdb.connect()
    try:
        paths = ", ".join(literal(path) for path in cache_paths)
        relation = (
            f"read_parquet({paths}, union_by_name=true)"
            if len(cache_paths) == 1
            else f"read_parquet([{paths}], union_by_name=true)"
        )
        return {
            row[0]
            for row in connection.execute(
                f"SELECT DISTINCT rejection_reason FROM {relation} "
                "WHERE rejection_reason IS NOT NULL"
            ).fetchall()
        }
    finally:
        connection.close()


def test_conflicting_snapshot_rows_aggregate_without_multiplying_reconciliation(
    tmp_path: Path,
) -> None:
    data_root = build_synthetic_data_root(tmp_path / "data")
    with (data_root / "snapshot_cases.csv").open(
        "a", newline="", encoding="utf-8"
    ) as handle:
        csv.writer(handle, lineterminator="\n").writerow(
            ["A", "A-CARD", "2026-01-02", "40", "false", "", "1"]
        )
    result = run_pipeline(
        make_runtime_paths(tmp_path / "r", data_root),
        mode="full",
        threads=2,
        memory_limit="512MB",
    )

    connection = duckdb.connect()
    try:
        summary = connection.execute(
            "SELECT snapshot_row_count, snapshot_distinct_payload_count, "
            "snapshot_referral_date_count "
            f"FROM read_parquet({literal(result.artifact_directory / 'snapshot_case_summary.parquet')}) "
            "WHERE case_id='C000000001'"
        ).fetchone()
        reconciliation = connection.execute(
            "SELECT count(*), min(reconciliation_status) "
            f"FROM read_parquet({literal(result.artifact_directory / 'reconciliation.parquet')}) "
            "WHERE case_id='C000000001'"
        ).fetchone()
    finally:
        connection.close()
    assert summary == (2, 2, 2)
    assert reconciliation == (1, "matched_snapshot_conflict")


def test_operational_event_is_retained_but_excluded_from_workflow_state(
    tmp_path: Path,
) -> None:
    data_root = build_synthetic_data_root(tmp_path / "data")
    january = data_root / "events" / "ingest_month=2026-01.parquet"
    _append_row(
        january,
        event(
            "0000000000000007",
            "1",
            "heartbeat",
            "2026-01-02 12:00:00",
            "2026-01-02 12:01:00",
        ),
    )
    result = run_pipeline(
        make_runtime_paths(tmp_path / "r", data_root),
        mode="full",
        threads=2,
        memory_limit="512MB",
    )

    connection = duckdb.connect()
    try:
        operational = connection.execute(
            "SELECT is_workflow_event "
            f"FROM read_parquet({literal(result.artifact_directory / 'canonical_events.parquet')}) "
            "WHERE event_id='0000000000000007'"
        ).fetchone()
        workflow = connection.execute(
            "SELECT event_time_state, workflow_event_count "
            f"FROM read_parquet({literal(result.artifact_directory / 'case_workflow.parquet')}) "
            "WHERE case_id='C000000001'"
        ).fetchone()
    finally:
        connection.close()
    assert operational == (False,)
    assert workflow == ("documents_received", 2)


def test_conflicting_real_service_codes_are_a_fatal_rejection(tmp_path: Path) -> None:
    data_root = build_synthetic_data_root(tmp_path / "data")
    january = data_root / "events" / "ingest_month=2026-01.parquet"
    _mutate_row(january, 0, svc_code="A-CARD", service_code="A-ENT")
    paths = make_runtime_paths(tmp_path / "r", data_root)

    with pytest.raises(ContractViolation, match="partition 2026-01 contains 1 fatal"):
        run_pipeline(paths, mode="full", threads=2, memory_limit="512MB")
    assert not (paths.output_root / "CURRENT").exists()
    assert "conflicting_service_codes" in _cached_rejection_reasons(paths.work_root)


@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    [
        ({"event_id": "malformed"}, "malformed_event_id"),
        ({"clinician_id": "MD9999"}, "unmatched_clinician_id"),
    ],
    ids=["malformed-event-id", "unmatched-clinician"],
)
def test_malformed_or_unmatched_key_fails_with_diagnostic_cache(
    tmp_path: Path, changes: dict[str, object], expected_reason: str
) -> None:
    data_root = build_synthetic_data_root(tmp_path / "data")
    january = data_root / "events" / "ingest_month=2026-01.parquet"
    _mutate_row(january, 0, **changes)
    paths = make_runtime_paths(tmp_path / "r", data_root)

    with pytest.raises(ContractViolation, match="partition 2026-01 contains 1 fatal"):
        run_pipeline(paths, mode="full", threads=2, memory_limit="512MB")
    assert not (paths.output_root / "CURRENT").exists()
    assert expected_reason in _cached_rejection_reasons(paths.work_root)


def test_unauthorized_cross_service_event_is_retained_and_flagged(tmp_path: Path) -> None:
    data_root = build_synthetic_data_root(tmp_path / "data")
    january = data_root / "events" / "ingest_month=2026-01.parquet"
    _append_row(
        january,
        event(
            "0000000000000007",
            "4",
            "referral_created",
            "2026-01-15 08:00:00",
            "2026-01-15 08:05:00",
            svc_code="A-ENT",
            service_code="A-ENT",
        ),
    )
    result = run_pipeline(
        make_runtime_paths(tmp_path / "r", data_root),
        mode="full",
        threads=2,
        memory_limit="512MB",
    )

    connection = duckdb.connect()
    try:
        canonical = connection.execute(
            "SELECT cross_service_event, clinician_coverage_anomaly "
            f"FROM read_parquet({literal(result.artifact_directory / 'canonical_events.parquet')}) "
            "WHERE event_id='0000000000000007'"
        ).fetchone()
        workflow = connection.execute(
            "SELECT has_cross_service_event, has_coverage_anomaly "
            f"FROM read_parquet({literal(result.artifact_directory / 'case_workflow.parquet')}) "
            "WHERE case_id='C000000004'"
        ).fetchone()
    finally:
        connection.close()
    assert canonical == (True, True)
    assert workflow == (True, True)


def test_ingest_timestamp_month_must_match_its_partition(tmp_path: Path) -> None:
    data_root = build_synthetic_data_root(tmp_path / "data")
    august = data_root / "events" / "ingest_month=2026-08.parquet"
    _mutate_row(
        august,
        0,
        event_ts="2026-08-31 23:55:00",
        ingest_ts=datetime(2026, 9, 1, 0, 5),
    )
    paths = make_runtime_paths(tmp_path / "r", data_root)

    with pytest.raises(ContractViolation, match="partition 2026-08 contains 1 fatal"):
        run_pipeline(paths, mode="full", threads=2, memory_limit="512MB")
    assert not (paths.output_root / "CURRENT").exists()
    assert "ingest_partition_mismatch" in _cached_rejection_reasons(paths.work_root)
