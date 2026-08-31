"""Reproducible claim-analysis pipeline.

All reported values are created by this module or the isolated analyst
reproduction module. No notebook is part of the path.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import yaml

from barnabus.analyst_reproduction import reproduce_analyst_materials, sha256_file
from barnabus.config import RuntimePaths
from barnabus.pipeline import run_pipeline, verify_artifacts
from barnabus.sql import case_id_expression, literal
from barnabus.stats import (
    auc,
    benjamini_yekutieli_adjust,
    binary_agreement,
    binary_metrics,
    e_value,
    finite,
    holm_adjust,
    km_rmst,
    logistic_calibration,
    mean,
    odds_shift,
    quantile,
    risk_ratio_bias_factor,
    sample_sd,
    synthetic_effect,
    webb_service_inference,
)


SCRIPT_PATH = "src/barnabus/analysis.py"
ANALYST_SCRIPT_PATH = "src/barnabus/analyst_reproduction.py"
RESULT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AnalysisPaths:
    repository_root: Path
    data_root: Path
    work_root: Path
    pipeline_output_root: Path
    result_root: Path
    execution_config: Path
    frozen_config: Path

    def validate(self) -> None:
        roots = [
            self.data_root.resolve(),
            self.work_root.resolve(),
            self.pipeline_output_root.resolve(),
            self.result_root.resolve(),
        ]
        if not self.data_root.is_dir():
            raise ValueError(f"data root does not exist: {self.data_root}")
        if any(root == roots[0] or root.is_relative_to(roots[0]) for root in roots[1:]):
            raise ValueError("work, pipeline output, and result roots must be outside data root")
        if self.result_root.resolve() == self.pipeline_output_root.resolve():
            raise ValueError("analysis results and pipeline outputs require separate roots")


@dataclass(frozen=True)
class Episode:
    case_id: str
    site: str
    service_code: str
    specialty: str
    ward_id: str
    referral_date: date
    referral_week: date
    sex: str | None
    age: int | None
    cancellation_proxy: bool
    snapshot_cancelled: bool | None
    snapshot_readiness_days: float | None
    days_to_ready: float | None
    days_to_close: float | None
    days_to_complete: float | None
    days_to_recommendation: float | None
    admin_followup_days: float
    recommendation_action_eligible: bool
    has_assessment: bool


class NumberRegistry:
    def __init__(
        self,
        input_fingerprint: str,
        config_fingerprint: str,
        implementation_commit: str,
    ) -> None:
        self.input_fingerprint = input_fingerprint
        self.config_fingerprint = config_fingerprint
        self.implementation_commit = implementation_commit
        self.rows: list[dict[str, Any]] = []
        self._ids: set[str] = set()

    def add(
        self,
        number_id: str,
        value: float | int | None,
        *,
        unit: str,
        script: str = SCRIPT_PATH,
        table: str,
        quantity_status: str,
        assumptions: str = "",
    ) -> float | int | None:
        if number_id in self._ids:
            raise ValueError(f"duplicate number_id: {number_id}")
        self._ids.add(number_id)
        numeric = value
        if isinstance(numeric, float) and not math.isfinite(numeric):
            numeric = None
        self.rows.append(
            {
                "number_id": number_id,
                "value": numeric,
                "unit": unit,
                "script": script,
                "table": table,
                "input_fingerprint": self.input_fingerprint,
                "config_fingerprint": self.config_fingerprint,
                "implementation_commit": self.implementation_commit,
                "quantity_status": quantity_status,
                "assumptions": assumptions,
            }
        )
        return numeric


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode(
        "utf-8"
    )


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _write_json(path: Path, value: object) -> None:
    _write_bytes(path, _json_bytes(value))


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})
    os.replace(temporary, path)


def _sha256_payload(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _tree_hash(paths: Sequence[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _implementation_commit(repository_root: Path) -> str:
    environment_value = os.environ.get("BARNABUS_ANALYSIS_IMPLEMENTATION_COMMIT", "").strip()
    if len(environment_value) == 40 and all(
        character in "0123456789abcdef" for character in environment_value
    ):
        return environment_value
    identity_file = repository_root / "config" / "analysis-implementation-commit.txt"
    if identity_file.is_file():
        value = identity_file.read_text(encoding="ascii").strip()
        if len(value) == 40 and all(character in "0123456789abcdef" for character in value):
            return value
    command = [
        "git",
        "log",
        "-1",
        "--format=%H",
        "--",
        SCRIPT_PATH,
        ANALYST_SCRIPT_PATH,
        "src/barnabus/stats.py",
        "config/analysis-execution-v1.yaml",
        "analyst_reproduction/reviewed_query.sql",
    ]
    value = subprocess.check_output(command, cwd=repository_root, text=True).strip()
    if len(value) != 40:
        raise ValueError("could not determine analysis implementation commit")
    return value


def _age_band(age: int | None) -> str:
    if age is None:
        return "unknown"
    if age < 40:
        return "age_lt_40"
    if age < 65:
        return "age_40_64"
    return "age_ge_65"


def _day_difference(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds() / 86400.0


def _load_episodes(
    connection: duckdb.DuckDBPyConnection, data_root: Path, artifact_directory: Path, study_end: date
) -> list[Episode]:
    workflow_path = artifact_directory / "case_workflow.parquet"
    normalized_case = case_id_expression("case_ref")
    connection.execute(
        f"CREATE OR REPLACE TEMP VIEW analysis_workflow AS SELECT * FROM read_parquet("
        f"{literal(workflow_path)})"
    )
    connection.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW analysis_snapshot AS
        WITH source AS (
          SELECT {normalized_case} case_id,
                 try_cast(cancelled AS BOOLEAN) cancelled,
                 try_cast(readiness_days AS DOUBLE) readiness_days
          FROM read_csv_auto({literal(data_root / 'snapshot_cases.csv')},
                             all_varchar=true, header=true)
        )
        SELECT case_id,
               CASE WHEN count(DISTINCT cancelled)=1 THEN min(cancelled) END cancelled,
               CASE WHEN count(DISTINCT readiness_days) FILTER(WHERE readiness_days IS NOT NULL)<=1
                    THEN min(readiness_days) END readiness_days,
               count(*) source_rows,
               count(DISTINCT cancelled) cancelled_value_count,
               count(DISTINCT readiness_days) FILTER(WHERE readiness_days IS NOT NULL)
                 readiness_value_count
        FROM source GROUP BY case_id
        """
    )
    connection.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW analysis_patients AS
        SELECT 'C' || substr(upper(trim(patient_id)), 3) case_id,
               try_cast(dob AS DATE) dob,
               upper(trim(sex)) sex,
               upper(trim(site)) patient_site
        FROM read_csv_auto({literal(data_root / 'patients.csv')},
                           all_varchar=true, header=true)
        """
    )
    rows = connection.execute(
        """
        SELECT w.case_id, w.site, w.service_code, substr(w.service_code, 3) specialty,
               w.referral_ward_id, cast(w.referral_ts_local AS DATE) referral_date,
               cast(date_trunc('week', w.referral_ts_local) AS DATE) referral_week,
               p.sex,
               date_diff('year', p.dob, cast(w.referral_ts_local AS DATE))
                 - CASE WHEN (month(cast(w.referral_ts_local AS DATE)),
                                  day(cast(w.referral_ts_local AS DATE)))
                               < (month(p.dob), day(p.dob)) THEN 1 ELSE 0 END age,
               w.cancellation_derived, s.cancelled, s.readiness_days,
               w.referral_ts_utc, w.readiness_ts_utc, w.closed_ts_utc,
               w.completed_ts_utc, w.recommendation_ts_utc, w.action_ts_utc,
               w.assessment_ts_utc
        FROM analysis_workflow w
        LEFT JOIN analysis_snapshot s USING(case_id)
        LEFT JOIN analysis_patients p USING(case_id)
        WHERE w.referral_study_status='in_study'
        ORDER BY w.case_id
        """
    ).fetchall()
    episodes: list[Episode] = []
    for row in rows:
        (
            case_id,
            site,
            service_code,
            specialty,
            ward_id,
            referral_date,
            referral_week,
            sex,
            age,
            cancellation_derived,
            snapshot_cancelled,
            snapshot_readiness,
            referral_ts,
            readiness_ts,
            closed_ts,
            completed_ts,
            recommendation_ts,
            action_ts,
            assessment_ts,
        ) = row
        admin = min(90.0, max(0.0, float((study_end - referral_date).days) + 1.0))
        eligible_pair = (
            recommendation_ts is not None
            and action_ts is not None
            and 0.0 <= (action_ts - recommendation_ts).total_seconds() <= 7 * 86400
        )
        episodes.append(
            Episode(
                case_id=case_id,
                site=site,
                service_code=service_code,
                specialty=specialty,
                ward_id=ward_id,
                referral_date=referral_date,
                referral_week=referral_week,
                sex=sex,
                age=int(age) if age is not None else None,
                cancellation_proxy=bool(cancellation_derived),
                snapshot_cancelled=snapshot_cancelled,
                snapshot_readiness_days=(
                    float(snapshot_readiness) if snapshot_readiness is not None else None
                ),
                days_to_ready=_day_difference(readiness_ts, referral_ts),
                days_to_close=_day_difference(closed_ts, referral_ts),
                days_to_complete=_day_difference(completed_ts, referral_ts),
                days_to_recommendation=_day_difference(recommendation_ts, referral_ts),
                admin_followup_days=admin,
                recommendation_action_eligible=eligible_pair,
                has_assessment=assessment_ts is not None,
            )
        )
    return episodes


def _c1_observation(episode: Episode, horizon: float = 90.0) -> tuple[bool, int | None]:
    cancellation_in_window = (
        episode.cancellation_proxy
        and episode.days_to_close is not None
        and 0.0 <= episode.days_to_close <= horizon
        and episode.days_to_close <= episode.admin_followup_days
    )
    completed_in_window = (
        episode.days_to_complete is not None
        and 0.0 <= episode.days_to_complete <= horizon
        and episode.days_to_complete <= episode.admin_followup_days
    )
    complete_followup = episode.admin_followup_days >= horizon
    observed = cancellation_in_window or completed_in_window or complete_followup
    return observed, int(cancellation_in_window) if observed else None


def _c2_observation(episode: Episode, horizon: float = 90.0) -> tuple[float, bool, bool]:
    followup = min(horizon, episode.admin_followup_days)
    ready = episode.days_to_ready
    cancel = episode.days_to_close if episode.cancellation_proxy else None
    if (
        ready is not None
        and 0.0 <= ready <= followup
        and ready <= horizon
        and (cancel is None or ready <= cancel)
    ):
        return ready, True, False
    if cancel is not None and 0.0 <= cancel <= followup and cancel <= horizon:
        return horizon, False, True
    if followup >= horizon:
        return horizon, False, False
    return followup, False, False


def _poststratified_binary_rate(
    episodes: Sequence[Episode], horizon: float = 90.0
) -> tuple[float, float, int, int]:
    if not episodes:
        return math.nan, math.nan, 0, 0
    strata: dict[tuple[str, str, int], list[tuple[bool, int | None]]] = defaultdict(list)
    all_observed: list[int] = []
    for episode in episodes:
        observed, outcome = _c1_observation(episode, horizon)
        key = (
            episode.sex or "unknown",
            _age_band(episode.age),
            episode.referral_date.month,
        )
        strata[key].append((observed, outcome))
        if observed and outcome is not None:
            all_observed.append(outcome)
    if not all_observed:
        return math.nan, 1.0, 0, len(episodes)
    fallback = mean([float(value) for value in all_observed])
    weighted_total = 0.0
    observed_count = 0
    for values in strata.values():
        observed_values = [outcome for observed, outcome in values if observed and outcome is not None]
        observed_count += len(observed_values)
        cell_rate = (
            mean([float(value) for value in observed_values]) if observed_values else fallback
        )
        weighted_total += len(values) * cell_rate
    return (
        weighted_total / len(episodes),
        1.0 - observed_count / len(episodes),
        observed_count,
        len(episodes),
    )


def _rmst(episodes: Sequence[Episode], horizon: float = 90.0) -> tuple[float, float, int]:
    records: list[tuple[float, bool]] = []
    censored = 0
    for episode in episodes:
        time, event, terminal_cancel = _c2_observation(episode, horizon)
        records.append((time, event))
        if not event and not terminal_cancel and time < horizon:
            censored += 1
    return km_rmst(records, horizon), censored / len(records) if records else math.nan, len(records)


def _service_effects(
    episodes: Sequence[Episode],
    switch: date,
    outcome: str,
    horizon: float = 90.0,
    predicate: Callable[[Episode], bool] | None = None,
) -> list[dict[str, Any]]:
    selected = [episode for episode in episodes if predicate is None or predicate(episode)]
    specialties = sorted({episode.specialty for episode in selected if episode.site == "A"})
    results: list[dict[str, Any]] = []
    for specialty in specialties:
        cells: dict[tuple[str, str], list[Episode]] = {}
        for site in ("A", "B"):
            for period in ("before", "after"):
                cells[(site, period)] = [
                    episode
                    for episode in selected
                    if episode.site == site
                    and episode.specialty == specialty
                    and ((episode.referral_date >= switch) == (period == "after"))
                ]
        if any(not value for value in cells.values()):
            continue
        estimates: dict[tuple[str, str], tuple[float, float, int, int]] = {}
        if outcome == "c1":
            for key, values in cells.items():
                rate, missing, observed, total = _poststratified_binary_rate(values, horizon)
                estimates[key] = (rate, missing, observed, total)
        elif outcome == "c2":
            for key, values in cells.items():
                rmst, missing, total = _rmst(values, horizon)
                estimates[key] = (rmst, missing, total - round(missing * total), total)
        else:
            raise ValueError(f"unknown outcome: {outcome}")
        effect = (
            estimates[("A", "after")][0]
            - estimates[("A", "before")][0]
            - estimates[("B", "after")][0]
            + estimates[("B", "before")][0]
        )
        results.append(
            {
                "specialty": specialty,
                "effect": effect,
                "a_before": estimates[("A", "before")][0],
                "a_after": estimates[("A", "after")][0],
                "b_before": estimates[("B", "before")][0],
                "b_after": estimates[("B", "after")][0],
                "a_after_missing_fraction": estimates[("A", "after")][1],
                "b_after_missing_fraction": estimates[("B", "after")][1],
                "a_after_n": estimates[("A", "after")][3],
                "b_after_n": estimates[("B", "after")][3],
            }
        )
    return results


def _week_panel(
    episodes: Sequence[Episode],
    outcome: str,
    switch: date,
    horizon: float = 90.0,
    predicate: Callable[[Episode], bool] | None = None,
) -> tuple[list[float], list[float], list[list[float]], list[list[float]], list[str]]:
    selected = [episode for episode in episodes if predicate is None or predicate(episode)]
    specialties = sorted({episode.specialty for episode in selected if episode.site == "B"})
    weeks = sorted({episode.referral_week for episode in selected})
    values: dict[tuple[str, str, date], float] = {}
    for site in ("A", "B"):
        for specialty in specialties:
            service_rows = [
                episode
                for episode in selected
                if episode.site == site and episode.specialty == specialty
            ]
            fallback = (
                _poststratified_binary_rate(service_rows, horizon)[0]
                if outcome == "c1"
                else _rmst(service_rows, horizon)[0]
            )
            for week in weeks:
                group = [episode for episode in service_rows if episode.referral_week == week]
                if not group:
                    values[(site, specialty, week)] = fallback
                elif outcome == "c1":
                    values[(site, specialty, week)] = _poststratified_binary_rate(
                        group, horizon
                    )[0]
                else:
                    values[(site, specialty, week)] = _rmst(group, horizon)[0]
    complete_weeks = [
        week
        for week in weeks
        if all(math.isfinite(values[(site, specialty, week)]) for site in ("A", "B") for specialty in specialties)
    ]
    pre_weeks = [week for week in complete_weeks if week < switch]
    post_weeks = [week for week in complete_weeks if week >= switch]
    target_pre = [mean([values[("A", specialty, week)] for specialty in specialties]) for week in pre_weeks]
    target_post = [mean([values[("A", specialty, week)] for specialty in specialties]) for week in post_weeks]
    donor_pre = [[values[("B", specialty, week)] for week in pre_weeks] for specialty in specialties]
    donor_post = [[values[("B", specialty, week)] for week in post_weeks] for specialty in specialties]
    return target_pre, target_post, donor_pre, donor_post, specialties


def _synthetic_result(
    episodes: Sequence[Episode],
    outcome: str,
    switch: date,
    ridge: float,
    iterations: int,
    horizon: float = 90.0,
    predicate: Callable[[Episode], bool] | None = None,
) -> dict[str, Any]:
    target_pre, target_post, donor_pre, donor_post, specialties = _week_panel(
        episodes, outcome, switch, horizon, predicate
    )
    effect, weights, pre_rmse = synthetic_effect(
        target_pre, target_post, donor_pre, donor_post, ridge, iterations
    )
    placebo_effects: list[float] = []
    for index in range(len(specialties)):
        placebo_pre = donor_pre[index]
        placebo_post = donor_post[index]
        comparison_pre = [row for j, row in enumerate(donor_pre) if j != index]
        comparison_post = [row for j, row in enumerate(donor_post) if j != index]
        placebo, _, _ = synthetic_effect(
            placebo_pre, placebo_post, comparison_pre, comparison_post, ridge, iterations
        )
        placebo_effects.append(placebo)
    p_value = (1 + sum(abs(value) >= abs(effect) for value in placebo_effects)) / (
        len(placebo_effects) + 1
    )
    return {
        "estimate": effect,
        "lower": min(placebo_effects),
        "upper": max(placebo_effects),
        "p_value": p_value,
        "pre_rmse": pre_rmse,
        "donor_weights": dict(zip(specialties, weights, strict=True)),
        "placebo_effects": placebo_effects,
        "placebo_count": len(placebo_effects),
        "pre_weeks": len(target_pre),
        "post_weeks": len(target_post),
    }


def _coverage_components(
    connection: duckdb.DuckDBPyConnection, artifact_directory: Path, switch: date
) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT site, clinician_id, list_sort(list_distinct(list(service_code))) services
        FROM read_parquet(?)
        WHERE cast(event_ts_local AS DATE) < ? AND is_workflow_event
        GROUP BY 1,2 ORDER BY 1,2
        """,
        [str(artifact_directory / "canonical_events.parquet"), switch],
    ).fetchall()
    services = sorted({service for _, _, service_list in rows for service in service_list})
    parent = {service: service for service in services}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for _, _, service_list in rows:
        for service in service_list[1:]:
            union(service_list[0], service)
    components: dict[str, list[str]] = defaultdict(list)
    for service in services:
        components[find(service)].append(service)
    site_components: dict[str, int] = {}
    for site in ("A", "B"):
        site_components[site] = len(
            {find(service) for service in services if service.startswith(site + "-")}
        )
    return {
        "total_services": len(services),
        "total_components": len(components),
        "site_a_components": site_components["A"],
        "site_b_components": site_components["B"],
        "components": [sorted(value) for _, value in sorted(components.items())],
    }


