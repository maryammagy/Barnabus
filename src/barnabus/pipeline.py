"""Command-line orchestration for the deterministic event-log pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import psutil
import pyarrow as pa
import pyarrow.parquet as pq

from barnabus import __version__
from barnabus.config import PipelineConfig, RuntimePaths, environment_default, load_config
from barnabus.contracts import (
    ContractResult,
    assert_many_to_one_join,
    assert_one_to_one_join,
    assert_unique,
    require,
    scalar,
)
from barnabus.errors import ContractViolation, InjectedFailure, PipelineError
from barnabus.perf import MetricsRecorder
from barnabus.sql import case_id_expression, copy_query, literal, parquet_relation, string_list
from barnabus.sql import normalized_events_query
from barnabus.timeutils import zone_valid_offsets


PARTITION_RE = re.compile(r"^ingest_month=(\d{4}-\d{2})\.parquet$")
STATE_VERSION = 1


@dataclass(frozen=True)
class InputFile:
    relative_path: str
    absolute_path: Path
    sha256: str
    size_bytes: int
    parquet_rows: int | None
    partition: str | None


@dataclass(frozen=True)
class RunResult:
    artifact_set_id: str
    artifact_directory: Path
    data_version: str
    raw_rows: int
    canonical_rows: int
    quarantine_rows: int
    telemetry_path: Path
    reused_artifact_set: bool


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_json(path: Path, default: object) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path, chunk_bytes: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_payload(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _package_code_hash() -> str:
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package_root.glob("*.py"), key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _normalizer_code_hash() -> str:
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in ("config.py", "sql.py", "timeutils.py"):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((package_root / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _event_schema(path: Path) -> tuple[set[str], int]:
    parquet_file = pq.ParquetFile(path)
    schema = parquet_file.schema_arrow
    columns = set(schema.names)
    expected_strings = {
        "case_id",
        "event_type",
        "site",
        "clinician_id",
        "ward_id",
        "source_system",
        "event_ts",
        "event_id",
    }
    if not expected_strings.issubset(columns):
        missing = sorted(expected_strings - columns)
        raise ContractViolation(f"{path.name} is missing required string columns: {missing}")
    for name in expected_strings | ({"service_code", "svc_code"} & columns):
        if not (pa.types.is_string(schema.field(name).type) or pa.types.is_large_string(schema.field(name).type)):
            raise ContractViolation(f"{path.name}:{name} must be a string column")
    if "ingest_ts" not in columns or not pa.types.is_timestamp(schema.field("ingest_ts").type):
        raise ContractViolation(f"{path.name}:ingest_ts must be a timestamp")
    for name in ("cost_cad", "tz_offset_hours"):
        if name not in columns or not (
            pa.types.is_floating(schema.field(name).type)
            or pa.types.is_integer(schema.field(name).type)
        ):
            raise ContractViolation(f"{path.name}:{name} must be numeric")
    if not ({"service_code", "svc_code"} & columns):
        raise ContractViolation(f"{path.name} has neither service-code generation")
    return columns, int(parquet_file.metadata.num_rows)


def _partition_from_path(path: Path) -> str:
    match = PARTITION_RE.fullmatch(path.name)
    if match is None:
        raise ContractViolation(f"event file does not follow ingest_month=YYYY-MM.parquet: {path.name}")
    return match.group(1)


def _configure_connection(
    connection: duckdb.DuckDBPyConnection,
    work_root: Path,
    threads: int,
    memory_limit: str,
    run_token: str,
) -> None:
    if not 1 <= threads <= 8:
        raise PipelineError("BARNABUS_DUCKDB_THREADS must be between 1 and 8")
    if not re.fullmatch(r"[1-9][0-9]*(MB|GB)", memory_limit.upper()):
        raise PipelineError("BARNABUS_DUCKDB_MEMORY_LIMIT must look like 512MB or 12GB")
    spill = work_root / "spill" / run_token
    spill.mkdir(parents=True, exist_ok=True)
    connection.execute(f"SET threads={threads}")
    connection.execute(f"SET memory_limit={literal(memory_limit.upper())}")
    connection.execute(f"SET temp_directory={literal(spill)}")
    connection.execute("SET preserve_insertion_order=false")
    connection.execute("PRAGMA disable_progress_bar")


def _prepare_timezone_rules(
    connection: duckdb.DuckDBPyConnection,
    raw_path: Path,
    config: PipelineConfig,
) -> None:
    hours = connection.execute(
        f"""
        SELECT DISTINCT trim(CAST(source_system AS VARCHAR)) AS source_system,
               date_trunc('hour', local_ts) AS local_hour
        FROM (
            SELECT source_system, try_cast(event_ts AS TIMESTAMP_NS) AS local_ts
            FROM read_parquet({literal(raw_path)}, union_by_name=true)
            UNION ALL
            SELECT source_system, CAST(ingest_ts AS TIMESTAMP_NS) AS local_ts
            FROM read_parquet({literal(raw_path)}, union_by_name=true)
        )
        WHERE local_ts IS NOT NULL
        ORDER BY 1, 2
        """
    ).fetchall()
    rows: list[tuple[str, datetime, float | None, float | None, int]] = []
    for source, local_hour in hours:
        source_config = config.event_time.sources.get(str(source))
        if source_config is None:
            continue
        offsets = sorted(
            zone_valid_offsets(
                local_hour + timedelta(minutes=30),
                source_config.inferred_named_zone_for_audit,
            )
        )
        first = offsets[0] if offsets else None
        second = offsets[1] if len(offsets) > 1 else None
        rows.append((str(source), local_hour, first, second, len(offsets)))
    connection.execute(
        """
        CREATE OR REPLACE TEMP TABLE timezone_rules (
            source_system VARCHAR NOT NULL,
            local_hour TIMESTAMP_NS NOT NULL,
            valid_offset_1 DOUBLE,
            valid_offset_2 DOUBLE,
            valid_count INTEGER NOT NULL,
            PRIMARY KEY (source_system, local_hour)
        )
        """
    )
    if rows:
        connection.executemany("INSERT INTO timezone_rules VALUES (?, ?, ?, ?, ?)", rows)


def _inventory_inputs(paths: RuntimePaths, metrics: MetricsRecorder) -> list[InputFile]:
    event_paths = sorted(paths.data_root.glob("events/ingest_month=*.parquet"), key=lambda p: p.name)
    if not event_paths:
        raise ContractViolation("no event partitions were found")
    inputs: list[InputFile] = []
    with metrics.step("inventory_and_content_hash_inputs"):
        for path in event_paths:
            _, row_count = _event_schema(path)
            inputs.append(
                InputFile(
                    relative_path=path.relative_to(paths.data_root).as_posix(),
                    absolute_path=path,
                    sha256=_sha256_file(path),
                    size_bytes=path.stat().st_size,
                    parquet_rows=row_count,
                    partition=_partition_from_path(path),
                )
            )
        for filename in ("clinicians.csv", "snapshot_cases.csv"):
            path = paths.data_root / filename
            if not path.is_file():
                raise ContractViolation(f"required input is missing: {filename}")
            inputs.append(
                InputFile(
                    relative_path=filename,
                    absolute_path=path,
                    sha256=_sha256_file(path),
                    size_bytes=path.stat().st_size,
                    parquet_rows=None,
                    partition=None,
                )
            )
    return inputs


def _validate_clinicians(connection: duckdb.DuckDBPyConnection, path: Path) -> list[ContractResult]:
    connection.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW clinicians AS
        SELECT upper(trim(clinician_id)) clinician_id,
               upper(trim(site)) site,
               upper(trim(home_service)) home_service,
               upper(trim(ward_id)) ward_id,
               lower(trim(covers_other_services)) = 'true' covers_other_services
        FROM read_csv_auto({literal(path)}, all_varchar=true, header=true)
        """
    )
    results = [assert_unique(connection, "clinicians", ["clinician_id"], "clinicians")]
    invalid = scalar(
        connection,
        """
        SELECT count(*) FROM clinicians
        WHERE NOT regexp_full_match(clinician_id, 'MD[0-9]{4}')
           OR site NOT IN ('A', 'B')
           OR NOT regexp_full_match(home_service,
             '[AB]-(CARD|ENT|GEN|GYN|NEURO|OPHT|ORTH|PAED|PLAS|THOR|URO|VASC)')
           OR NOT regexp_full_match(ward_id, '[AB]-W[1-4]')
           OR left(home_service, 1) <> site OR left(ward_id, 1) <> site
        """,
    )
    results.append(require(invalid == 0, "clinicians.types_ranges", invalid, 0))
    return results


