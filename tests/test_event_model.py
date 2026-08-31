from __future__ import annotations

import duckdb

from barnabus.sql import literal


def test_schema_evolution_uses_legacy_value_over_unknown_default(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    _, result = synthetic_run
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT service_code, service_code_provenance, "
            "service_code_backfilled_default, schema_generation "
            f"FROM read_parquet({literal(result.artifact_directory / 'canonical_events.parquet')}) "
            "WHERE event_id='0000000000000001'"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("A-CARD", "legacy_observed_backfilled_default", True, "legacy")


def test_semantic_dedup_ignores_retry_delivery_metadata(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    _, result = synthetic_run
    connection = duckdb.connect()
    try:
        rows = connection.execute(
            "SELECT event_id, observed_row_count, retry_row_count, retry_source_count "
            f"FROM read_parquet({literal(result.artifact_directory / 'canonical_events.parquet')}) "
            "WHERE case_id='C000000001' AND event_type='documents_received'"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [("0000000000000002", 2, 1, 2)]
    assert result.raw_rows == 6
    assert result.canonical_rows == 5


def test_workflow_state_is_ordered_by_event_time_not_arrival(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    _, result = synthetic_run
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT case_id, event_time_state, arrival_order_state, "
            "arrival_state_disagrees, arrival_inversion_count "
            f"FROM read_parquet({literal(result.artifact_directory / 'case_workflow.parquet')}) "
            "WHERE case_id='C000000001'"
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        "C000000001",
        "documents_received",
        "referral_created",
        True,
        1,
    )


def test_identifier_normalization_reaches_published_grain(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    _, result = synthetic_run
    connection = duckdb.connect()
    try:
        case_ids = [row[0] for row in connection.execute(
            "SELECT case_id "
            f"FROM read_parquet({literal(result.artifact_directory / 'case_workflow.parquet')}) "
            "ORDER BY case_id"
        ).fetchall()]
    finally:
        connection.close()
    assert case_ids == ["C000000001", "C000000002", "C000000003"]


def test_beyond_window_partition_is_characterized_not_deleted(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    _, result = synthetic_run
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT ingest_partition_status, raw_row_count, local_post_study_rows, "
            "late_in_window_rows "
            f"FROM read_parquet({literal(result.artifact_directory / 'partition_characterization.parquet')}) "
            "WHERE source_partition='2026-08'"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("post_study_ingest_partition", 1, 1, 0)


def test_nine_day_late_arrival_is_visible_as_a_revision(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    _, result = synthetic_run
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT calendar_days_late, arrived_row_count, "
            "includes_at_least_nine_day_arrival "
            f"FROM read_parquet({literal(result.artifact_directory / 'late_arrival_revisions.parquet')}) "
            "WHERE event_date_local=DATE '2026-01-01' "
            "AND revision_date_local=DATE '2026-01-10'"
        ).fetchone()
    finally:
        connection.close()
    assert row == (9, 1, True)


def test_money_is_published_as_exact_integer_cents(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    _, result = synthetic_run
    connection = duckdb.connect()
    try:
        cents = connection.execute(
            "SELECT total_cost_cents "
            f"FROM read_parquet({literal(result.artifact_directory / 'service_day.parquet')}) "
            "WHERE event_date_local=DATE '2026-01-01' AND site='A' AND service_code='A-CARD'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert cents == 1001
