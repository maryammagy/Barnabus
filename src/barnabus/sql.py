"""DuckDB SQL generation for the out-of-core event model."""

from __future__ import annotations

from pathlib import Path

from barnabus.config import PipelineConfig


def literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''").replace("\\", "/") + "'"


def string_list(values: list[str]) -> str:
    return ", ".join(literal(value) for value in values)


def parquet_relation(paths: list[Path]) -> str:
    if not paths:
        raise ValueError("at least one parquet path is required")
    if len(paths) == 1:
        return f"read_parquet({literal(paths[0])}, union_by_name=true)"
    joined = ", ".join(literal(path) for path in paths)
    return f"read_parquet([{joined}], union_by_name=true)"


def case_id_expression(column: str) -> str:
    return (
        f"CASE WHEN regexp_full_match(upper(trim({column})), 'C[0-9]{{9}}') "
        f"THEN upper(trim({column})) "
        f"WHEN regexp_full_match(trim({column}), '[0-9]+') "
        f"AND length(trim({column})) <= 9 "
        f"THEN 'C' || lpad(trim({column}), 9, '0') ELSE NULL END"
    )


def _column_or_null(columns: set[str], name: str, sql_type: str) -> str:
    if name in columns:
        return f"CAST({name} AS {sql_type}) AS raw_{name}"
    return f"CAST(NULL AS {sql_type}) AS raw_{name}"