def _normalize_partition(
    source: InputFile,
    destination: Path,
    paths: RuntimePaths,
    config: PipelineConfig,
    threads: int,
    memory_limit: str,
    run_token: str,
) -> tuple[int, int, int]:
    columns, footer_rows = _event_schema(source.absolute_path)
    connection = duckdb.connect()
    try:
        _configure_connection(connection, paths.work_root, threads, memory_limit, run_token)
        _prepare_timezone_rules(connection, source.absolute_path, config)
        query = normalized_events_query(
            source.absolute_path,
            source.relative_path,
            source.partition or "",
            paths.data_root / "clinicians.csv",
            columns,
            config,
        )
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        connection.execute(copy_query(query, temporary, "event_id, raw_row_hash"))
        normalized = f"read_parquet({literal(temporary)})"
        observed = scalar(connection, f"SELECT count(*) FROM {normalized}")
        require(observed == footer_rows, f"{source.partition}.row_conservation", observed, footer_rows)
        assert_unique(connection, normalized, ["event_id"], f"{source.partition}.event_id")
        contained = string_list(config.contracts.contained_rejection_reasons)
        rejected, fatal = connection.execute(
            f"SELECT count(*) FILTER (WHERE rejection_reason IS NOT NULL), "
            f"count(*) FILTER (WHERE rejection_reason IS NOT NULL "
            f"AND rejection_reason NOT IN ({contained})) FROM {normalized}"
        ).fetchone()
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)
        return observed, int(rejected), int(fatal)
    finally:
        connection.close()


def _canonical_query() -> str:
    return """
    WITH accepted AS (
        SELECT * FROM normalized_events WHERE rejection_reason IS NULL
    ), ranked AS (
        SELECT *, row_number() OVER (
            PARTITION BY semantic_event_key
            ORDER BY ingest_ts_utc, event_id, raw_source_file, raw_row_hash
        ) AS semantic_rank
        FROM accepted
    ), retry_stats AS (
        SELECT semantic_event_key,
               count(*) AS observed_row_count,
               min(ingest_ts_utc) AS first_ingest_ts_utc,
               max(ingest_ts_utc) AS last_ingest_ts_utc,
               count(DISTINCT source_system) AS retry_source_count
        FROM accepted GROUP BY semantic_event_key
    )
    SELECT
        r.semantic_event_key, r.event_id, r.case_id, r.event_type,
        r.event_ts_local, r.ingest_ts_local, r.event_ts_utc, r.ingest_ts_utc,
        r.source_system, r.supplied_tz_offset_hours,
        r.event_offset_hours, r.ingest_offset_hours,
        r.event_timezone_resolution, r.ingest_timezone_resolution,
        r.site, r.clinician_id, r.ward_id, r.service_code,
        r.service_code_provenance, r.service_code_backfilled_default,
        r.schema_generation, r.cost_cents, r.cost_cad,
        r.is_workflow_event, r.cross_service_event, r.clinician_coverage_anomaly,
        r.local_study_status, r.utc_study_status, r.ingest_lag_microseconds,
        r.source_partition, r.raw_source_file,
        s.observed_row_count, s.observed_row_count - 1 AS retry_row_count,
        s.first_ingest_ts_utc, s.last_ingest_ts_utc, s.retry_source_count
    FROM ranked r JOIN retry_stats s USING (semantic_event_key)
    WHERE r.semantic_rank = 1
    """


