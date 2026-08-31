"""Isolated, non-endorsed reproduction of the supplied analyst materials."""

from __future__ import annotations

import csv
import hashlib
import math
from pathlib import Path
from typing import Any

import duckdb

from barnabus.sql import case_id_expression, literal


REVIEWED_QUERY = """SELECT
  CASE WHEN e.event_ts >= '2026-05-01' THEN 'after' ELSE 'before' END AS period,
  COUNT(DISTINCT e.case_id)                            AS cases,
  AVG(CASE WHEN s.cancelled THEN 1.0 ELSE 0.0 END)     AS cancellation_rate,
  AVG(s.readiness_days)                                AS mean_readiness_days
FROM events e
JOIN snapshot_cases s ON s.case_ref = e.case_id
WHERE e.event_type = 'assessment_generated'
GROUP BY 1"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _period_metrics(rows: list[tuple[Any, ...]]) -> dict[str, dict[str, float]]:
    return {
        str(period): {
            "cases": float(cases),
            "cancellation_rate": float(cancellation_rate),
            "mean_readiness_days": float(mean_readiness_days),
        }
        for period, cases, cancellation_rate, mean_readiness_days in rows
    }


def _derived(periods: dict[str, dict[str, float]]) -> dict[str, float]:
    before = periods["before"]
    after = periods["after"]
    cancellation_reduction = (
        100.0
        * (before["cancellation_rate"] - after["cancellation_rate"])
        / before["cancellation_rate"]
    )
    readiness_reduction = before["mean_readiness_days"] - after["mean_readiness_days"]
    return {
        "cancellation_reduction_pct": cancellation_reduction,
        "readiness_time_reduction_days": readiness_reduction,
    }


def _stage_metrics(connection: duckdb.DuckDBPyConnection, sql: str) -> dict[str, float]:
    row = connection.execute(sql).fetchone()
    if row is None:
        raise ValueError("analyst audit stage returned no row")
    before_cancel, after_cancel, before_ready, after_ready = map(float, row)
    return {
        "cancellation_reduction_pct": 100.0 * (before_cancel - after_cancel) / before_cancel,
        "readiness_time_reduction_days": before_ready - after_ready,
    }


def reproduce_analyst_materials(
    connection: duckdb.DuckDBPyConnection,
    data_root: Path,
    workflow_path: Path,
    expected_query_sha256: str,
) -> dict[str, Any]:
    """Validate the untrusted SQL fingerprint, then run the reviewed literal query."""

    supplied_query_path = data_root / "analyst_query.sql"
    observed_query_sha = sha256_file(supplied_query_path)
    if observed_query_sha != expected_query_sha256:
        raise ValueError(
            "supplied analyst SQL fingerprint changed; arbitrary source SQL will not be executed"
        )
    event_glob = (data_root / "events" / "ingest_month=*.parquet").as_posix()
    connection.execute(
        f"CREATE OR REPLACE TEMP VIEW events AS "
        f"SELECT * FROM read_parquet({literal(event_glob)}, union_by_name=true)"
    )
    connection.execute(
        f"CREATE OR REPLACE TEMP VIEW snapshot_cases AS "
        f"SELECT * FROM read_csv_auto({literal(data_root / 'snapshot_cases.csv')}, header=true)"
    )
    exact_rows = connection.execute(REVIEWED_QUERY + " ORDER BY 1").fetchall()
    exact_periods = _period_metrics(exact_rows)
    exact_derived = _derived(exact_periods)

    labels = connection.execute(
        f"SELECT count(*), count(*) FILTER (WHERE upper(trim(recommendation)) = "
        f"upper(trim(clinician_action))) FROM read_csv_auto("
        f"{literal(data_root / 'labels_pairs.csv')}, header=true)"
    ).fetchone()
    if labels is None:
        raise ValueError("labels reproduction returned no row")
    label_agreement_pct = 100.0 * int(labels[1]) / int(labels[0])

    with (data_root / "analyst_deck_numbers.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        supplied_deck = list(csv.DictReader(handle))
    reproduced_values: dict[str, float | None] = {
        **exact_derived,
        "clinician_agreement_pct": label_agreement_pct,
        "clinician_satisfaction_pct": None,
    }
    deck_rows: list[dict[str, Any]] = []
    for row in supplied_deck:
        metric = row["metric"]
        supplied = float(row["value"])
        reproduced = reproduced_values.get(metric)
        if reproduced is None:
            status = "unreproduced"
        elif math.isclose(supplied, reproduced, rel_tol=0.0, abs_tol=0.051):
            status = "reproduced"
        else:
            status = "unreproduced"
        deck_rows.append(
            {
                "metric": metric,
                "supplied_value": supplied,
                "reproduced_value": reproduced,
                "supplied_source": row["source"],
                "reproduction_status": status,
                "quantity_status": "reproduced" if status == "reproduced" else "unreproduced",
                "supplied_value_quantity_status": "reproduced" if status == "reproduced" else "unreproduced",
                "reproduced_value_quantity_status": "reproduced" if reproduced is not None else "not_estimated",
            }
        )

    normalized_case = case_id_expression("case_ref")
    snapshot_path = literal(data_root / "snapshot_cases.csv")
    workflow = literal(workflow_path)
    connection.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW audit_snapshot_rows AS
        SELECT *, {normalized_case} AS case_id_norm,
               try_cast(referral_ts AS DATE) AS referral_date,
               try_cast(cancelled AS BOOLEAN) AS cancelled_bool,
               try_cast(readiness_days AS DOUBLE) AS readiness_value
        FROM read_csv_auto({snapshot_path}, all_varchar=true, header=true)
        """
    )
    connection.execute(
        """
        CREATE OR REPLACE TEMP VIEW audit_snapshot_one AS
        SELECT case_id_norm AS case_id,
               count(*) snapshot_rows,
               CASE WHEN count(DISTINCT cancelled_bool)=1 THEN min(cancelled_bool) END cancelled,
               CASE WHEN count(DISTINCT readiness_value) FILTER (WHERE readiness_value IS NOT NULL)<=1
                    THEN min(readiness_value) END readiness_days
        FROM audit_snapshot_rows GROUP BY 1
        """
    )
    connection.execute(
        f"CREATE OR REPLACE TEMP VIEW audit_workflow AS SELECT * FROM read_parquet({workflow})"
    )

    diagnostics_query = """
    WITH assessment AS (
      SELECT DISTINCT case_id,
             min(try_cast(event_ts AS TIMESTAMP)) event_ts
      FROM events WHERE event_type='assessment_generated' GROUP BY 1
    ), joined AS (
      SELECT w.case_id, w.referral_ts_local, w.assessment_ts_utc,
             s.snapshot_rows, s.cancelled, s.readiness_days
      FROM audit_workflow w LEFT JOIN audit_snapshot_one s USING(case_id)
      WHERE w.referral_study_status='in_study'
    )
    SELECT
      (SELECT count(*) FROM audit_snapshot_rows) snapshot_rows,
      (SELECT count(*) FROM audit_snapshot_rows WHERE trim(case_ref) NOT LIKE 'C%') normalized_only_key_rows,
      (SELECT count(*) FROM audit_snapshot_one WHERE snapshot_rows>1) duplicate_snapshot_keys,
      count(*) eligible_referrals,
      count(*) FILTER (WHERE assessment_ts_utc IS NULL) excluded_without_assessment,
      count(*) FILTER (WHERE cast(referral_ts_local AS DATE) < DATE '2026-05-01'
                        AND cast(assessment_ts_utc AS DATE) >= DATE '2026-05-01') crossed_switch_after_assessment,
      count(*) FILTER (WHERE cancelled IS NULL) unresolved_cancellation_rows,
      count(*) FILTER (WHERE readiness_days IS NULL) missing_readiness_rows
    FROM joined
    """
    diagnostics = connection.execute(diagnostics_query).fetchone()
    diagnostic_names = [description[0] for description in connection.description]
    diagnostics_row = dict(zip(diagnostic_names, diagnostics, strict=True))

    stages: list[dict[str, Any]] = []
    raw_metrics = exact_derived
    stages.append(
        {
            "stage": "00_exact_supplied_query",
            **raw_metrics,
            "quantity_status": "reproduced",
            "interpretation": "exact_nonendorsed_reproduction",
        }
    )

    normalized_dedup = _stage_metrics(
        connection,
        """
        WITH cohort AS (
          SELECT CASE WHEN try_cast(e.event_ts AS TIMESTAMP) >= TIMESTAMP '2026-05-01'
                      THEN 'after' ELSE 'before' END period,
                 s.cancelled, s.readiness_days
          FROM events e JOIN audit_snapshot_one s ON s.case_id=e.case_id
          WHERE e.event_type='assessment_generated'
        )
        SELECT avg(cancelled::INTEGER) FILTER(WHERE period='before'),
               avg(cancelled::INTEGER) FILTER(WHERE period='after'),
               avg(readiness_days) FILTER(WHERE period='before'),
               avg(readiness_days) FILTER(WHERE period='after') FROM cohort
        """,
    )
    stages.append(
        {
            "stage": "01_normalized_join_and_deduplicated_snapshot",
            **normalized_dedup,
            "quantity_status": "sensitivity_only",
            "interpretation": "mechanical_key_and_multiplicity_correction",
        }
    )

    referral_all = _stage_metrics(
        connection,
        """
        WITH cohort AS (
          SELECT CASE WHEN cast(w.referral_ts_local AS DATE) >= DATE '2026-05-01'
                      THEN 'after' ELSE 'before' END period,
                 s.cancelled, s.readiness_days
          FROM audit_workflow w JOIN audit_snapshot_one s USING(case_id)
          WHERE w.referral_study_status='in_study'
        )
        SELECT avg(cancelled::INTEGER) FILTER(WHERE period='before'),
               avg(cancelled::INTEGER) FILTER(WHERE period='after'),
               avg(readiness_days) FILTER(WHERE period='before'),
               avg(readiness_days) FILTER(WHERE period='after') FROM cohort
        """,
    )
    stages.append(
        {
            "stage": "02_referral_time_zero_and_all_referrals",
            **referral_all,
            "quantity_status": "sensitivity_only",
            "interpretation": "removes_survival_to_assessment_and_realigns_time_zero",
        }
    )

    site_a = _stage_metrics(
        connection,
        """
        WITH cohort AS (
          SELECT CASE WHEN cast(w.referral_ts_local AS DATE) >= DATE '2026-05-01'
                      THEN 'after' ELSE 'before' END period,
                 s.cancelled, s.readiness_days
          FROM audit_workflow w JOIN audit_snapshot_one s USING(case_id)
          WHERE w.referral_study_status='in_study' AND w.site='A'
        )
        SELECT avg(cancelled::INTEGER) FILTER(WHERE period='before'),
               avg(cancelled::INTEGER) FILTER(WHERE period='after'),
               avg(readiness_days) FILTER(WHERE period='before'),
               avg(readiness_days) FILTER(WHERE period='after') FROM cohort
        """,
    )
    stages.append(
        {
            "stage": "03_site_a_only_not_selected_services",
            **site_a,
            "quantity_status": "sensitivity_only",
            "interpretation": "removes_silent_site_b_mixing_but_not_service_selection",
        }
    )

    equal_service = _stage_metrics(
        connection,
        """
        WITH cells AS (
          SELECT w.service_code,
                 CASE WHEN cast(w.referral_ts_local AS DATE) >= DATE '2026-05-01'
                      THEN 'after' ELSE 'before' END period,
                 avg(s.cancelled::INTEGER) cancel_rate,
                 avg(s.readiness_days) ready_mean
          FROM audit_workflow w JOIN audit_snapshot_one s USING(case_id)
          WHERE w.referral_study_status='in_study' AND w.site='A'
          GROUP BY 1,2
        )
        SELECT avg(cancel_rate) FILTER(WHERE period='before'),
               avg(cancel_rate) FILTER(WHERE period='after'),
               avg(ready_mean) FILTER(WHERE period='before'),
               avg(ready_mean) FILTER(WHERE period='after') FROM cells
        """,
    )
    stages.append(
        {
            "stage": "04_equal_service_weighting_all_site_a",
            **equal_service,
            "quantity_status": "sensitivity_only",
            "interpretation": "changes_case_volume_weighting_but_exposure_still_unknown",
        }
    )

    for index, stage in enumerate(stages):
        if index == 0:
            stage["delta_cancellation_pct_from_prior"] = None
            stage["delta_readiness_days_from_prior"] = None
        else:
            prior = stages[index - 1]
            stage["delta_cancellation_pct_from_prior"] = (
                stage["cancellation_reduction_pct"] - prior["cancellation_reduction_pct"]
            )
            stage["delta_readiness_days_from_prior"] = (
                stage["readiness_time_reduction_days"] - prior["readiness_time_reduction_days"]
            )
        stage["contribution_scope"] = "order_dependent_descriptive_not_causal"

    return {
        "query_sha256": observed_query_sha,
        "exact_periods": exact_periods,
        "exact_derived": exact_derived,
        "deck_rows": deck_rows,
        "diagnostics": diagnostics_row,
        "bias_audit": stages,
    }