def normalized_events_query(
    raw_path: Path,
    relative_source_file: str,
    source_partition: str,
    clinicians_path: Path,
    columns: set[str],
    config: PipelineConfig,
) -> str:
    required = {
        "case_id",
        "event_type",
        "site",
        "clinician_id",
        "ward_id",
        "cost_cad",
        "ingest_ts",
        "source_system",
        "tz_offset_hours",
        "event_ts",
        "event_id",
    }
    missing = sorted(required - columns)
    if missing:
        raise ValueError(f"Parquet schema is missing required columns: {missing}")
    if not ({"service_code", "svc_code"} & columns):
        raise ValueError("Parquet schema must contain service_code or svc_code")

    schema = config.schema_
    contracts = config.contracts
    workflow_types = string_list(schema.workflow_event_order)
    allowed_types = string_list(config.allowed_event_types)
    allowed_sources = string_list(sorted(config.event_time.sources))
    allowed_sites = string_list(contracts.allowed_sites)
    study_start = literal(config.study_window.start)
    study_end = literal(config.study_window.end)
    legacy_col = _column_or_null(columns, "svc_code", "VARCHAR")
    current_col = _column_or_null(columns, "service_code", "VARCHAR")
    normalized_case = case_id_expression("raw_case_id")
    code_pattern = literal(schema.service_code_pattern)
    event_id_pattern = literal(schema.event_id_pattern)
    clinician_pattern = literal(schema.clinician_id_pattern)
    ward_pattern = literal(schema.ward_id_pattern)
    offset_contract_terms: list[str] = []
    for source, source_config in sorted(config.event_time.sources.items()):
        if source_config.supplied_offset_required:
            allowed_offsets = ", ".join(str(value) for value in source_config.allowed_offsets_hours)
            offset_contract_terms.append(
                f"(source_system={literal(source)} AND "
                f"(raw_tz_offset_hours IS NULL OR raw_tz_offset_hours NOT IN ({allowed_offsets})))"
            )
        elif not source_config.allowed_offsets_hours:
            offset_contract_terms.append(
                f"(source_system={literal(source)} AND raw_tz_offset_hours IS NOT NULL)"
            )
    invalid_offset_expression = " OR ".join(offset_contract_terms) or "false"

    return f"""
WITH raw AS (
    SELECT
        CAST(case_id AS VARCHAR) AS raw_case_id,
        CAST(event_type AS VARCHAR) AS raw_event_type,
        CAST(site AS VARCHAR) AS raw_site,
        CAST(clinician_id AS VARCHAR) AS raw_clinician_id,
        CAST(ward_id AS VARCHAR) AS raw_ward_id,
        CAST(cost_cad AS DOUBLE) AS raw_cost_cad,
        CAST(ingest_ts AS TIMESTAMP_NS) AS raw_ingest_ts,
        CAST(source_system AS VARCHAR) AS raw_source_system,
        CAST(tz_offset_hours AS DOUBLE) AS raw_tz_offset_hours,
        CAST(event_ts AS VARCHAR) AS raw_event_ts,
        {legacy_col},
        {current_col},
        CAST(event_id AS VARCHAR) AS raw_event_id
    FROM read_parquet({literal(raw_path)}, union_by_name=true)
), typed AS (
    SELECT
        *,
        lower(trim(raw_event_id)) AS event_id,
        {normalized_case} AS case_id,
        lower(trim(raw_event_type)) AS event_type,
        upper(trim(raw_site)) AS site,
        upper(trim(raw_clinician_id)) AS clinician_id,
        upper(trim(raw_ward_id)) AS ward_id,
        trim(raw_source_system) AS source_system,
        try_cast(raw_event_ts AS TIMESTAMP_NS) AS event_ts_local,
        raw_ingest_ts AS ingest_ts_local,
        CASE
            WHEN raw_service_code IS NOT NULL
             AND upper(trim(raw_service_code)) <> 'UNKNOWN'
                THEN upper(trim(raw_service_code))
            WHEN raw_svc_code IS NOT NULL
             AND upper(trim(raw_svc_code)) <> 'UNKNOWN'
                THEN upper(trim(raw_svc_code))
            ELSE NULL
        END AS service_code,
        CASE
            WHEN raw_service_code IS NOT NULL
             AND upper(trim(raw_service_code)) <> 'UNKNOWN'
                THEN 'current_observed'
            WHEN raw_svc_code IS NOT NULL
             AND upper(trim(raw_svc_code)) <> 'UNKNOWN'
             AND upper(trim(coalesce(raw_service_code, ''))) = 'UNKNOWN'
                THEN 'legacy_observed_backfilled_default'
            WHEN raw_svc_code IS NOT NULL
             AND upper(trim(raw_svc_code)) <> 'UNKNOWN'
                THEN 'legacy_observed'
            ELSE 'missing'
        END AS service_code_provenance,
        raw_service_code IS NOT NULL
          AND upper(trim(raw_service_code)) = 'UNKNOWN'
          AND raw_svc_code IS NOT NULL
          AND upper(trim(raw_svc_code)) <> 'UNKNOWN'
            AS service_code_backfilled_default,
        CASE
            WHEN raw_service_code IS NOT NULL
             AND upper(trim(raw_service_code)) <> 'UNKNOWN' THEN 'current'
            ELSE 'legacy'
        END AS schema_generation,
        CASE WHEN raw_cost_cad IS NULL THEN NULL
             ELSE cast(round(raw_cost_cad * 100) AS BIGINT) END AS cost_cents
    FROM raw
), with_rules AS (
    SELECT
        t.*,
        er.valid_offset_1 AS event_valid_offset_1,
        er.valid_offset_2 AS event_valid_offset_2,
        er.valid_count AS event_valid_offset_count,
        ir.valid_offset_1 AS ingest_valid_offset_1,
        ir.valid_offset_2 AS ingest_valid_offset_2,
        ir.valid_count AS ingest_valid_offset_count
    FROM typed t
    LEFT JOIN timezone_rules er
      ON t.source_system = er.source_system
     AND date_trunc('hour', t.event_ts_local) = er.local_hour
    LEFT JOIN timezone_rules ir
      ON t.source_system = ir.source_system
     AND date_trunc('hour', t.ingest_ts_local) = ir.local_hour
), resolved AS (
    SELECT
        *,
        CASE
            WHEN event_valid_offset_count = 1 THEN event_valid_offset_1
            WHEN event_valid_offset_count = 2
             AND raw_tz_offset_hours IN (event_valid_offset_1, event_valid_offset_2)
                THEN raw_tz_offset_hours
            ELSE NULL
        END AS event_offset_hours,
        CASE
            WHEN ingest_valid_offset_count = 1 THEN ingest_valid_offset_1
            WHEN ingest_valid_offset_count = 2
             AND raw_tz_offset_hours IN (ingest_valid_offset_1, ingest_valid_offset_2)
                THEN raw_tz_offset_hours
            ELSE NULL
        END AS ingest_offset_hours,
        CASE
            WHEN event_valid_offset_count = 0 THEN 'nonexistent_local_time'
            WHEN event_valid_offset_count = 2 THEN 'ambiguous_fold_disambiguated'
            WHEN raw_tz_offset_hours IS NULL THEN 'declared_source_default'
            WHEN raw_tz_offset_hours <> event_valid_offset_1 THEN 'offset_zone_corrected'
            ELSE 'zone_consistent'
        END AS event_timezone_resolution,
        CASE
            WHEN ingest_valid_offset_count = 0 THEN 'nonexistent_local_time'
            WHEN ingest_valid_offset_count = 2 THEN 'ambiguous_fold_disambiguated'
            WHEN raw_tz_offset_hours IS NULL THEN 'declared_source_default'
            WHEN raw_tz_offset_hours <> ingest_valid_offset_1 THEN 'offset_zone_corrected'
            ELSE 'zone_consistent'
        END AS ingest_timezone_resolution
    FROM with_rules
), normalized AS (
    SELECT
        *,
        event_ts_local - to_seconds(cast(event_offset_hours * 3600 AS BIGINT)) AS event_ts_utc,
        ingest_ts_local - to_seconds(cast(ingest_offset_hours * 3600 AS BIGINT)) AS ingest_ts_utc,
        cast(cost_cents AS DECIMAL(20, 2)) / cast(100 AS DECIMAL(20, 2)) AS cost_cad,
        event_type IN ({workflow_types}) AS is_workflow_event
    FROM resolved
), classified AS (
    SELECT
        n.*,
        c.site AS clinician_site,
        c.home_service,
        c.ward_id AS clinician_ward_id,
        c.covers_other_services,
        service_code <> c.home_service AS cross_service_event,
        service_code <> c.home_service
          AND NOT c.covers_other_services AS clinician_coverage_anomaly,
        CASE
            WHEN event_ts_local IS NULL THEN 'invalid_event_timestamp'
            WHEN ingest_ts_local IS NULL THEN 'invalid_ingest_timestamp'
            WHEN strftime(ingest_ts_local, '%Y-%m') <> {literal(source_partition)}
                THEN 'ingest_partition_mismatch'
            WHEN source_system NOT IN ({allowed_sources}) THEN 'unknown_source_system'
            WHEN {invalid_offset_expression} THEN 'invalid_supplied_offset'
            WHEN event_valid_offset_count IS NULL THEN 'missing_event_timezone_rule'
            WHEN ingest_valid_offset_count IS NULL THEN 'missing_ingest_timezone_rule'
            WHEN event_valid_offset_count = 0 THEN 'event_nonexistent_local_time'
            WHEN ingest_valid_offset_count = 0 THEN 'ingest_nonexistent_local_time'
            WHEN event_offset_hours IS NULL THEN 'unresolved_event_timezone'
            WHEN ingest_offset_hours IS NULL THEN 'unresolved_ingest_timezone'
            WHEN event_id IS NULL OR NOT regexp_full_match(event_id, {event_id_pattern})
                THEN 'malformed_event_id'
            WHEN case_id IS NULL THEN 'malformed_case_id'
            WHEN event_type NOT IN ({allowed_types}) THEN 'unknown_event_type'
            WHEN n.site NOT IN ({allowed_sites}) THEN 'invalid_site'
            WHEN clinician_id IS NULL OR NOT regexp_full_match(clinician_id, {clinician_pattern})
                THEN 'malformed_clinician_id'
            WHEN c.clinician_id IS NULL THEN 'unmatched_clinician_id'
            WHEN n.ward_id IS NULL OR NOT regexp_full_match(n.ward_id, {ward_pattern})
                THEN 'malformed_ward_id'
            WHEN c.site <> n.site OR c.ward_id <> n.ward_id THEN 'clinician_dimension_mismatch'
            WHEN service_code IS NULL OR NOT regexp_full_match(service_code, {code_pattern})
                THEN 'invalid_service_code'
            WHEN left(service_code, 1) <> n.site THEN 'service_site_mismatch'
            WHEN raw_service_code IS NOT NULL
             AND upper(trim(raw_service_code)) <> 'UNKNOWN'
             AND raw_svc_code IS NOT NULL
             AND upper(trim(raw_svc_code)) <> 'UNKNOWN'
             AND upper(trim(raw_service_code)) <> upper(trim(raw_svc_code))
                THEN 'conflicting_service_codes'
            WHEN event_type = 'case_closed' AND raw_cost_cad IS NULL THEN 'missing_close_cost'
            WHEN event_type <> 'case_closed' AND raw_cost_cad IS NOT NULL THEN 'unexpected_event_cost'
            WHEN raw_cost_cad IS NOT NULL
             AND (raw_cost_cad < {contracts.cost_cad_min}
               OR raw_cost_cad > {contracts.cost_cad_max}) THEN 'cost_out_of_range'
            WHEN raw_cost_cad IS NOT NULL
             AND abs(raw_cost_cad * 100 - round(raw_cost_cad * 100)) > 0.000001
                THEN 'cost_not_cent_exact'
            WHEN ingest_ts_utc < event_ts_utc THEN 'negative_ingest_lag'
            WHEN ingest_ts_utc > event_ts_utc + INTERVAL '{contracts.max_ingest_lag_days} days'
                THEN 'ingest_lag_exceeds_contract'
            ELSE NULL
        END AS rejection_reason
    FROM normalized n
    LEFT JOIN (
        SELECT
            upper(trim(clinician_id)) AS clinician_id,
            upper(trim(site)) AS site,
            upper(trim(home_service)) AS home_service,
            upper(trim(ward_id)) AS ward_id,
            lower(trim(covers_other_services)) = 'true' AS covers_other_services
        FROM read_csv_auto({literal(clinicians_path)}, all_varchar=true, header=true)
    ) c USING (clinician_id)
), keyed AS (
    SELECT
        *,
        sha256(concat_ws('|',
            coalesce(case_id, '<NULL>'), coalesce(event_type, '<NULL>'),
            coalesce(cast(event_ts_utc AS VARCHAR), '<NULL>'), coalesce(site, '<NULL>'),
            coalesce(clinician_id, '<NULL>'), coalesce(ward_id, '<NULL>'),
            coalesce(service_code, '<NULL>'), coalesce(cast(cost_cents AS VARCHAR), '<NULL>')
        )) AS semantic_event_key,
        sha256(concat_ws('|',
            coalesce(raw_event_id, '<NULL>'), coalesce(raw_case_id, '<NULL>'),
            coalesce(raw_event_type, '<NULL>'), coalesce(raw_event_ts, '<NULL>'),
            coalesce(cast(raw_ingest_ts AS VARCHAR), '<NULL>'),
            coalesce(raw_source_system, '<NULL>'),
            coalesce(cast(raw_tz_offset_hours AS VARCHAR), '<NULL>'),
            coalesce(raw_site, '<NULL>'), coalesce(raw_clinician_id, '<NULL>'),
            coalesce(raw_ward_id, '<NULL>'), coalesce(raw_service_code, '<NULL>'),
            coalesce(raw_svc_code, '<NULL>'), coalesce(cast(raw_cost_cad AS VARCHAR), '<NULL>')
        )) AS raw_row_hash
    FROM classified
)
SELECT
    event_id, case_id, event_type,
    event_ts_local, ingest_ts_local, event_ts_utc, ingest_ts_utc,
    source_system, raw_tz_offset_hours AS supplied_tz_offset_hours,
    event_offset_hours, ingest_offset_hours,
    event_timezone_resolution, ingest_timezone_resolution,
    site, clinician_id, ward_id, service_code,
    service_code_provenance, service_code_backfilled_default, schema_generation,
    cost_cents, cost_cad, is_workflow_event,
    coalesce(cross_service_event, false) AS cross_service_event,
    coalesce(clinician_coverage_anomaly, false) AS clinician_coverage_anomaly,
    CASE
        WHEN event_ts_local IS NULL THEN 'unknown'
        WHEN cast(event_ts_local AS DATE) < cast({study_start} AS DATE) THEN 'pre_study'
        WHEN cast(event_ts_local AS DATE) > cast({study_end} AS DATE) THEN 'post_study'
        ELSE 'in_study'
    END AS local_study_status,
    CASE
        WHEN event_ts_utc IS NULL THEN 'unknown'
        WHEN cast(event_ts_utc AS DATE) < cast({study_start} AS DATE) THEN 'pre_study'
        WHEN cast(event_ts_utc AS DATE) > cast({study_end} AS DATE) THEN 'post_study'
        ELSE 'in_study'
    END AS utc_study_status,
    date_diff('microseconds', event_ts_utc, ingest_ts_utc) AS ingest_lag_microseconds,
    {literal(source_partition)} AS source_partition,
    {literal(relative_source_file)} AS raw_source_file,
    semantic_event_key, raw_row_hash, rejection_reason,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_event_id END AS rejected_raw_event_id,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_case_id END AS rejected_raw_case_id,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_event_type END AS rejected_raw_event_type,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_event_ts END AS rejected_raw_event_ts,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_source_system END AS rejected_raw_source_system,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_site END AS rejected_raw_site,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_clinician_id END AS rejected_raw_clinician_id,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_ward_id END AS rejected_raw_ward_id,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_service_code END AS rejected_raw_service_code,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_svc_code END AS rejected_raw_svc_code,
    CASE WHEN rejection_reason IS NOT NULL THEN raw_cost_cad END AS rejected_raw_cost_cad
FROM keyed
"""


def copy_query(query: str, destination: Path, order_by: str | None = None) -> str:
    ordered = f"SELECT * FROM ({query}) q"
    if order_by:
        ordered += f" ORDER BY {order_by}"
    return (
        f"COPY ({ordered}) TO {literal(destination)} "
        "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)"
    )