def _case_workflow_query(config: PipelineConfig) -> str:
    start = literal(config.study_window.start)
    end = literal(config.study_window.end)
    return f"""
    WITH ranked AS (
        SELECT *, CASE event_type
            WHEN 'referral_created' THEN 1 WHEN 'documents_received' THEN 2
            WHEN 'assessment_generated' THEN 3 WHEN 'recommendation_issued' THEN 4
            WHEN 'clinician_review_opened' THEN 5 WHEN 'clinician_action_recorded' THEN 6
            WHEN 'readiness_marked' THEN 7 WHEN 'surgery_scheduled' THEN 8
            WHEN 'surgery_completed' THEN 9 WHEN 'case_closed' THEN 10
            ELSE 0 END AS workflow_rank
        FROM canonical_events WHERE is_workflow_event
    ), ordered AS (
        SELECT *, lag(event_ts_utc) OVER (
            PARTITION BY case_id ORDER BY ingest_ts_utc, event_id
        ) AS prior_event_ts_by_arrival
        FROM ranked
    ), cases AS (
        SELECT
            case_id,
            first(site ORDER BY event_ts_utc, event_id)
                FILTER (WHERE event_type='referral_created') AS site,
            first(service_code ORDER BY event_ts_utc, event_id)
                FILTER (WHERE event_type='referral_created') AS service_code,
            first(clinician_id ORDER BY event_ts_utc, event_id)
                FILTER (WHERE event_type='referral_created') AS referral_clinician_id,
            first(ward_id ORDER BY event_ts_utc, event_id)
                FILTER (WHERE event_type='referral_created') AS referral_ward_id,
            min(event_ts_local) FILTER (WHERE event_type='referral_created') AS referral_ts_local,
            min(event_ts_utc) FILTER (WHERE event_type='referral_created') AS referral_ts_utc,
            min(event_ts_utc) FILTER (WHERE event_type='documents_received') AS documents_ts_utc,
            min(event_ts_utc) FILTER (WHERE event_type='assessment_generated') AS assessment_ts_utc,
            min(event_ts_utc) FILTER (WHERE event_type='recommendation_issued') AS recommendation_ts_utc,
            min(event_ts_utc) FILTER (WHERE event_type='clinician_review_opened') AS review_ts_utc,
            min(event_ts_utc) FILTER (WHERE event_type='clinician_action_recorded') AS action_ts_utc,
            min(event_ts_utc) FILTER (WHERE event_type='readiness_marked') AS readiness_ts_utc,
            min(event_ts_utc) FILTER (WHERE event_type='surgery_scheduled') AS scheduled_ts_utc,
            min(event_ts_utc) FILTER (WHERE event_type='surgery_completed') AS completed_ts_utc,
            min(event_ts_utc) FILTER (WHERE event_type='case_closed') AS closed_ts_utc,
            first(event_type ORDER BY event_ts_utc DESC, workflow_rank DESC, event_id DESC)
                AS event_time_state,
            first(event_type ORDER BY ingest_ts_utc DESC, event_id DESC) AS arrival_order_state,
            count(*) AS workflow_event_count,
            count(*) FILTER (WHERE event_ts_utc < prior_event_ts_by_arrival) AS arrival_inversion_count,
            sum(coalesce(cost_cents, 0))::HUGEINT AS total_cost_cents,
            bool_or(cross_service_event) AS has_cross_service_event,
            bool_or(clinician_coverage_anomaly) AS has_coverage_anomaly,
            sum(retry_row_count)::HUGEINT AS retry_rows_removed
        FROM ordered GROUP BY case_id
    )
    SELECT *,
        event_time_state <> arrival_order_state AS arrival_state_disagrees,
        completed_ts_utc IS NULL AND closed_ts_utc IS NOT NULL AS cancellation_derived,
        CASE WHEN referral_ts_utc IS NULL OR readiness_ts_utc IS NULL THEN NULL
             ELSE date_diff('seconds', referral_ts_utc, readiness_ts_utc) END
             AS readiness_elapsed_seconds,
        CASE WHEN referral_ts_local IS NULL THEN 'missing_referral'
             WHEN cast(referral_ts_local AS DATE) < cast({start} AS DATE) THEN 'pre_study'
             WHEN cast(referral_ts_local AS DATE) > cast({end} AS DATE) THEN 'post_study'
             ELSE 'in_study' END AS referral_study_status
    FROM cases
    """


def _service_day_query() -> str:
    return """
    SELECT
        cast(event_ts_local AS DATE) AS event_date_local,
        site, service_code,
        count(*) AS workflow_event_count,
        count(DISTINCT case_id) AS case_count,
        sum(coalesce(cost_cents, 0))::HUGEINT AS total_cost_cents,
        count(*) FILTER (WHERE ingest_lag_microseconds >= 777600000000::BIGINT)
            AS events_at_least_nine_days_late,
        min(ingest_ts_utc) AS first_observed_utc,
        max(ingest_ts_utc) AS last_observed_utc
    FROM canonical_events
    WHERE is_workflow_event
    GROUP BY 1, 2, 3
    """


def _snapshot_query(snapshot_path: Path) -> str:
    normalized_case = case_id_expression("case_ref")
    return f"""
    WITH raw AS (
        SELECT *, {normalized_case} AS case_id,
               try_cast(referral_ts AS DATE) AS referral_date,
               sha256(concat_ws('|', site, service_code, referral_ts,
                     patient_age, cancelled, coalesce(readiness_days, '<NULL>'), case_ref))
                 AS snapshot_row_hash
        FROM read_csv_auto({literal(snapshot_path)}, all_varchar=true, header=true)
    )
    SELECT
        case_id,
        count(*) AS snapshot_row_count,
        count(DISTINCT snapshot_row_hash) AS snapshot_distinct_payload_count,
        count(DISTINCT referral_date) AS snapshot_referral_date_count,
        list_sort(list_distinct(list(referral_date))) AS snapshot_referral_dates,
        min(referral_date) AS snapshot_referral_date_min,
        max(referral_date) AS snapshot_referral_date_max,
        count(DISTINCT upper(trim(site))) AS snapshot_site_count,
        min(upper(trim(site))) AS snapshot_site,
        count(DISTINCT upper(trim(service_code))) AS snapshot_service_count,
        min(upper(trim(service_code))) AS snapshot_service_code,
        count(DISTINCT lower(trim(cancelled))) AS snapshot_cancelled_value_count,
        count(DISTINCT nullif(trim(readiness_days), '')) AS snapshot_readiness_value_count
    FROM raw GROUP BY case_id
    """