def _label_analysis(
    connection: duckdb.DuckDBPyConnection,
    data_root: Path,
    episodes: Sequence[Episode],
    switch: date,
    draws: int,
    seed: int,
) -> dict[str, Any]:
    episode_index = {episode.case_id: episode for episode in episodes}
    raw_rows = connection.execute(
        f"""
        SELECT pair_id, case_id, upper(trim(recommendation)),
               upper(trim(clinician_action)), try_cast(reviewed_in_detail AS BOOLEAN)
        FROM read_csv_auto({literal(data_root / 'labels_pairs.csv')},
                           all_varchar=true, header=true)
        ORDER BY pair_id
        """
    ).fetchall()
    labeled: list[dict[str, Any]] = []
    for pair_id, case_id, recommendation, action, reviewed in raw_rows:
        episode = episode_index.get(case_id)
        if episode is None:
            continue
        labeled.append(
            {
                "pair_id": pair_id,
                "case_id": case_id,
                "recommendation": recommendation,
                "action": action,
                "reviewed": bool(reviewed),
                "site": episode.site,
                "service_code": episode.service_code,
                "specialty": episode.specialty,
                "post": episode.referral_date >= switch,
                "sex": episode.sex,
                "age_band": _age_band(episode.age),
                "eligible_event_pair": episode.recommendation_action_eligible,
            }
        )

    universe = [
        episode
        for episode in episodes
        if episode.site == "A"
        and episode.referral_date >= switch
        and episode.recommendation_action_eligible
    ]
    target_labels = [
        row
        for row in labeled
        if row["site"] == "A" and row["post"] and row["eligible_event_pair"]
    ]
    universe_by_service: dict[str, int] = defaultdict(int)
    label_by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for episode in universe:
        universe_by_service[episode.service_code] += 1
    for row in target_labels:
        label_by_service[row["service_code"]].append(row)
    weighted_total = 0.0
    weighted_agree = 0.0
    weighted_rec_positive = 0.0
    weighted_action_positive = 0.0
    service_metrics: list[dict[str, Any]] = []
    for service in sorted(universe_by_service):
        rows = label_by_service.get(service, [])
        if not rows:
            continue
        weight = universe_by_service[service] / len(rows)
        agree = sum(row["recommendation"] == row["action"] for row in rows)
        rec_positive = sum(row["recommendation"] == "PROCEED" for row in rows)
        action_positive = sum(row["action"] == "PROCEED" for row in rows)
        weighted_total += weight * len(rows)
        weighted_agree += weight * agree
        weighted_rec_positive += weight * rec_positive
        weighted_action_positive += weight * action_positive
        service_metrics.append(
            {
                "service_code": service,
                "labeled_n": len(rows),
                "eligible_universe_n": universe_by_service[service],
                "agreement": agree / len(rows),
                "recommendation_positive": rec_positive / len(rows),
                "action_positive": action_positive / len(rows),
            }
        )
    if not weighted_total:
        raise ValueError("no labeled pairs overlap the agreement sensitivity population")
    # Frozen primary weighting is equal service. The inverse selection weights
    # reconstruct each service; services are then averaged equally.
    metrics = binary_agreement(
        mean([row["recommendation_positive"] for row in service_metrics]),
        mean([row["action_positive"] for row in service_metrics]),
        mean([row["agreement"] for row in service_metrics]),
    )
    for row in service_metrics:
        local = binary_agreement(
            row["recommendation_positive"], row["action_positive"], row["agreement"]
        )
        row.update(local)
    generator = random.Random(seed + 301)
    chance_draws: list[float] = []
    raw_margin_draws: list[float] = []
    for _ in range(draws):
        sampled = [service_metrics[generator.randrange(len(service_metrics))] for _ in service_metrics]
        draw_metrics = binary_agreement(
            mean([row["recommendation_positive"] for row in sampled]),
            mean([row["action_positive"] for row in sampled]),
            mean([row["agreement"] for row in sampled]),
        )
        chance_draws.append(draw_metrics["chance_excess"])
        raw_margin_draws.append(draw_metrics["raw_agreement"] - 0.80)
    chance_centered = [value - metrics["chance_excess"] for value in chance_draws]
    raw_centered = [value - (metrics["raw_agreement"] - 0.80) for value in raw_margin_draws]
    chance_p = 2.0 * min(
        sum(value <= 0 for value in chance_draws) / draws,
        sum(value >= 0 for value in chance_draws) / draws,
    )
    raw_p = 2.0 * min(
        sum(value <= 0 for value in raw_margin_draws) / draws,
        sum(value >= 0 for value in raw_margin_draws) / draws,
    )

    all_rec = mean([float(row["recommendation"] == "PROCEED") for row in labeled])
    all_action = mean([float(row["action"] == "PROCEED") for row in labeled])
    all_agree = mean([float(row["recommendation"] == row["action"]) for row in labeled])
    all_metrics = binary_agreement(all_rec, all_action, all_agree)
    return {
        "approach_a": {
            **metrics,
            "raw_margin_over_0_80": metrics["raw_agreement"] - 0.80,
            "chance_lower": metrics["chance_excess"] - quantile(chance_centered, 0.975),
            "chance_upper": metrics["chance_excess"] - quantile(chance_centered, 0.025),
            "chance_p_value": min(chance_p, 1.0),
            "raw_margin_lower": metrics["raw_agreement"] - 0.80 - quantile(raw_centered, 0.975),
            "raw_margin_upper": metrics["raw_agreement"] - 0.80 - quantile(raw_centered, 0.025),
            "raw_margin_p_value": min(raw_p, 1.0),
            "labeled_n": len(target_labels),
            "eligible_universe_n": len(universe),
            "services_with_labels": len(service_metrics),
        },
        "all_supplied_pairs": {**all_metrics, "n": len(labeled)},
        "service_metrics": service_metrics,
        "labeled_rows": labeled,
        "universe_by_service": dict(universe_by_service),
    }