def _validate_snapshot(connection: duckdb.DuckDBPyConnection, path: Path, config: PipelineConfig) -> list[ContractResult]:
    normalized_case = case_id_expression("case_ref")
    service_pattern = literal(config.schema_.service_code_pattern)
    allowed_sites = string_list(config.contracts.allowed_sites)
    invalid = scalar(
        connection,
        f"""
        SELECT count(*) FROM read_csv_auto({literal(path)}, all_varchar=true, header=true)
        WHERE {normalized_case} IS NULL
           OR upper(trim(site)) NOT IN ({allowed_sites})
           OR NOT regexp_full_match(upper(trim(service_code)), {service_pattern})
           OR left(upper(trim(service_code)), 1) <> upper(trim(site))
           OR try_cast(referral_ts AS DATE) IS NULL
           OR try_cast(patient_age AS INTEGER) NOT BETWEEN 0 AND 120
           OR lower(trim(cancelled)) NOT IN ('true', 'false')
           OR (nullif(trim(readiness_days), '') IS NOT NULL
               AND (try_cast(readiness_days AS DOUBLE) IS NULL
                    OR try_cast(readiness_days AS DOUBLE) < 0))
        """,
    )
    return [require(invalid == 0, "snapshot.types_nullability_ranges", invalid, 0)]


def _reconciliation_query(snapshot_path: Path) -> str:
    normalized_case = case_id_expression("case_ref")
    return f"""
    WITH snapshot_rows AS (
        SELECT {normalized_case} AS case_id, try_cast(referral_ts AS DATE) referral_date
        FROM read_csv_auto({literal(snapshot_path)}, all_varchar=true, header=true)
    ), snapshot_match AS (
        SELECT w.case_id,
               count(*) FILTER (WHERE s.referral_date = cast(w.referral_ts_local AS DATE)) > 0
                 AS snapshot_has_event_referral_date
        FROM case_workflow w LEFT JOIN snapshot_rows s USING (case_id)
        GROUP BY w.case_id
    )
    SELECT
        coalesce(w.case_id, s.case_id) AS case_id,
        w.case_id IS NOT NULL AS in_event_log,
        s.case_id IS NOT NULL AS in_snapshot,
        CASE WHEN w.case_id IS NULL THEN 'snapshot_only'
             WHEN s.case_id IS NULL THEN 'event_only'
             WHEN s.snapshot_distinct_payload_count > 1 THEN 'matched_snapshot_conflict'
             ELSE 'matched_unambiguous' END AS reconciliation_status,
        w.site AS event_site, s.snapshot_site,
        CASE WHEN w.case_id IS NULL OR s.case_id IS NULL THEN NULL
             ELSE w.site = s.snapshot_site END AS site_matches,
        w.service_code AS event_service_code, s.snapshot_service_code,
        CASE WHEN w.case_id IS NULL OR s.case_id IS NULL THEN NULL
             ELSE w.service_code = s.snapshot_service_code END AS service_matches,
        cast(w.referral_ts_local AS DATE) AS event_referral_date,
        s.snapshot_referral_date_min, s.snapshot_referral_date_max,
        m.snapshot_has_event_referral_date,
        s.snapshot_row_count, s.snapshot_distinct_payload_count,
        s.snapshot_referral_date_count,
        s.snapshot_cancelled_value_count, s.snapshot_readiness_value_count,
        'event_log' AS authoritative_workflow_source,
        'snapshot capture time unstated; retained for reconciliation only' AS authority_reason
    FROM case_workflow w
    FULL OUTER JOIN snapshot_case_summary s USING (case_id)
    LEFT JOIN snapshot_match m ON coalesce(w.case_id, s.case_id) = m.case_id
    """


def _partition_characterization_query(config: PipelineConfig) -> str:
    cutoff_month = config.study_window.end[:7]
    return f"""
    SELECT
        source_partition,
        CASE WHEN source_partition > {literal(cutoff_month)}
             THEN 'post_study_ingest_partition' ELSE 'nominal_study_ingest_partition' END
             AS ingest_partition_status,
        count(*) AS raw_row_count,
        count(*) FILTER (WHERE rejection_reason IS NULL) AS accepted_row_count,
        count(*) FILTER (WHERE rejection_reason IS NOT NULL) AS quarantined_row_count,
        min(event_ts_local) AS min_event_ts_local,
        max(event_ts_local) AS max_event_ts_local,
        min(event_ts_utc) AS min_event_ts_utc,
        max(event_ts_utc) AS max_event_ts_utc,
        count(*) FILTER (WHERE local_study_status='in_study') AS local_in_study_rows,
        count(*) FILTER (WHERE local_study_status='post_study') AS local_post_study_rows,
        count(*) FILTER (WHERE utc_study_status='post_study') AS utc_post_study_rows,
        count(*) FILTER (WHERE source_partition > {literal(cutoff_month)}
                          AND local_study_status='in_study') AS late_in_window_rows,
        count(*) FILTER (WHERE service_code_backfilled_default) AS backfilled_default_rows,
        count(*) FILTER (WHERE event_timezone_resolution='offset_zone_corrected'
                          OR ingest_timezone_resolution='offset_zone_corrected')
            AS timezone_corrected_rows,
        count(*) FILTER (WHERE rejection_reason LIKE '%nonexistent_local_time')
            AS nonexistent_local_time_rows,
        count(*) FILTER (WHERE cross_service_event) AS cross_service_rows,
        count(*) FILTER (WHERE clinician_coverage_anomaly) AS coverage_anomaly_rows
    FROM normalized_events GROUP BY source_partition
    """