def _legacy_comparison_rows(
    connection: duckdb.DuckDBPyConnection, data_root: Path, workflow_path: Path
) -> list[dict[str, Any]]:
    normalized_case = case_id_expression("s.case_ref")
    rows = connection.execute(
        f"""
        WITH patients AS (
          SELECT 'C' || substr(upper(trim(patient_id)),3) case_id,
                 upper(trim(sex)) sex
          FROM read_csv_auto({literal(data_root / 'patients.csv')},
                             all_varchar=true, header=true)
        ), workflow AS (
          SELECT case_id, referral_ward_id ward_id FROM read_parquet({literal(workflow_path)})
        )
        SELECT upper(trim(s.site)) site, upper(trim(s.service_code)) service_code,
               try_cast(s.referral_ts AS DATE) referral_date,
               try_cast(s.patient_age AS INTEGER) age,
               try_cast(s.cancelled AS BOOLEAN) cancelled,
               p.sex, w.ward_id
        FROM read_csv_auto({literal(data_root / 'snapshot_cases.csv')},
                           all_varchar=true, header=true) s
        LEFT JOIN patients p ON p.case_id={normalized_case}
        LEFT JOIN workflow w ON w.case_id={normalized_case}
        ORDER BY referral_date, service_code
        """
    ).fetchall()
    source_rows = connection.execute(
        f"SELECT * FROM read_csv_auto({literal(data_root / 'comparisons_log.csv')}, "
        "all_varchar=true, header=true)"
    ).fetchall()

    def select(comparison: str, row: tuple[Any, ...]) -> bool:
        site, service, referral, age, _, sex, ward = row
        parts = comparison.split("|")
        for part in parts:
            if part.startswith("service:") and service != part.split(":", 1)[1]:
                return False
            if part.startswith("site:") and site != part.split(":", 1)[1]:
                return False
            if part.startswith("ward:") and ward != part.split(":", 1)[1]:
                return False
            if part.startswith("sex:") and sex != part.split(":", 1)[1]:
                return False
            if part == "age<50" and not (age is not None and age < 50):
                return False
            if part == "age50-70" and not (age is not None and 50 <= age <= 70):
                return False
            if part == "age>70" and not (age is not None and age > 70):
                return False
            if part == "weekend" and referral.weekday() < 5:
                return False
            if part == "weekday" and referral.weekday() >= 5:
                return False
        return True

    results: list[dict[str, Any]] = []
    for comparison, supplied_n_before, supplied_n_after, supplied_diff, supplied_p, in_deck in source_rows:
        selected = [row for row in rows if select(comparison, row)]
        before = [int(row[4]) for row in selected if row[2] < date(2026, 5, 1)]
        after = [int(row[4]) for row in selected if row[2] >= date(2026, 5, 1)]
        p_before = mean([float(value) for value in before])
        p_after = mean([float(value) for value in after])
        difference = p_after - p_before
        pooled = (sum(before) + sum(after)) / (len(before) + len(after))
        standard_error = math.sqrt(pooled * (1 - pooled) * (1 / len(before) + 1 / len(after)))
        z_value = difference / standard_error if standard_error else 0.0
        p_value = math.erfc(abs(z_value) / math.sqrt(2.0))
        count_matches = len(before) == int(supplied_n_before) and len(after) == int(supplied_n_after)
        diff_matches = math.isclose(difference, float(supplied_diff), abs_tol=0.000051)
        p_matches = math.isclose(p_value, float(supplied_p), abs_tol=0.000051)
        reproduced = count_matches and diff_matches and p_matches
        results.append(
            {
                "comparison": comparison,
                "n_before": len(before),
                "n_after": len(after),
                "diff": difference,
                "p_value": p_value,
                "supplied_n_before": int(supplied_n_before),
                "supplied_n_after": int(supplied_n_after),
                "supplied_diff": float(supplied_diff),
                "supplied_p_value": float(supplied_p),
                "in_deck": str(in_deck).lower() == "true",
                "count_matches": count_matches,
                "diff_matches_rounded": diff_matches,
                "p_matches_rounded": p_matches,
                "reproduction_status": "reproduced" if reproduced else "unreproduced",
                "quantity_status": "reproduced" if reproduced else "unreproduced",
            }
        )
    if len(results) != 51:
        raise ValueError(f"legacy family must contain 51 comparisons, found {len(results)}")
    adjusted = holm_adjust([float(row["p_value"]) for row in results])
    supplied_adjusted = holm_adjust([float(row["supplied_p_value"]) for row in results])
    for row, p_adjusted, supplied_p_adjusted in zip(
        results, adjusted, supplied_adjusted, strict=True
    ):
        row["p_adjusted_holm"] = p_adjusted
        row["supplied_p_adjusted_holm"] = supplied_p_adjusted
    return results


def _c1_shifted_rate(
    episodes: Sequence[Episode], multiplier: float, horizon: float = 90.0
) -> tuple[float, float]:
    observed = [_c1_observation(episode, horizon) for episode in episodes]
    observed_values = [outcome for is_observed, outcome in observed if is_observed and outcome is not None]
    if not observed_values:
        return math.nan, 1.0
    observed_rate = mean([float(value) for value in observed_values])
    missing = len(episodes) - len(observed_values)
    missing_rate = odds_shift(observed_rate, multiplier)
    combined = (sum(observed_values) + missing * missing_rate) / len(episodes)
    return combined, missing / len(episodes)


def _exponential_rmst(probability_ready: float, multiplier: float, horizon: float) -> float:
    probability_ready = min(max(probability_ready, 0.0), 1.0 - 1e-12)
    if probability_ready <= 0:
        return horizon
    hazard = -math.log(1.0 - probability_ready) / horizon
    shifted = hazard * multiplier
    if shifted <= 1e-12:
        return horizon
    return (1.0 - math.exp(-shifted * horizon)) / shifted


def _c2_shifted_rmst(
    episodes: Sequence[Episode], multiplier: float, horizon: float = 90.0
) -> tuple[float, float]:
    base, missing_fraction, _ = _rmst(episodes, horizon)
    observed_ready = 0
    observed_total = 0
    for episode in episodes:
        time, event, terminal_cancel = _c2_observation(episode, horizon)
        if event or terminal_cancel or time >= horizon:
            observed_total += 1
            observed_ready += int(event)
    probability_ready = observed_ready / observed_total if observed_total else 0.0
    shifted_missing = _exponential_rmst(probability_ready, multiplier, horizon)
    return (1.0 - missing_fraction) * base + missing_fraction * shifted_missing, missing_fraction


def _scenario_effects(
    episodes: Sequence[Episode],
    switch: date,
    outcome: str,
    treated_multiplier: float,
    control_multiplier: float,
    horizon: float = 90.0,
) -> float:
    specialties = sorted({episode.specialty for episode in episodes if episode.site == "A"})
    effects: list[float] = []
    for specialty in specialties:
        rates: dict[tuple[str, str], float] = {}
        for site in ("A", "B"):
            for period in ("before", "after"):
                group = [
                    episode
                    for episode in episodes
                    if episode.site == site
                    and episode.specialty == specialty
                    and ((episode.referral_date >= switch) == (period == "after"))
                ]
                multiplier = (
                    treated_multiplier
                    if site == "A" and period == "after"
                    else control_multiplier
                )
                if outcome == "c1":
                    rates[(site, period)] = _c1_shifted_rate(group, multiplier, horizon)[0]
                else:
                    rates[(site, period)] = _c2_shifted_rmst(group, multiplier, horizon)[0]
        effects.append(
            rates[("A", "after")]
            - rates[("A", "before")]
            - rates[("B", "after")]
            + rates[("B", "before")]
        )
    return mean(effects)