def _late_revisions_query() -> str:
    return """
    SELECT
        cast(event_ts_local AS DATE) AS event_date_local,
        cast(ingest_ts_local AS DATE) AS revision_date_local,
        source_partition,
        date_diff('day', cast(event_ts_local AS DATE), cast(ingest_ts_local AS DATE))
            AS calendar_days_late,
        count(*) AS arrived_row_count,
        count(DISTINCT semantic_event_key) AS arrived_semantic_event_count,
        max(ingest_lag_microseconds) AS max_ingest_lag_microseconds,
        bool_or(ingest_lag_microseconds >= 777600000000::BIGINT)
            AS includes_at_least_nine_day_arrival
    FROM normalized_events
    WHERE rejection_reason IS NULL
      AND cast(ingest_ts_local AS DATE) > cast(event_ts_local AS DATE)
    GROUP BY 1, 2, 3, 4
    """


def _write_parquet(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    destination: Path,
    order_by: str,
) -> None:
    connection.execute(copy_query(query, destination, order_by))


def _file_manifest_entry(connection: duckdb.DuckDBPyConnection, path: Path, grain: object) -> dict[str, object]:
    row_count = scalar(connection, f"SELECT count(*) FROM read_parquet({literal(path)})")
    return {
        "path": path.name,
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
        "rows": row_count,
        "declared_grain": grain,
    }


def _append_revision_history(
    path: Path,
    previous: str | None,
    current: str,
    changed_partitions: list[str],
    data_version: str,
) -> None:
    if previous == current:
        return
    entry = {
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "previous_artifact_set_id": previous,
        "current_artifact_set_id": current,
        "changed_partitions": changed_partitions,
        "data_version": data_version,
        "visibility": "operational history; intentionally excluded from deterministic artifacts",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write((json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _read_current(output_root: Path) -> str | None:
    path = output_root / "CURRENT"
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def _verify_same_artifacts(left: Path, right: Path) -> None:
    left_files = sorted(path.name for path in left.iterdir() if path.is_file())
    right_files = sorted(path.name for path in right.iterdir() if path.is_file())
    if left_files != right_files:
        raise ContractViolation("deterministic rerun produced a different artifact file set")
    for name in left_files:
        if _sha256_file(left / name) != _sha256_file(right / name):
            raise ContractViolation(f"deterministic rerun mismatch in {name}")


def run_pipeline(
    paths: RuntimePaths,
    *,
    mode: str = "incremental",
    from_partition: str | None = None,
    through_partition: str | None = None,
    fail_after_partitions: int | None = None,
    threads: int = 8,
    memory_limit: str = "12GB",
) -> RunResult:
    if mode not in {"full", "incremental"}:
        raise PipelineError("mode must be full or incremental")
    if from_partition and not re.fullmatch(r"\d{4}-\d{2}", from_partition):
        raise PipelineError("from_partition must be YYYY-MM")
    if through_partition and not re.fullmatch(r"\d{4}-\d{2}", through_partition):
        raise PipelineError("through_partition must be YYYY-MM")
    if from_partition and through_partition and from_partition > through_partition:
        raise PipelineError("from_partition cannot be after through_partition")

    config, config_bytes = load_config(paths.config_path)
    engineering_seed = int(environment_default("BARNABUS_SEED", "20250301") or "20250301")
    random.seed(engineering_seed)
    paths.work_root.mkdir(parents=True, exist_ok=True)
    paths.output_root.mkdir(parents=True, exist_ok=True)
    metrics = MetricsRecorder()
    total_start = time.perf_counter()
    run_token = uuid.uuid4().hex
    code_version = _package_code_hash()
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    runtime_lock = paths.config_path.parent.parent / "requirements.lock"
    environment_lock_sha = _sha256_file(runtime_lock) if runtime_lock.is_file() else "unavailable"
    inputs = _inventory_inputs(paths, metrics)
    event_inputs = [item for item in inputs if item.partition is not None]
    raw_rows = sum(item.parquet_rows or 0 for item in event_inputs)
    require(
        raw_rows >= config.contracts.expected_raw_min_rows,
        "event_log.minimum_rows",
        raw_rows,
        f">={config.contracts.expected_raw_min_rows}",
    )
    input_manifest = [
        {
            "path": item.relative_path,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "parquet_rows": item.parquet_rows,
        }
        for item in inputs
    ]
    data_version = _sha256_payload(input_manifest)
    clinician_input = next(item for item in input_manifest if item["path"] == "clinicians.csv")
    normalizer_version = _sha256_payload(
        {
            "normalizer_code_sha256": _normalizer_code_hash(),
            "normalizer_config": {
                "study_window": config.study_window.model_dump(mode="json"),
                "event_time": config.event_time.model_dump(mode="json"),
                "schema": config.schema_.model_dump(mode="json"),
                "contracts": config.contracts.model_dump(mode="json"),
            },
            "clinicians": clinician_input,
        }
    )

    state_path = paths.work_root / "partition-state.json"
    state = _load_json(state_path, {"version": STATE_VERSION, "partitions": {}})
    if state.get("version") != STATE_VERSION:
        raise PipelineError("partition state version is not supported")
    state_partitions: dict[str, dict[str, object]] = state.setdefault("partitions", {})
    cache_root = paths.work_root / "normalized-partitions"
    cache_root.mkdir(parents=True, exist_ok=True)
    selected_caches: list[Path] = []
    changed_partitions: list[str] = []
    processed = 0

    for item in event_inputs:
        assert item.partition is not None
        cache_identity = _sha256_payload(
            {"raw_sha256": item.sha256, "normalizer_version": normalizer_version}
        )
        cache_path = cache_root / f"ingest_month={item.partition}-{cache_identity[:20]}.parquet"
        existing = state_partitions.get(item.relative_path, {})
        in_forced_window = (
            (from_partition is None or item.partition >= from_partition)
            and (through_partition is None or item.partition <= through_partition)
            and (from_partition is not None or through_partition is not None)
        )
        reusable = (
            mode == "incremental"
            and not in_forced_window
            and existing.get("cache_identity") == cache_identity
            and cache_path.is_file()
            and existing.get("cache_sha256") == _sha256_file(cache_path)
        )
        if reusable:
            with metrics.step(f"normalize_partition:{item.partition}", reused=True):
                pass
        else:
            changed_partitions.append(item.partition)
            with metrics.step(f"normalize_partition:{item.partition}"):
                rows, rejected, fatal = _normalize_partition(
                    item,
                    cache_path,
                    paths,
                    config,
                    threads,
                    memory_limit,
                    f"{run_token}-{item.partition}",
                )
                state_partitions[item.relative_path] = {
                    "partition": item.partition,
                    "raw_sha256": item.sha256,
                    "cache_identity": cache_identity,
                    "cache_path": cache_path.relative_to(paths.work_root).as_posix(),
                    "cache_sha256": _sha256_file(cache_path),
                    "row_count": rows,
                    "rejected_row_count": rejected,
                    "fatal_rejection_count": fatal,
                }
                _atomic_write_bytes(state_path, _json_bytes(state))
                if fatal:
                    raise ContractViolation(
                        f"partition {item.partition} contains {fatal} fatal rejected rows; "
                        f"diagnostic cache retained at {cache_path}"
                    )
            processed += 1
            if fail_after_partitions is not None and processed >= fail_after_partitions:
                raise InjectedFailure(
                    f"injected after {processed} durable partition checkpoints; no artifacts published"
                )
        selected_caches.append(cache_path)

    artifact_set_id = _sha256_payload(
        {
            "data_version": data_version,
            "config_sha256": config_sha,
            "code_version": code_version,
            "environment_lock_sha256": environment_lock_sha,
            "engineering_seed": engineering_seed,
            "package_version": __version__,
        }
    )
    staging = paths.output_root / ".staging" / f"{artifact_set_id}-{run_token}"
    staging.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect()
    contract_results: list[ContractResult] = []
    try:
        _configure_connection(connection, paths.work_root, threads, memory_limit, f"final-{run_token}")
        contract_results.extend(_validate_clinicians(connection, paths.data_root / "clinicians.csv"))
        connection.execute(
            f"CREATE OR REPLACE TEMP VIEW normalized_events AS "
            f"SELECT * FROM {parquet_relation(selected_caches)}"
        )
        with metrics.step("validate_global_normalized_contracts"):
            normalized_rows = scalar(connection, "SELECT count(*) FROM normalized_events")
            contract_results.append(
                require(normalized_rows == raw_rows, "normalized.row_conservation", normalized_rows, raw_rows)
            )
            contract_results.append(assert_unique(connection, "normalized_events", ["event_id"], "normalized_events"))
            accepted_rows, quarantine_rows = connection.execute(
                "SELECT count(*) FILTER (WHERE rejection_reason IS NULL), "
                "count(*) FILTER (WHERE rejection_reason IS NOT NULL) FROM normalized_events"
            ).fetchone()
            contract_results.append(
                require(
                    int(accepted_rows) + int(quarantine_rows) == raw_rows,
                    "normalized.disposition_conservation",
                    int(accepted_rows) + int(quarantine_rows),
                    raw_rows,
                )
            )
            fatal_reasons = string_list(config.contracts.contained_rejection_reasons)
            fatal = scalar(
                connection,
                "SELECT count(*) FROM normalized_events WHERE rejection_reason IS NOT NULL "
                f"AND rejection_reason NOT IN ({fatal_reasons})",
            )
            contract_results.append(require(fatal == 0, "normalized.no_fatal_rejections", fatal, 0))

        with metrics.step("build_canonical_events"):
            canonical_path = staging / "canonical_events.parquet"
            _write_parquet(connection, _canonical_query(), canonical_path, "semantic_event_key")
            connection.execute(
                f"CREATE OR REPLACE TEMP VIEW canonical_events AS "
                f"SELECT * FROM read_parquet({literal(canonical_path)})"
            )
            canonical_rows = scalar(connection, "SELECT count(*) FROM canonical_events")
            expected_canonical = scalar(
                connection,
                "SELECT count(DISTINCT semantic_event_key) FROM normalized_events "
                "WHERE rejection_reason IS NULL",
            )
            contract_results.append(
                require(
                    canonical_rows == expected_canonical,
                    "canonical.semantic_dedup_count",
                    canonical_rows,
                    expected_canonical,
                )
            )
            contract_results.append(
                assert_unique(connection, "canonical_events", ["semantic_event_key"], "canonical_events")
            )
            duplicate_workflow = scalar(
                connection,
                "SELECT count(*) FROM (SELECT case_id, event_type FROM canonical_events "
                "WHERE is_workflow_event GROUP BY 1, 2 HAVING count(*) > 1)",
            )
            contract_results.append(
                require(
                    duplicate_workflow == 0,
                    "canonical.workflow_case_event_unique",
                    duplicate_workflow,
                    0,
                )
            )

        with metrics.step("build_case_workflow"):
            case_path = staging / "case_workflow.parquet"
            _write_parquet(connection, _case_workflow_query(config), case_path, "case_id")
            connection.execute(
                f"CREATE OR REPLACE TEMP VIEW case_workflow AS "
                f"SELECT * FROM read_parquet({literal(case_path)})"
            )
            contract_results.append(assert_unique(connection, "case_workflow", ["case_id"], "case_workflow"))
            expected_cases = scalar(
                connection, "SELECT count(DISTINCT case_id) FROM canonical_events WHERE is_workflow_event"
            )
            observed_cases = scalar(connection, "SELECT count(*) FROM case_workflow")
            contract_results.append(
                require(observed_cases == expected_cases, "case_workflow.row_count", observed_cases, expected_cases)
            )
            contract_results.extend(
                assert_many_to_one_join(
                    connection,
                    "(SELECT * FROM canonical_events WHERE is_workflow_event)",
                    "case_workflow",
                    ["case_id"],
                    "canonical_to_case_workflow",
                )
            )

        with metrics.step("build_service_day"):
            service_path = staging / "service_day.parquet"
            _write_parquet(
                connection,
                _service_day_query(),
                service_path,
                "event_date_local, site, service_code",
            )
            connection.execute(
                f"CREATE OR REPLACE TEMP VIEW service_day AS "
                f"SELECT * FROM read_parquet({literal(service_path)})"
            )
            contract_results.append(
                assert_unique(
                    connection,
                    "service_day",
                    ["event_date_local", "site", "service_code"],
                    "service_day",
                )
            )

        with metrics.step("build_snapshot_reconciliation"):
            snapshot_path = paths.data_root / "snapshot_cases.csv"
            contract_results.extend(_validate_snapshot(connection, snapshot_path, config))
            snapshot_output = staging / "snapshot_case_summary.parquet"
            _write_parquet(connection, _snapshot_query(snapshot_path), snapshot_output, "case_id")
            connection.execute(
                f"CREATE OR REPLACE TEMP VIEW snapshot_case_summary AS "
                f"SELECT * FROM read_parquet({literal(snapshot_output)})"
            )
            contract_results.append(
                assert_unique(connection, "snapshot_case_summary", ["case_id"], "snapshot_case_summary")
            )
            contract_results.extend(
                assert_one_to_one_join(
                    connection,
                    "case_workflow",
                    "snapshot_case_summary",
                    ["case_id"],
                    "workflow_snapshot_reconciliation",
                )
            )
            reconciliation_path = staging / "reconciliation.parquet"
            _write_parquet(
                connection,
                _reconciliation_query(snapshot_path),
                reconciliation_path,
                "case_id",
            )
            connection.execute(
                f"CREATE OR REPLACE TEMP VIEW reconciliation AS "
                f"SELECT * FROM read_parquet({literal(reconciliation_path)})"
            )
            contract_results.append(assert_unique(connection, "reconciliation", ["case_id"], "reconciliation"))
            expected_reconciliation = scalar(
                connection,
                "SELECT count(*) FROM (SELECT case_id FROM case_workflow UNION "
                "SELECT case_id FROM snapshot_case_summary)",
            )
            observed_reconciliation = scalar(connection, "SELECT count(*) FROM reconciliation")
            contract_results.append(
                require(
                    observed_reconciliation == expected_reconciliation,
                    "reconciliation.union_row_count",
                    observed_reconciliation,
                    expected_reconciliation,
                )
            )

        with metrics.step("build_quality_and_revision_outputs"):
            partition_path = staging / "partition_characterization.parquet"
            _write_parquet(
                connection,
                _partition_characterization_query(config),
                partition_path,
                "source_partition",
            )
            revision_path = staging / "late_arrival_revisions.parquet"
            _write_parquet(
                connection,
                _late_revisions_query(),
                revision_path,
                "event_date_local, revision_date_local, source_partition",
            )
            quarantine_path = staging / "quarantine.parquet"
            _write_parquet(
                connection,
                "SELECT * FROM normalized_events WHERE rejection_reason IS NOT NULL",
                quarantine_path,
                "event_id, raw_row_hash",
            )
            connection.execute(
                f"CREATE OR REPLACE TEMP VIEW partition_characterization AS "
                f"SELECT * FROM read_parquet({literal(partition_path)})"
            )
            connection.execute(
                f"CREATE OR REPLACE TEMP VIEW late_arrival_revisions AS "
                f"SELECT * FROM read_parquet({literal(revision_path)})"
            )
            connection.execute(
                f"CREATE OR REPLACE TEMP VIEW quarantine AS "
                f"SELECT * FROM read_parquet({literal(quarantine_path)})"
            )
            contract_results.append(
                assert_unique(
                    connection, "partition_characterization", ["source_partition"], "partition_characterization"
                )
            )
            contract_results.append(assert_unique(connection, "quarantine", ["event_id"], "quarantine"))
            contract_results.append(
                assert_unique(
                    connection,
                    "late_arrival_revisions",
                    ["event_date_local", "revision_date_local", "source_partition"],
                    "late_arrival_revisions",
                )
            )

        with metrics.step("write_contracts_and_manifest"):
            contracts_payload = {
                "schema_version": 1,
                "all_passed": all(result.passed for result in contract_results),
                "results": [asdict(result) for result in contract_results],
            }
            contracts_path = staging / "contract_results.json"
            contracts_path.write_bytes(_json_bytes(contracts_payload))
            parquet_files = {
                "canonical_events.parquet": config.grains["canonical_events"],
                "case_workflow.parquet": config.grains["case_workflow"],
                "service_day.parquet": config.grains["service_day"],
                "snapshot_case_summary.parquet": config.grains["snapshot_case_summary"],
                "reconciliation.parquet": config.grains["reconciliation"],
                "late_arrival_revisions.parquet": config.grains["late_arrival_revisions"],
                "partition_characterization.parquet": config.grains["partition_characterization"],
                "quarantine.parquet": config.grains["quarantine"],
            }
            artifacts = [
                _file_manifest_entry(connection, staging / name, grain)
                for name, grain in sorted(parquet_files.items())
            ]
            artifacts.append(
                {
                    "path": contracts_path.name,
                    "sha256": _sha256_file(contracts_path),
                    "size_bytes": contracts_path.stat().st_size,
                    "rows": len(contract_results),
                    "declared_grain": "contract_name",
                }
            )
            manifest = {
                "schema_version": 1,
                "artifact_set_id": artifact_set_id,
                "package_version": __version__,
                "code_tree_sha256": code_version,
                "environment_lock_sha256": environment_lock_sha,
                "engineering_seed": engineering_seed,
                "config_sha256": config_sha,
                "data_version": data_version,
                "inputs": input_manifest,
                "artifacts": artifacts,
                "source_of_truth": {
                    "workflow": "append-only event log",
                    "snapshot": "diagnostic only; capture time is unstated and normalized key is non-unique",
                },
                "time_convention": {
                    "stored": "timezone-naive UTC after configurable IANA normalization",
                    "study_window_boundary": "source-local business date; UTC status retained separately",
                    "device_missing_offset": "configured UTC inference",
                    "nonexistent_local_time": "quarantined from ordered workflow and retained in diagnostics",
                },
                "determinism": {
                    "row_order": "explicit for every artifact",
                    "money": "integer cents / DECIMAL; no unordered floating-point sum",
                    "operational_telemetry": "stored outside deterministic artifact set",
                },
                "scale_evidence": "local run only unless telemetry explicitly says otherwise",
            }
            (staging / "artifact_manifest.json").write_bytes(_json_bytes(manifest))
    except Exception:
        failure = {
            "failed_at_utc": datetime.now(UTC).isoformat(),
            "artifact_set_id": artifact_set_id,
            "publication": "not published; CURRENT unchanged",
        }
        (staging / "FAILED.json").write_bytes(_json_bytes(failure))
        raise
    finally:
        connection.close()

    final_directory = paths.output_root / "artifacts" / artifact_set_id
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    reused_artifact_set = False
    if final_directory.exists():
        _verify_same_artifacts(staging, final_directory)
        shutil.rmtree(staging)
        reused_artifact_set = True
    else:
        os.replace(staging, final_directory)

    previous_current = _read_current(paths.output_root)
    _append_revision_history(
        paths.work_root / "revision-history.jsonl",
        previous_current,
        artifact_set_id,
        sorted(changed_partitions),
        data_version,
    )
    _atomic_write_bytes(paths.output_root / "CURRENT", (artifact_set_id + "\n").encode("ascii"))

    total_seconds = time.perf_counter() - total_start
    peak_memory = max((step.peak_rss_bytes for step in metrics.steps), default=0)
    projection = total_seconds * (400_000_000 / raw_rows) if raw_rows else None
    telemetry = {
        "schema_version": 1,
        "run_id": run_token,
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "mode": mode,
        "artifact_set_id": artifact_set_id,
        "data_version": data_version,
        "raw_rows_observed": raw_rows,
        "wall_seconds_observed": total_seconds,
        "peak_rss_bytes_observed": peak_memory,
        "steps": metrics.as_dicts(),
        "machine": {
            "logical_cpu_count": psutil.cpu_count(logical=True),
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "total_memory_bytes": psutil.virtual_memory().total,
            "duckdb_threads": threads,
            "duckdb_memory_limit": memory_limit.upper(),
        },
        "sealed_scale_projection": {
            "target_rows": 400_000_000,
            "linear_seconds": projection,
            "status": "unvalidated linear projection; not a sealed-scale benchmark",
        },
        "deterministic_artifacts_exclude_this_file": True,
    }
    telemetry_path = paths.work_root / "telemetry" / f"{run_token}.json"
    _atomic_write_bytes(telemetry_path, _json_bytes(telemetry))
    return RunResult(
        artifact_set_id=artifact_set_id,
        artifact_directory=final_directory,
        data_version=data_version,
        raw_rows=raw_rows,
        canonical_rows=canonical_rows,
        quarantine_rows=int(quarantine_rows),
        telemetry_path=telemetry_path,
        reused_artifact_set=reused_artifact_set,
    )


def verify_artifacts(output_root: Path) -> dict[str, object]:
    artifact_set_id = _read_current(output_root)
    if not artifact_set_id:
        raise PipelineError("no CURRENT artifact set is published")
    directory = output_root / "artifacts" / artifact_set_id
    manifest_path = directory / "artifact_manifest.json"
    if not manifest_path.is_file():
        raise ContractViolation("CURRENT artifact set has no manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    for artifact in manifest["artifacts"]:
        path = directory / artifact["path"]
        if not path.is_file() or _sha256_file(path) != artifact["sha256"]:
            failures.append(str(artifact["path"]))
    if failures:
        raise ContractViolation(f"artifact hash verification failed: {failures}")
    return {"artifact_set_id": artifact_set_id, "verified_files": len(manifest["artifacts"])}


def _default_config_path() -> Path:
    configured = environment_default("BARNABUS_CONFIG")
    return Path(configured) if configured else Path("config/pipeline.yaml")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="barnabus-pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="build and atomically publish analytic artifacts")
    run.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    run.add_argument("--from-partition", metavar="YYYY-MM")
    run.add_argument("--through-partition", metavar="YYYY-MM")
    run.add_argument("--fail-after-partitions", type=int, help=argparse.SUPPRESS)
    run.add_argument("--config", type=Path, default=_default_config_path())
    run.add_argument("--data-root", type=Path, default=environment_default("BARNABUS_DATA_ROOT"))
    run.add_argument("--work-root", type=Path, default=environment_default("BARNABUS_WORK_ROOT", "work"))
    run.add_argument("--output-root", type=Path, default=environment_default("BARNABUS_OUTPUT_ROOT", "outputs"))
    verify = subparsers.add_parser("verify", help="verify the hashes in the published artifact set")
    verify.add_argument("--output-root", type=Path, default=environment_default("BARNABUS_OUTPUT_ROOT", "outputs"))
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            _emit({"level": "info", "event": "verification_complete", **verify_artifacts(args.output_root.resolve())})
            return 0
        if args.data_root is None:
            raise PipelineError("set BARNABUS_DATA_ROOT or pass --data-root")
        paths = RuntimePaths(
            data_root=Path(args.data_root),
            work_root=Path(args.work_root),
            output_root=Path(args.output_root),
            config_path=Path(args.config),
        )
        result = run_pipeline(
            paths,
            mode=args.mode,
            from_partition=args.from_partition,
            through_partition=args.through_partition,
            fail_after_partitions=args.fail_after_partitions,
            threads=int(environment_default("BARNABUS_DUCKDB_THREADS", "8") or "8"),
            memory_limit=environment_default("BARNABUS_DUCKDB_MEMORY_LIMIT", "12GB") or "12GB",
        )
        _emit(
            {
                "level": "info",
                "event": "pipeline_complete",
                "artifact_set_id": result.artifact_set_id,
                "artifact_directory": str(result.artifact_directory),
                "data_version": result.data_version,
                "raw_rows": result.raw_rows,
                "canonical_rows": result.canonical_rows,
                "quarantine_rows": result.quarantine_rows,
                "telemetry": str(result.telemetry_path),
                "reused_artifact_set": result.reused_artifact_set,
            }
        )
        return 0
    except (PipelineError, ValueError, OSError, duckdb.Error) as exc:
        _emit({"level": "error", "event": "pipeline_failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