def _missingness_rows(
    episodes: Sequence[Episode],
    switch: date,
    c3: dict[str, Any],
    multipliers: Sequence[float],
    site_b_odds: Sequence[float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for outcome in ("c1", "c2"):
        for multiplier in multipliers:
            rows.append(
                {
                    "claim": outcome,
                    "mechanism": "pattern_mixture_symmetric",
                    "treated_multiplier": multiplier,
                    "control_multiplier": multiplier,
                    "estimate": _scenario_effects(
                        episodes, switch, outcome, multiplier, multiplier
                    ),
                    "quantity_status": "imputed_sensitivity_only",
                }
            )
            rows.append(
                {
                    "claim": outcome,
                    "mechanism": "directional_treated_worse_control_better",
                    "treated_multiplier": (1.0 / multiplier if outcome == "c2" else multiplier),
                    "control_multiplier": (multiplier if outcome == "c2" else 1.0 / multiplier),
                    "estimate": _scenario_effects(
                        episodes,
                        switch,
                        outcome,
                        1.0 / multiplier if outcome == "c2" else multiplier,
                        multiplier if outcome == "c2" else 1.0 / multiplier,
                    ),
                    "quantity_status": "imputed_sensitivity_only",
                }
            )
            rows.append(
                {
                    "claim": outcome,
                    "mechanism": "directional_treated_better_control_worse",
                    "treated_multiplier": (multiplier if outcome == "c2" else 1.0 / multiplier),
                    "control_multiplier": (1.0 / multiplier if outcome == "c2" else multiplier),
                    "estimate": _scenario_effects(
                        episodes,
                        switch,
                        outcome,
                        multiplier if outcome == "c2" else 1.0 / multiplier,
                        1.0 / multiplier if outcome == "c2" else multiplier,
                    ),
                    "quantity_status": "imputed_sensitivity_only",
                }
            )
        for odds in site_b_odds:
            rows.append(
                {
                    "claim": outcome,
                    "mechanism": "site_b_poor_outcome_observation_odds",
                    "treated_multiplier": 1.0,
                    "control_multiplier": odds if outcome == "c1" else 1.0 / odds,
                    "estimate": _scenario_effects(
                        episodes,
                        switch,
                        outcome,
                        1.0,
                        odds if outcome == "c1" else 1.0 / odds,
                    ),
                    "quantity_status": "imputed_sensitivity_only",
                }
            )

    c3_a = c3["approach_a"]
    observed_n = int(c3_a["labeled_n"])
    universe_n = int(c3_a["eligible_universe_n"])
    missing_n = max(0, universe_n - observed_n)
    for multiplier in multipliers:
        missing_probability = odds_shift(float(c3_a["raw_agreement"]), multiplier)
        combined = (
            observed_n * float(c3_a["raw_agreement"]) + missing_n * missing_probability
        ) / universe_n
        rows.append(
            {
                "claim": "c3",
                "mechanism": "unlabeled_pair_selection_odds",
                "treated_multiplier": multiplier,
                "control_multiplier": None,
                "estimate": combined,
                "chance_excess_estimate": combined - float(c3_a["expected_agreement"]),
                "quantity_status": "imputed_sensitivity_only",
            }
        )
    for direction, missing_probability in (
        ("directional_all_unlabeled_disagree", 0.0),
        ("directional_all_unlabeled_agree", 1.0),
    ):
        rows.append(
            {
                "claim": "c3",
                "mechanism": direction,
                "treated_multiplier": None,
                "control_multiplier": None,
                "estimate": (
                    observed_n * float(c3_a["raw_agreement"]) + missing_n * missing_probability
                )
                / universe_n,
                "chance_excess_estimate": (
                    observed_n * float(c3_a["raw_agreement"])
                    + missing_n * missing_probability
                )
                / universe_n
                - float(c3_a["expected_agreement"]),
                "quantity_status": "imputed_sensitivity_only",
            }
        )
    return rows


def _negative_controls(
    episodes: Sequence[Episode], switch: date, draws: int, seed: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, value in (
        ("patient_age_at_referral", lambda episode: float(episode.age) if episode.age is not None else math.nan),
        ("sex_distribution_female", lambda episode: float(episode.sex == "F")),
    ):
        service_effects: list[float] = []
        for specialty in sorted({episode.specialty for episode in episodes if episode.site == "A"}):
            cells: dict[tuple[str, str], list[float]] = defaultdict(list)
            for episode in episodes:
                if episode.specialty != specialty:
                    continue
                observed = value(episode)
                if math.isfinite(observed):
                    cells[(episode.site, "after" if episode.referral_date >= switch else "before")].append(observed)
            if all(cells[key] for key in (("A", "before"), ("A", "after"), ("B", "before"), ("B", "after"))):
                service_effects.append(
                    mean(cells[("A", "after")])
                    - mean(cells[("A", "before")])
                    - mean(cells[("B", "after")])
                    + mean(cells[("B", "before")])
                )
        inference = webb_service_inference(service_effects, draws, seed + len(rows) + 501)
        rows.append(
            {
                "control": name,
                "estimate": inference["estimate"],
                "lower": inference["lower"],
                "upper": inference["upper"],
                "p_value": inference["p_value"],
                "service_clusters": inference["service_clusters"],
                "quantity_status": "sensitivity_only",
            }
        )
    return rows


def _qba_rows(
    c1_effect: float,
    c1_post_risk: float,
    c2_effect: float,
    c2_service_sd: float,
    c3_chance_excess: float,
    rr_grid: Sequence[float],
    r2_grid: Sequence[float],
    selection_grid: Sequence[float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    c1_counterfactual = c1_post_risk - c1_effect
    c1_rr = c1_post_risk / c1_counterfactual if c1_counterfactual > 0 else math.nan
    rows.append(
        {
            "claim": "c1",
            "method": "e_value",
            "parameter": "observed_proxy_risk_ratio",
            "grid_value": None,
            "biased_estimate": e_value(c1_rr),
            "quantity_status": "assumed_sensitivity_only",
        }
    )
    for exposure_rr in rr_grid:
        for outcome_rr in rr_grid:
            factor = risk_ratio_bias_factor(exposure_rr, outcome_rr)
            adjusted_rr = c1_rr * factor if c1_rr < 1 else c1_rr / factor
            rows.append(
                {
                    "claim": "c1",
                    "method": "joint_risk_ratio_bias_factor",
                    "parameter": f"exposure_rr={exposure_rr};outcome_rr={outcome_rr}",
                    "grid_value": factor,
                    "biased_estimate": adjusted_rr,
                    "quantity_status": "assumed_sensitivity_only",
                }
            )
    for partial_r2 in r2_grid:
        bias_days = math.sqrt(partial_r2 / (1.0 - partial_r2)) * c2_service_sd
        rows.append(
            {
                "claim": "c2",
                "method": "partial_r2_bias_days",
                "parameter": "bias_toward_null",
                "grid_value": partial_r2,
                "biased_estimate": c2_effect + math.copysign(bias_days, -c2_effect),
                "quantity_status": "assumed_sensitivity_only",
            }
        )
    base_probability = min(max(c3_chance_excess + 0.5, 1e-6), 1 - 1e-6)
    for odds in selection_grid:
        shifted = odds_shift(base_probability, odds) - 0.5
        rows.append(
            {
                "claim": "c3",
                "method": "unmeasured_selection_odds",
                "parameter": "selection_odds",
                "grid_value": odds,
                "biased_estimate": shifted,
                "quantity_status": "assumed_sensitivity_only",
            }
        )
    return rows


def _evaluation_rows(
    connection: duckdb.DuckDBPyConnection,
    data_root: Path,
    episodes: Sequence[Episode],
    c3: dict[str, Any],
    draws: int,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    episode_index = {episode.case_id: episode for episode in episodes}
    label_index = {row["pair_id"]: row for row in c3["labeled_rows"]}
    reviewer_rows = connection.execute(
        f"""
        SELECT r.pair_id, upper(trim(r.reviewer_1)), upper(trim(r.reviewer_2)),
               upper(trim(j.llm_judge_verdict)), try_cast(j.llm_judge_score AS DOUBLE)
        FROM read_csv_auto({literal(data_root / 'labels_reviewers.csv')},
                           all_varchar=true, header=true) r
        JOIN read_csv_auto({literal(data_root / 'llm_judge_scores.csv')},
                           all_varchar=true, header=true) j USING(pair_id)
        ORDER BY r.pair_id
        """
    ).fetchall()
    evaluation: list[dict[str, Any]] = []
    r1_positive = mean([float(row[1] == "PROCEED") for row in reviewer_rows])
    r2_positive = mean([float(row[2] == "PROCEED") for row in reviewer_rows])
    reviewer_agree = mean([float(row[1] == row[2]) for row in reviewer_rows])
    reviewer_metrics = binary_agreement(r1_positive, r2_positive, reviewer_agree)
    for metric, value in reviewer_metrics.items():
        evaluation.append(
            {
                "evaluation": "reviewer_1_vs_reviewer_2",
                "metric": metric,
                "value": value,
                "n": len(reviewer_rows),
                "p_value": None,
                "quantity_status": "observed",
                "limitation": "no_adjudicated_ground_truth",
            }
        )

    for reviewer_index, reviewer_name in ((1, "reviewer_1"), (2, "reviewer_2")):
        labels = [int(row[reviewer_index] == "PROCEED") for row in reviewer_rows]
        # Judge score is documented only as a confidence. Mapping HOLD confidence
        # to 1-score is explicit and therefore marked assumed.
        scores = [row[4] if row[3] == "PROCEED" else 1.0 - row[4] for row in reviewer_rows]
        metrics = binary_metrics(labels, scores, [0.5] * len(labels))
        intercept, slope = logistic_calibration(labels, scores)
        metrics["calibration_intercept"] = intercept
        metrics["calibration_slope"] = slope
        by_service: dict[str, list[float]] = defaultdict(list)
        for source, label, score in zip(reviewer_rows, labels, scores, strict=True):
            labeled = label_index.get(source[0])
            service = labeled["service_code"] if labeled is not None else "unknown"
            by_service[service].append(float((score >= 0.5) == bool(label)))
        service_margins = [mean(values) - 0.90 for values in by_service.values() if values]
        inference = webb_service_inference(service_margins, draws, seed + 600 + reviewer_index)
        for metric, value in metrics.items():
            evaluation.append(
                {
                    "evaluation": f"llm_judge_vs_{reviewer_name}",
                    "metric": metric,
                    "value": value,
                    "n": len(labels),
                    "p_value": inference["p_value"] if metric == "balanced_accuracy" else None,
                    "quantity_status": "assumed" if metric in {"brier", "log_loss", "calibration_intercept", "calibration_slope"} else "observed",
                    "limitation": "reviewer_specific_no_adjudication_shared_model_family",
                }
            )

    model_source = connection.execute(
        f"""
        SELECT case_id, try_cast(score_batch AS DOUBLE), try_cast(score_live AS DOUBLE),
               try_cast(feature_visit_null AS BOOLEAN), try_cast(threshold_used AS DOUBLE),
               try_cast(scored_ts AS DATE)
        FROM read_csv_auto({literal(data_root / 'model_scores.csv')},
                           all_varchar=true, header=true)
        ORDER BY case_id
        """
    ).fetchall()
    model_records: list[tuple[Episode, float, float, bool, float, date]] = []
    for case_id, batch, live, feature_null, threshold, scored_date in model_source:
        episode = episode_index.get(case_id)
        if episode is None:
            continue
        observed, outcome = _c1_observation(episode)
        if observed and outcome is not None:
            model_records.append(
                (episode, float(batch), float(live), bool(feature_null), float(threshold), scored_date)
            )
    labels = [int(_c1_observation(row[0])[1]) for row in model_records]
    live_scores = [row[2] for row in model_records]
    thresholds = [row[4] for row in model_records]
    model_metrics = binary_metrics(labels, live_scores, thresholds)
    intercept, slope = logistic_calibration(labels, live_scores)
    model_metrics["calibration_intercept"] = intercept
    model_metrics["calibration_slope"] = slope
    threshold_mean = mean(thresholds)
    predictions = [score >= threshold for score, threshold in zip(live_scores, thresholds, strict=True)]
    true_positives = sum(label == 1 and prediction for label, prediction in zip(labels, predictions, strict=True))
    false_positives = sum(label == 0 and prediction for label, prediction in zip(labels, predictions, strict=True))
    model_metrics["net_benefit"] = (
        true_positives / len(labels)
        - false_positives / len(labels) * threshold_mean / (1.0 - threshold_mean)
    )
    for metric, value in model_metrics.items():
        evaluation.append(
            {
                "evaluation": "recommendation_model_score_live_vs_event_cancellation_proxy",
                "metric": metric,
                "value": value,
                "n": len(model_records),
                "p_value": None,
                "quantity_status": "sensitivity_only",
                "limitation": "outcome_is_not_authorized_day_of_surgery_cancellation_mapping",
            }
        )

    batch_live_changed = sum(not math.isclose(row[1], row[2], abs_tol=1e-15) for row in model_records)
    null_changed = sum(
        row[3] and not math.isclose(row[1], row[2], abs_tol=1e-15) for row in model_records
    )
    scored_before_referral = sum(row[5] < row[0].referral_date for row in model_records)
    scored_after_recommendation = sum(
        row[0].days_to_recommendation is not None
        and row[5] > row[0].referral_date + timedelta(days=math.floor(row[0].days_to_recommendation))
        for row in model_records
    )
    evaluation.extend(
        [
            {
                "evaluation": "recommendation_model_transport",
                "metric": "batch_live_changed_rows",
                "value": batch_live_changed,
                "n": len(model_records),
                "p_value": None,
                "quantity_status": "observed",
                "limitation": "batch_score_is_transport_diagnostic_only",
            },
            {
                "evaluation": "recommendation_model_transport",
                "metric": "feature_null_changed_rows",
                "value": null_changed,
                "n": len(model_records),
                "p_value": None,
                "quantity_status": "observed",
                "limitation": "batch_live_difference_concentrated_in_null_feature_rows",
            },
            {
                "evaluation": "leakage_check",
                "metric": "scored_before_referral_rows",
                "value": scored_before_referral,
                "n": len(model_records),
                "p_value": None,
                "quantity_status": "observed",
                "limitation": "date_only_score_timestamp",
            },
            {
                "evaluation": "leakage_check",
                "metric": "scored_after_recommendation_rows",
                "value": scored_after_recommendation,
                "n": len(model_records),
                "p_value": None,
                "quantity_status": "observed",
                "limitation": "date_only_score_timestamp_and_no_feature_lineage",
            },
        ]
    )

    uplift = connection.execute(
        f"""
        SELECT count(*), avg(try_cast(targeted AS BOOLEAN)::INTEGER),
               min(try_cast(model_auc_reported AS DOUBLE)),
               max(try_cast(model_auc_reported AS DOUBLE))
        FROM read_csv_auto({literal(data_root / 'uplift_targeting.csv')},
                           all_varchar=true, header=true)
        """
    ).fetchone()
    evaluation.extend(
        [
            {
                "evaluation": "uplift_targeting",
                "metric": "targeted_fraction",
                "value": float(uplift[1]),
                "n": int(uplift[0]),
                "p_value": None,
                "quantity_status": "observed",
                "limitation": "no_treatment_assignment_or_counterfactual_outcome_for_qini_auuc_or_policy_value",
            },
            {
                "evaluation": "uplift_targeting",
                "metric": "model_auc_reported",
                "value": float(uplift[2]) if uplift[2] == uplift[3] else None,
                "n": int(uplift[0]),
                "p_value": None,
                "quantity_status": "unreproduced",
                "limitation": "auc_is_not_uplift_validation_and_no_source_predictions_labels_are_supplied",
            },
        ]
    )

    monitoring = connection.execute(
        f"""
        SELECT count(*) row_count, sum(try_cast(alert_fired AS BOOLEAN)::INTEGER) alerts,
               count(DISTINCT service_code) services, min(try_cast(week AS DATE)),
               max(try_cast(week AS DATE))
        FROM read_csv_auto({literal(data_root / 'segment_weekly.csv')},
                           all_varchar=true, header=true)
        """
    ).fetchone()
    monitoring_rows = [
        {
            "metric": "supplied_weekly_rows",
            "value": int(monitoring[0]),
            "quantity_status": "observed",
            "limitation": "supplied_summary_not_recomputed_from_event_log_in_claim_path",
        },
        {
            "metric": "supplied_alerts_fired",
            "value": int(monitoring[1]),
            "quantity_status": "observed",
            "limitation": "alert_rule_and_seasonality_model_not_supplied_so_alert_validity_is_unreproduced",
        },
        {
            "metric": "services_monitored",
            "value": int(monitoring[2]),
            "quantity_status": "observed",
            "limitation": "descriptive_only",
        },
    ]
    return evaluation, monitoring_rows


def _subgroup_rows(
    episodes: Sequence[Episode],
    c3: dict[str, Any],
    switch: date,
    draws: int,
    seed: int,
    ridge: float,
    iterations: int,
) -> list[dict[str, Any]]:
    definitions: list[tuple[str, str, Callable[[Episode], bool]]] = [
        ("sex", "F", lambda episode: episode.sex == "F"),
        ("sex", "M", lambda episode: episode.sex == "M"),
        ("age", "age_lt_40", lambda episode: _age_band(episode.age) == "age_lt_40"),
        ("age", "age_40_64", lambda episode: _age_band(episode.age) == "age_40_64"),
        ("age", "age_ge_65", lambda episode: _age_band(episode.age) == "age_ge_65"),
    ]
    rows: list[dict[str, Any]] = []
    effects_by_claim_group: dict[tuple[str, str, str], list[float]] = {}
    for claim in ("c1", "c2"):
        for dimension, level, predicate in definitions:
            effects = _service_effects(episodes, switch, claim, predicate=predicate)
            values = [float(row["effect"]) for row in effects]
            effects_by_claim_group[(claim, dimension, level)] = values
            if len(values) >= 5:
                inference = webb_service_inference(values, draws, seed + len(rows) + 700)
                rows.append(
                    {
                        "claim": claim,
                        "method": "approach_a_service_did",
                        "dimension": dimension,
                        "level": level,
                        "estimate": inference["estimate"],
                        "lower": inference["lower"],
                        "upper": inference["upper"],
                        "p_value": inference["p_value"],
                        "service_clusters": len(values),
                        "episodes": sum(predicate(episode) for episode in episodes),
                        "status": "estimated",
                        "quantity_status": "sensitivity_only",
                    }
                )
            else:
                rows.append(
                    {
                        "claim": claim,
                        "method": "approach_a_service_did",
                        "dimension": dimension,
                        "level": level,
                        "estimate": None,
                        "lower": None,
                        "upper": None,
                        "p_value": None,
                        "service_clusters": len(values),
                        "episodes": sum(predicate(episode) for episode in episodes),
                        "status": "suppressed_fewer_than_5_service_clusters",
                        "quantity_status": "not_estimated",
                    }
                )
            try:
                synthetic = _synthetic_result(
                    episodes, claim, switch, ridge, iterations, predicate=predicate
                )
                rows.append(
                    {
                        "claim": claim,
                        "method": "approach_b_synthetic_control",
                        "dimension": dimension,
                        "level": level,
                        "estimate": synthetic["estimate"],
                        "lower": synthetic["lower"],
                        "upper": synthetic["upper"],
                        "p_value": None,
                        "service_clusters": synthetic["placebo_count"],
                        "episodes": sum(predicate(episode) for episode in episodes),
                        "status": "descriptive_no_formal_subgroup_test",
                        "quantity_status": "sensitivity_only",
                    }
                )
            except (ValueError, ZeroDivisionError):
                rows.append(
                    {
                        "claim": claim,
                        "method": "approach_b_synthetic_control",
                        "dimension": dimension,
                        "level": level,
                        "estimate": None,
                        "lower": None,
                        "upper": None,
                        "p_value": None,
                        "service_clusters": 0,
                        "episodes": sum(predicate(episode) for episode in episodes),
                        "status": "not_estimable_donor_fit",
                        "quantity_status": "not_estimated",
                    }
                )

    # Formal interaction tests are service-level differences, not separate
    # within-subgroup claims.
    for claim in ("c1", "c2"):
        for dimension, level, reference in (
            ("sex", "F", "M"),
            ("age", "age_40_64", "age_lt_40"),
            ("age", "age_ge_65", "age_lt_40"),
        ):
            left = effects_by_claim_group[(claim, dimension, level)]
            right = effects_by_claim_group[(claim, dimension, reference)]
            count = min(len(left), len(right))
            interactions = [left[index] - right[index] for index in range(count)]
            inference = webb_service_inference(interactions, draws, seed + len(rows) + 800)
            rows.append(
                {
                    "claim": claim,
                    "method": "approach_a_interaction",
                    "dimension": dimension,
                    "level": f"{level}_vs_{reference}",
                    "estimate": inference["estimate"],
                    "lower": inference["lower"],
                    "upper": inference["upper"],
                    "p_value": inference["p_value"],
                    "service_clusters": count,
                    "episodes": None,
                    "status": "estimated_interaction",
                    "quantity_status": "sensitivity_only",
                }
            )

    target = [
        row
        for row in c3["labeled_rows"]
        if row["site"] == "A" and row["post"] and row["eligible_event_pair"]
    ]
    for dimension, level, _ in definitions:
        selected = [
            row
            for row in target
            if (row["sex"] == level if dimension == "sex" else row["age_band"] == level)
        ]
        services = {row["service_code"] for row in selected}
        if len(selected) < 20 or len(services) < 5:
            rows.append(
                {
                    "claim": "c3",
                    "method": "approach_a_two_phase_ipw",
                    "dimension": dimension,
                    "level": level,
                    "estimate": None,
                    "lower": None,
                    "upper": None,
                    "p_value": None,
                    "service_clusters": len(services),
                    "episodes": len(selected),
                    "status": "suppressed_minimum_cluster_or_episode_rule",
                    "quantity_status": "not_estimated",
                }
            )
        else:
            service_values: dict[str, list[float]] = defaultdict(list)
            for row in selected:
                service_values[row["service_code"]].append(
                    float(row["recommendation"] == row["action"])
                )
            margins = [mean(values) - 0.80 for values in service_values.values()]
            inference = webb_service_inference(margins, draws, seed + len(rows) + 900)
            rows.append(
                {
                    "claim": "c3",
                    "method": "approach_a_two_phase_ipw",
                    "dimension": dimension,
                    "level": level,
                    "estimate": inference["estimate"] + 0.80,
                    "lower": inference["lower"] + 0.80,
                    "upper": inference["upper"] + 0.80,
                    "p_value": inference["p_value"],
                    "service_clusters": len(services),
                    "episodes": len(selected),
                    "status": "estimated_raw_agreement",
                    "quantity_status": "sensitivity_only",
                }
            )
    return rows


def _sensitivity_rows(
    episodes: Sequence[Episode],
    switch: date,
    ridge: float,
    iterations: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for horizon in (30.0, 60.0, 90.0):
        for claim in ("c1", "c2"):
            effects = _service_effects(episodes, switch, claim, horizon=horizon)
            rows.append(
                {
                    "claim": claim,
                    "sensitivity": "followup_days",
                    "setting": int(horizon),
                    "estimate": mean([float(row["effect"]) for row in effects]),
                    "quantity_status": "sensitivity_only",
                }
            )
    for shift_weeks in (-4, -2, 2, 4):
        shifted_switch = switch + timedelta(weeks=shift_weeks)
        for claim in ("c1", "c2"):
            effects = _service_effects(episodes, shifted_switch, claim)
            rows.append(
                {
                    "claim": claim,
                    "sensitivity": "policy_timing_shift_weeks",
                    "setting": shift_weeks,
                    "estimate": mean([float(row["effect"]) for row in effects]),
                    "quantity_status": "sensitivity_only",
                }
            )
    for claim in ("c1", "c2"):
        effects = _service_effects(episodes, switch, claim)
        case_weighted = sum(float(row["effect"]) * int(row["a_after_n"]) for row in effects) / sum(
            int(row["a_after_n"]) for row in effects
        )
        rows.append(
            {
                "claim": claim,
                "sensitivity": "case_volume_weighting",
                "setting": "post_site_a_volume",
                "estimate": case_weighted,
                "quantity_status": "sensitivity_only",
            }
        )
        for omitted in sorted(row["specialty"] for row in effects):
            retained = [float(row["effect"]) for row in effects if row["specialty"] != omitted]
            rows.append(
                {
                    "claim": claim,
                    "sensitivity": "leave_one_service_out",
                    "setting": omitted,
                    "estimate": mean(retained),
                    "quantity_status": "sensitivity_only",
                }
            )

    # Snapshot endpoints are explicitly non-authoritative sensitivity proxies.
    for claim in ("c1", "c2"):
        specialty_effects: list[float] = []
        for specialty in sorted({episode.specialty for episode in episodes if episode.site == "A"}):
            cells: dict[tuple[str, str], float] = {}
            for site in ("A", "B"):
                for period in ("before", "after"):
                    group = [
                        episode
                        for episode in episodes
                        if episode.site == site
                        and episode.specialty == specialty
                        and ((episode.referral_date >= switch) == (period == "after"))
                    ]
                    if claim == "c1":
                        values = [float(episode.snapshot_cancelled) for episode in group if episode.snapshot_cancelled is not None]
                    else:
                        values = [episode.snapshot_readiness_days for episode in group if episode.snapshot_readiness_days is not None]
                    cells[(site, period)] = mean(values)
            specialty_effects.append(
                cells[("A", "after")]
                - cells[("A", "before")]
                - cells[("B", "after")]
                + cells[("B", "before")]
            )
        rows.append(
            {
                "claim": claim,
                "sensitivity": "mutable_snapshot_endpoint",
                "setting": "capture_time_unknown_complete_cases_for_readiness",
                "estimate": mean(specialty_effects),
                "quantity_status": "sensitivity_only",
            }
        )
    rows.extend(
        [
            {
                "claim": "c1_c2",
                "sensitivity": "timezone_convention",
                "setting": "not_run_no_authoritative_alternate_zone_mapping",
                "estimate": None,
                "quantity_status": "not_estimated",
            },
            {
                "claim": "c1_c2_c3",
                "sensitivity": "selected_service_mapping",
                "setting": "not_run_authoritative_activation_contract_absent",
                "estimate": None,
                "quantity_status": "not_estimated",
            },
        ]
    )
    return rows


def _adjust_family(rows: list[dict[str, Any]], method: str) -> None:
    p_values = [float(row["p_value"]) if row.get("p_value") is not None else None for row in rows]
    adjusted = holm_adjust(p_values) if method == "holm" else benjamini_yekutieli_adjust(p_values)
    for row, value in zip(rows, adjusted, strict=True):
        row["adjustment_method"] = method
        row["p_adjusted"] = value


def _unit_for(field: str, table: str) -> str:
    if "p_value" in field or "rate" in field or "fraction" in field or "agreement" in field or field in {
        "estimate",
        "lower",
        "upper",
        "diff",
        "value",
    }:
        if "c2" in table or "readiness" in table:
            return "days_or_dimensionless_as_labeled"
        return "proportion_or_dimensionless_as_labeled"
    if "days" in field or "rmst" in field:
        return "days"
    if "pct" in field:
        return "percent"
    if field.startswith("n") or "count" in field or "rows" in field or "clusters" in field or "weeks" in field:
        return "count"
    return "dimensionless"


def _register_table_numbers(
    registry: NumberRegistry, table_name: str, rows: list[dict[str, Any]], script: str = SCRIPT_PATH
) -> None:
    for row_index, row in enumerate(rows):
        status = str(row.get("quantity_status", "observed"))
        assumptions = str(
            row.get("limitation")
            or row.get("interpretation")
            or row.get("assumptions")
            or ""
        )
        additions: dict[str, str] = {}
        for field, value in list(row.items()):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number_id = f"N-{table_name.upper().replace('_', '-')}-{row_index + 1:04d}-{field.upper()}"
            registry.add(
                number_id,
                value,
                unit=_unit_for(field, table_name),
                script=script,
                table=f"{table_name}.csv",
                quantity_status=str(row.get(f"{field}_quantity_status", status)),
                assumptions=assumptions,
            )
            additions[f"{field}_number_id"] = number_id
        row.update(additions)


def _escape_svg(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _claim_svg(claim: dict[str, Any], path: Path) -> None:
    lower = min(float(claim["approach_a_lower"]), float(claim["approach_b_lower"]), 0.0)
    upper = max(float(claim["approach_a_upper"]), float(claim["approach_b_upper"]), 0.0)
    padding = max((upper - lower) * 0.15, 0.01)
    lower -= padding
    upper += padding
    width, height = 900, 280
    left, right = 170, 850

    def x(value: float) -> float:
        return left + (value - lower) / (upper - lower) * (right - left)

    unit = claim["unit"]
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f"<metadata>claim={_escape_svg(claim['claim'])}; quantity_status=sensitivity_only; implementation={_escape_svg(claim['implementation_commit'])}</metadata>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="30" y="32" font-family="Arial" font-size="20" font-weight="bold" fill="#172033">{_escape_svg(claim["short_title"])}</text>',
        '<text x="30" y="56" font-family="Arial" font-size="12" fill="#8b1e3f">Assumed all-Site A sensitivity proxy; not the unidentified frozen primary estimand</text>',
        f'<line x1="{left}" y1="225" x2="{right}" y2="225" stroke="#61708a"/>',
        f'<line x1="{x(0)}" y1="80" x2="{x(0)}" y2="232" stroke="#172033" stroke-dasharray="4 4"/>',
        f'<text x="{left}" y="250" font-family="Arial" font-size="11">{lower:.3f}</text>',
        f'<text x="{right - 50}" y="250" font-family="Arial" font-size="11">{upper:.3f}</text>',
        f'<text x="{(left + right) / 2 - 35}" y="270" font-family="Arial" font-size="11">{_escape_svg(unit)}</text>',
    ]
    for y, label, estimate, interval_lower, interval_upper, color in (
        (120, "Approach A", claim["approach_a_estimate"], claim["approach_a_lower"], claim["approach_a_upper"], "#16697a"),
        (180, "Approach B", claim["approach_b_estimate"], claim["approach_b_lower"], claim["approach_b_upper"], "#c56a1a"),
    ):
        lines.extend(
            [
                f'<text x="30" y="{y + 5}" font-family="Arial" font-size="14" fill="#172033">{label}</text>',
                f'<line x1="{x(float(interval_lower))}" y1="{y}" x2="{x(float(interval_upper))}" y2="{y}" stroke="{color}" stroke-width="5"/>',
                f'<circle cx="{x(float(estimate))}" cy="{y}" r="7" fill="{color}"/>',
            ]
        )
    lines.append("</svg>")
    _write_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))


def _service_svg(service_rows: Sequence[dict[str, Any]], path: Path) -> None:
    c1 = [row for row in service_rows if row["claim"] == "c1"]
    width, height = 1000, 520
    values = [float(row["effect"]) for row in c1]
    bound = max(max(abs(value) for value in values) * 1.2, 0.02)
    left, right = 230, 950

    def x(value: float) -> float:
        return left + (value + bound) / (2 * bound) * (right - left)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<metadata>service-specific matched-site DiD; assumed sensitivity-only exposure proxy</metadata>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="30" y="32" font-family="Arial" font-size="20" font-weight="bold" fill="#172033">Service-specific cancellation-proxy DiD</text>',
        '<text x="30" y="55" font-family="Arial" font-size="12" fill="#8b1e3f">Variation across services shows why the pooled analyst result is not a treatment effect.</text>',
        f'<line x1="{x(0)}" y1="75" x2="{x(0)}" y2="485" stroke="#172033" stroke-dasharray="4 4"/>',
    ]
    for index, row in enumerate(c1):
        y = 90 + index * 32
        value = float(row["effect"])
        lines.append(
            f'<text x="40" y="{y + 5}" font-family="Arial" font-size="12">{_escape_svg(row["specialty"])}</text>'
        )
        lines.append(
            f'<line x1="{x(0)}" y1="{y}" x2="{x(value)}" y2="{y}" stroke="#6b7c93" stroke-width="5"/>'
        )
        lines.append(f'<circle cx="{x(value)}" cy="{y}" r="5" fill="#16697a"/>')
        lines.append(
            f'<text x="{x(value) + 9}" y="{y + 5}" font-family="Arial" font-size="11">{value:.4f}</text>'
        )
    lines.append("</svg>")
    _write_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))


def _analyst_svg(rows: Sequence[dict[str, Any]], path: Path) -> None:
    width, height = 1000, 250
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<metadata>isolated non-endorsed analyst deck-number reproduction</metadata>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="30" y="32" font-family="Arial" font-size="20" font-weight="bold" fill="#172033">Analyst deck numbers: supplied versus reproducible</text>',
        '<text x="30" y="55" font-family="Arial" font-size="12" fill="#8b1e3f">Unreproduced means no matching derivation was found; no value was invented.</text>',
        '<text x="360" y="82" font-family="Arial" font-size="12" font-weight="bold">Supplied</text>',
        '<text x="520" y="82" font-family="Arial" font-size="12" font-weight="bold">Reproduced</text>',
        '<text x="720" y="82" font-family="Arial" font-size="12" font-weight="bold">Status</text>',
    ]
    for index, row in enumerate(rows):
        y = 110 + index * 32
        reproduced = "NA" if row["reproduced_value"] is None else f"{float(row['reproduced_value']):.3f}"
        color = "#1f7a4d" if row["reproduction_status"] == "reproduced" else "#a33a2b"
        lines.extend(
            [
                f'<text x="30" y="{y}" font-family="Arial" font-size="12">{_escape_svg(row["metric"])}</text>',
                f'<text x="360" y="{y}" font-family="Arial" font-size="12">{float(row["supplied_value"]):.3f}</text>',
                f'<text x="520" y="{y}" font-family="Arial" font-size="12">{reproduced}</text>',
                f'<text x="720" y="{y}" font-family="Arial" font-size="12" fill="{color}">{_escape_svg(row["reproduction_status"])}</text>',
            ]
        )
    lines.append("</svg>")
    _write_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))


def _format(value: Any, digits: int = 3) -> str:
    if value is None:
        return "not estimable"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{float(value):.{digits}f}"


def _report_markdown(
    claims: Sequence[dict[str, Any]],
    analyst_rows: Sequence[dict[str, Any]],
    evidence: dict[str, Any],
    absent_identity: str,
) -> str:
    by_claim = {row["claim"]: row for row in claims}

    def tagged(row: dict[str, Any], field: str, digits: int = 3) -> str:
        return f"{_format(row[field], digits)} [{row[field + '_number_id']}]"

    c1, c2, c3 = by_claim["c1"], by_claim["c2"], by_claim["c3"]
    analyst_status = ", ".join(
        f"{row['metric']}={row['reproduction_status']}" for row in analyst_rows
    )
    return f"""# Claim Results - v1

Status: locked scripted result. Every numeric value below cites its `number_id`; `number_registry.csv` links that ID to the producing script, data/config fingerprints, implementation commit, and quantity label. No number comes from a notebook or manual calculation.

## Bottom line in plain language

- **Claim 1 - fewer day-of-surgery cancellations: {c1['verdict']}.** The data package does not say which services actually switched on, and its cancellation field does not identify day-of-surgery cancellations. Under an explicitly assumed all-Site A proxy, Approach A estimates {tagged(c1, 'approach_a_estimate', 4)} and Approach B estimates {tagged(c1, 'approach_b_estimate', 4)}. These are sensitivity results, not the claimed treatment effect.
- **Claim 2 - faster referral-to-readiness: {c2['verdict']}.** The same missing service assignment and the concurrent scheduling-policy change prevent a clean causal comparison. Under the same assumed proxy, Approach A estimates {tagged(c2, 'approach_a_estimate', 2)} days and Approach B estimates {tagged(c2, 'approach_b_estimate', 2)} days. Administrative censoring and MNAR scenarios remain assumption uncertainty, not sampling error.
- **Claim 3 - clinicians agree in a large majority: {c3['verdict']}.** In the assumed all-Site A post-switch sensitivity population, the service-poststratified raw agreement is {tagged(c3, 'raw_agreement', 3)}, but the chance-excess estimate is only {tagged(c3, 'approach_a_estimate', 3)}. More importantly, the active-service population is unknown and most eligible pairs have no supplied recommendation/action label. A weaker statement about the supplied labeled sample is possible; the written population claim is not proved.

The frozen absent-effect rule identifies **{absent_identity}**. It is unresolved because a failed identification gate or a wide assumption region is not evidence that an effect is absent.

## Why the intervals are not ordinary patient-level confidence intervals

The intervention is assigned at service level. Baseline clinician cross-cover connects {tagged(evidence, 'total_services', 0)} services into {tagged(evidence, 'total_components', 0)} components ({tagged(evidence, 'site_a_components', 0)} at Site A and {tagged(evidence, 'site_b_components', 0)} at Site B). Because the actual treated services are unknown, the treated-component count is also unknown. The displayed Approach A ranges are service-level wild-bootstrap bounds; Approach B ranges are in-space placebo bounds. Neither is labeled a nominal patient-level confidence interval.

## Analyst reproduction, without endorsement

The reviewed literal analyst query was reproduced in the isolated `analyst_reproduction` path. Its key defects are mechanical and visible: raw-key join loss, duplicate snapshot multiplication, selection on `assessment_generated`, assessment-time rather than referral-time period assignment, Site B and untreated-service mixing, complete-case readiness averaging, and no service-level inference. Correctable deltas in `analyst_bias_audit.csv` are explicitly order-dependent descriptions; selection, the scheduling policy, interference, and the missing active-service contract are marked not quantifiable. Deck status: {analyst_status}.

The script counted {tagged(evidence, 'normalized_only_key_rows', 0)} snapshot rows requiring key normalization, {tagged(evidence, 'duplicate_snapshot_keys', 0)} duplicate snapshot keys, {tagged(evidence, 'excluded_without_assessment', 0)} eligible referrals excluded by assessment conditioning, and {tagged(evidence, 'crossed_switch_after_assessment', 0)} episodes whose referral was pre-switch but assessment was post-switch. These counts are generated evidence, not causal effect contributions.

## What the data cannot honestly prove

1. It cannot identify the selected treated services from an independent activation record.
2. It cannot separate the system from the same-time scheduling-policy change or from service selection and spillover.
3. It cannot map the general cancellation flag to day-of-surgery cancellation with supplied clinical authority.
4. It cannot turn the analyst-selected labeled pairs into the full displayed-recommendation population without untestable selection assumptions.
5. It cannot validate clinician correctness: the two reviewers were not adjudicated, and the LLM judge is not independent ground truth.

Sampling uncertainty is reported separately from MNAR/QBA assumptions. Simulated, imputed, assumed, extrapolated, and sensitivity-only values carry those labels in every machine-readable table and in the number registry.
"""


def run_analysis(paths: AnalysisPaths) -> dict[str, Any]:
    paths.validate()
    execution = yaml.safe_load(paths.execution_config.read_text(encoding="utf-8"))
    frozen = yaml.safe_load(paths.frozen_config.read_text(encoding="utf-8"))
    implementation_commit = _implementation_commit(paths.repository_root)
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "prespec-v1", implementation_commit],
        cwd=paths.repository_root,
        check=False,
    ).returncode
    if ancestor != 0:
        raise ValueError("prespec-v1 is not an ancestor of the analysis implementation")

    pipeline_result = run_pipeline(
        RuntimePaths(
            data_root=paths.data_root,
            work_root=paths.work_root,
            output_root=paths.pipeline_output_root,
            config_path=paths.repository_root / "config" / "pipeline.yaml",
        ),
        mode="incremental",
    )
    verify_artifacts(paths.pipeline_output_root)
    artifact_directory = pipeline_result.artifact_directory
    pipeline_manifest_path = artifact_directory / "artifact_manifest.json"
    pipeline_manifest = json.loads(pipeline_manifest_path.read_text(encoding="utf-8"))

    source_names = [
        "snapshot_cases.csv",
        "patients.csv",
        "model_scores.csv",
        "uplift_targeting.csv",
        "segment_weekly.csv",
        "labels_pairs.csv",
        "labels_reviewers.csv",
        "llm_judge_scores.csv",
        "comparisons_log.csv",
        "analyst_deck_numbers.csv",
        "analyst_query.sql",
    ]
    inputs = [
        {
            "path": name,
            "sha256": sha256_file(paths.data_root / name),
            "size_bytes": (paths.data_root / name).stat().st_size,
            "role": "direct_analysis_input",
        }
        for name in source_names
    ]
    inputs.append(
        {
            "path": "pipeline/artifact_manifest.json",
            "sha256": sha256_file(pipeline_manifest_path),
            "size_bytes": pipeline_manifest_path.stat().st_size,
            "role": "canonical_event_pipeline_input",
        }
    )
    input_fingerprint = _sha256_payload(inputs)
    config_fingerprint = _sha256_payload(
        {
            "execution": execution,
            "frozen": frozen,
            "execution_sha256": sha256_file(paths.execution_config),
            "frozen_sha256": sha256_file(paths.frozen_config),
        }
    )
    analysis_id = _sha256_payload(
        {
            "schema_version": RESULT_SCHEMA_VERSION,
            "input_fingerprint": input_fingerprint,
            "config_fingerprint": config_fingerprint,
            "implementation_commit": implementation_commit,
        }
    )
    version_root = paths.result_root / execution["result_version"]
    final_directory = version_root / analysis_id
    if final_directory.exists():
        return verify_analysis_results(final_directory)
    staging = version_root / ".staging" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=False)

    connection = duckdb.connect()
    try:
        study_end = date.fromisoformat(execution["study"]["end"])
        switch = date.fromisoformat(execution["study"]["switch"])
        placebo_switch = date.fromisoformat(execution["study"]["placebo_switch"])
        seed = int(execution["inference"]["seed"])
        draws = int(execution["inference"]["wild_bootstrap_draws"])
        ridge = float(execution["inference"]["synthetic_ridge"])
        iterations = int(execution["inference"]["synthetic_iterations"])
        episodes = _load_episodes(connection, paths.data_root, artifact_directory, study_end)
        components = _coverage_components(connection, artifact_directory, switch)
        analyst = reproduce_analyst_materials(
            connection,
            paths.data_root,
            artifact_directory / "case_workflow.parquet",
            execution["analyst_reproduction"]["expected_query_sha256"],
        )
        c1_service = _service_effects(episodes, switch, "c1")
        c2_service = _service_effects(episodes, switch, "c2")
        c1_a = webb_service_inference(
            [float(row["effect"]) for row in c1_service], draws, seed + 1
        )
        c2_a = webb_service_inference(
            [float(row["effect"]) for row in c2_service], draws, seed + 2
        )
        c1_b = _synthetic_result(episodes, "c1", switch, ridge, iterations)
        c2_b = _synthetic_result(episodes, "c2", switch, ridge, iterations)
        c3 = _label_analysis(connection, paths.data_root, episodes, switch, draws, seed)
        missingness = _missingness_rows(
            episodes,
            switch,
            c3,
            [float(value) for value in execution["missingness"]["odds_or_hazard_multipliers"]],
            [float(value) for value in execution["missingness"]["site_b_observation_odds"]],
        )
        c3_pattern = [
            float(row["chance_excess_estimate"])
            for row in missingness
            if row["claim"] == "c3" and row.get("chance_excess_estimate") is not None
        ]
        c3_a = c3["approach_a"]
        claim_rows: list[dict[str, Any]] = [
            {
                "claim": "c1",
                "short_title": "Claim 1: cancellation proxy",
                "unit": "risk difference",
                "approach_a": "service-level covariate-poststratified DiD sensitivity",
                "approach_a_estimate": c1_a["estimate"],
                "approach_a_lower": c1_a["lower"],
                "approach_a_upper": c1_a["upper"],
                "approach_a_p_value": c1_a["p_value"],
                "approach_b": "augmented synthetic control sensitivity",
                "approach_b_estimate": c1_b["estimate"],
                "approach_b_lower": c1_b["lower"],
                "approach_b_upper": c1_b["upper"],
                "approach_b_p_value": c1_b["p_value"],
                "raw_agreement": None,
                "verdict": "unsupported_by_observational_design",
                "primary_estimand_identified": False,
                "absent_rule_qualifies": False,
                "quantity_status": "assumed_sensitivity_only",
                "approach_a_lower_quantity_status": "simulated_sensitivity_only",
                "approach_a_upper_quantity_status": "simulated_sensitivity_only",
                "approach_a_p_value_quantity_status": "simulated_sensitivity_only",
                "gates": "selected_services_unknown;scheduling_policy_cointervention;interference;endpoint_mapping_absent;treated_component_count_unknown",
                "implementation_commit": implementation_commit,
            },
            {
                "claim": "c2",
                "short_title": "Claim 2: days not ready",
                "unit": "days",
                "approach_a": "service-level KM-RMST DiD sensitivity",
                "approach_a_estimate": c2_a["estimate"],
                "approach_a_lower": c2_a["lower"],
                "approach_a_upper": c2_a["upper"],
                "approach_a_p_value": c2_a["p_value"],
                "approach_b": "augmented synthetic control sensitivity",
                "approach_b_estimate": c2_b["estimate"],
                "approach_b_lower": c2_b["lower"],
                "approach_b_upper": c2_b["upper"],
                "approach_b_p_value": c2_b["p_value"],
                "raw_agreement": None,
                "verdict": "unsupported_by_observational_design",
                "primary_estimand_identified": False,
                "absent_rule_qualifies": False,
                "quantity_status": "assumed_sensitivity_only",
                "approach_a_lower_quantity_status": "simulated_sensitivity_only",
                "approach_a_upper_quantity_status": "simulated_sensitivity_only",
                "approach_a_p_value_quantity_status": "simulated_sensitivity_only",
                "gates": "selected_services_unknown;scheduling_policy_cointervention;interference;administrative_censoring;treated_component_count_unknown",
                "implementation_commit": implementation_commit,
            },
            {
                "claim": "c3",
                "short_title": "Claim 3: chance-excess agreement",
                "unit": "proportion",
                "approach_a": "service-poststratified two-phase IPW sensitivity",
                "approach_a_estimate": c3_a["chance_excess"],
                "approach_a_lower": c3_a["chance_lower"],
                "approach_a_upper": c3_a["chance_upper"],
                "approach_a_p_value": c3_a["chance_p_value"],
                "approach_b": "pattern-mixture identification region",
                "approach_b_estimate": c3_a["chance_excess"],
                "approach_b_lower": min(c3_pattern),
                "approach_b_upper": max(c3_pattern),
                "approach_b_p_value": None,
                "raw_agreement": c3_a["raw_agreement"],
                "verdict": "unsupported_with_these_data",
                "primary_estimand_identified": False,
                "absent_rule_qualifies": False,
                "quantity_status": "assumed_sensitivity_only",
                "approach_a_lower_quantity_status": "simulated_sensitivity_only",
                "approach_a_upper_quantity_status": "simulated_sensitivity_only",
                "approach_a_p_value_quantity_status": "simulated_sensitivity_only",
                "gates": "active_services_unknown;nonrandom_label_sample;unlabeled_pair_outcomes;no_adjudication;treated_component_count_unknown",
                "implementation_commit": implementation_commit,
            },
        ]

        service_rows = [
            {"claim": "c1", **row, "quantity_status": "sensitivity_only"}
            for row in c1_service
        ] + [
            {"claim": "c2", **row, "quantity_status": "sensitivity_only"}
            for row in c2_service
        ]
        weight_rows = [
            {
                "claim": claim,
                "donor_service": service,
                "weight": weight,
                "quantity_status": "assumed_sensitivity_only",
            }
            for claim, result in (("c1", c1_b), ("c2", c2_b))
            for service, weight in result["donor_weights"].items()
        ]

        placebo_rows: list[dict[str, Any]] = []
        placebo_episodes = [episode for episode in episodes if episode.referral_date < switch]
        for claim, service in (("c1", _service_effects(placebo_episodes, placebo_switch, "c1")), ("c2", _service_effects(placebo_episodes, placebo_switch, "c2"))):
            pre_only = [
                row
                for row in service
                if True
            ]
            inference = webb_service_inference(
                [float(row["effect"]) for row in pre_only], draws, seed + 100 + len(placebo_rows)
            )
            placebo_rows.append(
                {
                    "control": f"{claim}_pre_switch_placebo",
                    "estimate": inference["estimate"],
                    "lower": inference["lower"],
                    "upper": inference["upper"],
                    "p_value": inference["p_value"],
                    "service_clusters": inference["service_clusters"],
                    "quantity_status": "sensitivity_only",
                }
            )
        negative_controls = placebo_rows + _negative_controls(episodes, switch, draws, seed)
        qba = _qba_rows(
            float(c1_a["estimate"]),
            mean([float(row["a_after"]) for row in c1_service]),
            float(c2_a["estimate"]),
            sample_sd([float(row["effect"]) for row in c2_service]),
            float(c3_a["chance_excess"]),
            [float(value) for value in execution["quantitative_bias"]["risk_ratio_grid"]],
            [float(value) for value in execution["quantitative_bias"]["partial_r2_grid"]],
            [float(value) for value in execution["quantitative_bias"]["selection_odds_grid"]],
        )
        subgroups = _subgroup_rows(
            episodes, c3, switch, draws, seed, ridge, iterations
        )
        sensitivities = _sensitivity_rows(episodes, switch, ridge, iterations)
        evaluation, monitoring = _evaluation_rows(
            connection, paths.data_root, episodes, c3, draws, seed
        )
        legacy = _legacy_comparison_rows(
            connection, paths.data_root, artifact_directory / "case_workflow.parquet"
        )

        analyst_query_rows = [
            {
                "period": period,
                **values,
                "quantity_status": "reproduced",
            }
            for period, values in sorted(analyst["exact_periods"].items())
        ]
        analyst_bias = list(analyst["bias_audit"])
        analyst_bias.extend(
            [
                {
                    "stage": "05_selected_service_assignment",
                    "cancellation_reduction_pct": None,
                    "readiness_time_reduction_days": None,
                    "delta_cancellation_pct_from_prior": None,
                    "delta_readiness_days_from_prior": None,
                    "quantity_status": "not_estimated",
                    "interpretation": "not_quantifiable_without_independent_activation_contract",
                    "contribution_scope": "not_quantifiable",
                },
                {
                    "stage": "06_scheduling_policy_and_interference",
                    "cancellation_reduction_pct": None,
                    "readiness_time_reduction_days": None,
                    "delta_cancellation_pct_from_prior": None,
                    "delta_readiness_days_from_prior": None,
                    "quantity_status": "not_estimated",
                    "interpretation": "not_quantifiable_without_policy_indicator_or_valid_instrument",
                    "contribution_scope": "not_quantifiable",
                },
            ]
        )

        multiplicity: list[dict[str, Any]] = []
        for claim, result in (("c1_primary", c1_a), ("c2_primary", c2_a)):
            multiplicity.append(
                {
                    "family": "F_headline",
                    "comparison": claim,
                    "p_value": result["p_value"],
                    "quantity_status": "sensitivity_only",
                }
            )
        multiplicity.append(
            {
                "family": "F_headline",
                "comparison": "c3_primary",
                "p_value": c3_a["chance_p_value"],
                "quantity_status": "sensitivity_only",
            }
        )
        for row in legacy:
            multiplicity.append(
                {
                    "family": "F_legacy51",
                    "comparison": row["comparison"],
                    "p_value": row["supplied_p_value"],
                    "quantity_status": "unreproduced" if row["reproduction_status"] == "unreproduced" else "reproduced",
                }
            )
        for row in negative_controls:
            multiplicity.append(
                {
                    "family": "F_diagnostic",
                    "comparison": row["control"],
                    "p_value": row["p_value"],
                    "quantity_status": "sensitivity_only",
                }
            )
        for row in subgroups:
            if row.get("p_value") is not None and "interaction" in str(row.get("method")):
                multiplicity.append(
                    {
                        "family": "F_subgroup",
                        "comparison": f"{row['claim']}:{row['method']}:{row['dimension']}:{row['level']}",
                        "p_value": row["p_value"],
                        "quantity_status": "sensitivity_only",
                    }
                )
        for row in evaluation:
            if row.get("p_value") is not None:
                multiplicity.append(
                    {
                        "family": "F_evaluation",
                        "comparison": f"{row['evaluation']}:{row['metric']}",
                        "p_value": row["p_value"],
                        "quantity_status": row["quantity_status"],
                    }
                )
        for family, method in (
            ("F_headline", "holm"),
            ("F_legacy51", "holm"),
            ("F_diagnostic", "holm"),
            ("F_subgroup", "benjamini_yekutieli"),
            ("F_evaluation", "benjamini_yekutieli"),
        ):
            _adjust_family([row for row in multiplicity if row["family"] == family], method)

        absent_identity = "unresolved_no_claim_met_the_frozen_absence_rule"
        decision_rows = [
            {
                "decision": "absent_effect_identity",
                "value": absent_identity,
                "quantity_status": "decision_rule_output",
                "reason": "no_identified_claim_had_both_method_and_all_missingness_bounds_wholly_inside_its_null_region",
            }
        ]
        exclusion_rows = [
            {
                "reason": "missing_or_out_of_study_referral",
                "count": 40000 - len(episodes),
                "quantity_status": "observed",
            },
            {
                "reason": "outcome_assessment_readiness_or_review_status",
                "count": 0,
                "quantity_status": "observed",
            },
            {
                "reason": "contained_impossible_local_timestamps_excluded_upstream",
                "count": int(pipeline_result.quarantine_rows),
                "quantity_status": "observed",
            },
        ]
        design_row = {
            **{key: value for key, value in components.items() if key != "components"},
            **analyst["diagnostics"],
            "quantity_status": "observed",
            "limitation": "actual_treated_component_count_unknown_without_selected_service_contract",
        }
        all_pairs_row = {
            **c3["all_supplied_pairs"],
            "quantity_status": "observed",
            "limitation": "all_supplied_pairs_not_the_claim_target_population",
        }

        tables: dict[str, list[dict[str, Any]]] = {
            "claims": claim_rows,
            "service_effects": service_rows,
            "synthetic_weights": weight_rows,
            "missingness_scenarios": missingness,
            "quantitative_bias": qba,
            "negative_controls": negative_controls,
            "subgroups": subgroups,
            "sensitivities": sensitivities,
            "evaluation": evaluation,
            "monitoring": monitoring,
            "legacy51": legacy,
            "multiplicity": multiplicity,
            "analyst_query_output": analyst_query_rows,
            "analyst_deck_reproduction": analyst["deck_rows"],
            "analyst_bias_audit": analyst_bias,
            "decisions": decision_rows,
            "exclusions": exclusion_rows,
            "design_diagnostics": [design_row],
            "labels_all_supplied_pairs": [all_pairs_row],
        }
        registry = NumberRegistry(input_fingerprint, config_fingerprint, implementation_commit)
        for table_name, rows in tables.items():
            _register_table_numbers(
                registry,
                table_name,
                rows,
                script=ANALYST_SCRIPT_PATH if table_name.startswith("analyst_") else SCRIPT_PATH,
            )
            _write_csv(staging / "tables" / f"{table_name}.csv", rows)
        _write_csv(staging / "number_registry.csv", registry.rows)
        _write_json(
            staging / "results.json",
            {
                "schema_version": RESULT_SCHEMA_VERSION,
                "analysis_id": analysis_id,
                "claims": claim_rows,
                "absent_effect_identity": absent_identity,
                "design_diagnostics": design_row,
                "component_membership": components["components"],
                "labels_all_supplied_pairs": all_pairs_row,
                "quantity_status": "mixed_see_number_registry",
            },
        )
        for claim in claim_rows:
            _claim_svg(claim, staging / "figures" / f"figure_{claim['claim']}.svg")
        _service_svg(service_rows, staging / "figures" / "figure_service_heterogeneity.svg")
        _analyst_svg(
            analyst["deck_rows"], staging / "figures" / "figure_analyst_deck_reproduction.svg"
        )
        _write_bytes(
            staging / "claim-results-v1.md",
            _report_markdown(
                claim_rows,
                analyst["deck_rows"],
                design_row,
                absent_identity,
            ).encode("utf-8"),
        )

        artifact_rows: list[dict[str, Any]] = []
        for path in sorted(staging.rglob("*"), key=lambda item: item.relative_to(staging).as_posix()):
            if not path.is_file():
                continue
            relative = path.relative_to(staging).as_posix()
            row_count: int | None = None
            if path.suffix == ".csv":
                with path.open(newline="", encoding="utf-8") as handle:
                    row_count = sum(1 for _ in csv.DictReader(handle))
            artifact_rows.append(
                {
                    "path": relative,
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                    "rows": row_count,
                }
            )
        manifest = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "result_version": execution["result_version"],
            "analysis_id": analysis_id,
            "implementation_commit": implementation_commit,
            "prespec_tag": "prespec-v1",
            "prespec_commit": subprocess.check_output(
                ["git", "rev-list", "-n", "1", "prespec-v1"],
                cwd=paths.repository_root,
                text=True,
            ).strip(),
            "prespec_is_ancestor": True,
            "input_fingerprint": input_fingerprint,
            "config_fingerprint": config_fingerprint,
            "pipeline_artifact_set_id": pipeline_result.artifact_set_id,
            "pipeline_data_version": pipeline_manifest["data_version"],
            "inputs": inputs,
            "artifacts": artifact_rows,
            "number_registry": {
                "path": "number_registry.csv",
                "rows": len(registry.rows),
                "rule": "every_numeric_cell_in_reported_tables_has_a_number_id",
            },
            "untrusted_inputs_not_used": [
                "clinical_notes.csv",
                "questions.csv",
                "authorization_model.json",
            ],
            "untrusted_sql_rule": "fingerprint_validated_then_reviewed_literal_query_executed",
            "quantity_label_rule": "simulated_imputed_assumed_extrapolated_and_sensitivity_only_values_are_labeled",
            "result_commit_note": "Git commits cannot self-reference; implementation_commit identifies producing code and the final results commit is recorded in docs/decision-log.md after commit.",
        }
        _write_json(staging / "manifest.json", manifest)
    except Exception:
        raise
    finally:
        connection.close()

    final_directory.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, final_directory)
    _write_bytes(version_root / "CURRENT", (analysis_id + "\n").encode("ascii"))
    return verify_analysis_results(final_directory)


def verify_analysis_results(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        path = directory / artifact["path"]
        if not path.is_file() or sha256_file(path) != artifact["sha256"]:
            raise ValueError(f"analysis artifact verification failed: {artifact['path']}")
    registry_path = directory / manifest["number_registry"]["path"]
    with registry_path.open(newline="", encoding="utf-8") as handle:
        registry_count = sum(1 for _ in csv.DictReader(handle))
    if registry_count != manifest["number_registry"]["rows"]:
        raise ValueError("number registry row count mismatch")
    return {
        "analysis_id": manifest["analysis_id"],
        "directory": str(directory),
        "verified_artifacts": len(manifest["artifacts"]),
        "registered_numbers": registry_count,
        "implementation_commit": manifest["implementation_commit"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Barnabus claim-analysis pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--data-root", type=Path, required=True)
    run.add_argument("--work-root", type=Path, default=Path("work"))
    run.add_argument("--pipeline-output-root", type=Path, default=Path("outputs"))
    run.add_argument("--result-root", type=Path, default=Path("results"))
    run.add_argument(
        "--execution-config", type=Path, default=Path("config/analysis-execution-v1.yaml")
    )
    verify = subparsers.add_parser("verify")
    verify.add_argument("--result-directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            print(json.dumps(verify_analysis_results(args.result_directory), sort_keys=True))
            return 0
        repository_root = Path.cwd().resolve()
        result = run_analysis(
            AnalysisPaths(
                repository_root=repository_root,
                data_root=args.data_root.resolve(),
                work_root=args.work_root.resolve(),
                pipeline_output_root=args.pipeline_output_root.resolve(),
                result_root=args.result_root.resolve(),
                execution_config=args.execution_config.resolve(),
                frozen_config=(repository_root / "config" / "analysis-plan-v1.yaml").resolve(),
            )
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, duckdb.Error) as exc:
        print(json.dumps({"event": "analysis_failed", "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
