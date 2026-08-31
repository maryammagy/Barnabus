"""Reproducible scientific supplement for labels, models, study design, and monitoring."""

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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import yaml

from barnabus.analysis import (
    Episode,
    NumberRegistry,
    _c1_observation,
    _coverage_components,
    _json_bytes,
    _load_episodes,
    _register_table_numbers,
    _sha256_payload,
    _write_bytes,
    _write_csv,
    _write_json,
    verify_analysis_results,
)
from barnabus.analyst_reproduction import sha256_file
from barnabus.config import RuntimePaths
from barnabus.pipeline import run_pipeline, verify_artifacts
from barnabus.scientific_stats import (
    agreement_metrics,
    average_precision,
    by_adjust,
    calibrated_favorable_tail_power,
    net_benefit,
    normal_cdf,
    population_stability_index,
    service_cluster_bootstrap,
    service_cluster_classification_intervals,
    service_cluster_reliability_bins,
    simulate_staggered_statistics,
    wilson_interval,
)
from barnabus.stats import logistic_calibration, mean, quantile


SCRIPT_PATH = "src/barnabus/scientific.py"
STATS_PATH = "src/barnabus/scientific_stats.py"
SCHEMA_VERSION = 1

TABLE_GRAINS: dict[str, tuple[str, ...]] = {
    "reviewer_agreement": ("comparison", "metric"),
    "label_provenance": ("field_or_classification",),
    "adjudication_protocol": ("step",),
    "judge_evaluation": ("label_source", "family_relation", "metric"),
    "judge_family_dependence": ("family_relation",),
    "judge_use_decision": ("use_case",),
    "judge_use_gates": ("gate",),
    "label_flow": ("flow",),
    "model_operating_point": ("context", "metric"),
    "model_calibration": ("context", "bin"),
    "decision_curve": ("context", "strategy", "threshold"),
    "threshold_consequences": ("context",),
    "model_subgroups": ("dimension", "group", "metric"),
    "leakage_audit": ("check",),
    "model_flow": ("flow",),
    "uplift_curve": ("population_fraction",),
    "uplift_overlap_calibration": ("score_bin",),
    "uplift_summary": ("metric",),
    "uplift_flow": ("flow",),
    "interference_components": ("component_id",),
    "study_assignment": ("component_id",),
    "study_design": ("element",),
    "power_scenarios": ("icc", "risk_difference"),
    "minimum_detectable_effect": ("icc",),
    "study_feasibility": ("decision",),
    "sequential_rules": ("look",),
    "ethics_governance": ("domain",),
    "monitor_catalog": ("monitor_id",),
    "monitor_replay": ("service_code", "week"),
    "monitor_summary": ("metric",),
    "monitor_snapshot": ("monitor_id", "metric"),
    "monitor_defect_map": ("monitor_id", "defect"),
    "gaps": ("gap_id",),
    "design_parameters": ("parameter",),
    "evaluation_comparison_ledger": ("comparison_id",),
    "table_contracts": ("table",),
}


@dataclass(frozen=True)
class ScientificPaths:
    repository_root: Path
    data_root: Path
    work_root: Path
    pipeline_output_root: Path
    locked_result_root: Path
    result_root: Path
    config_path: Path

    def validate(self) -> None:
        if not self.data_root.is_dir():
            raise ValueError(f"data root does not exist: {self.data_root}")
        source = self.data_root.resolve()
        for path in (
            self.work_root.resolve(),
            self.pipeline_output_root.resolve(),
            self.locked_result_root.resolve(),
            self.result_root.resolve(),
        ):
            if path == source or path.is_relative_to(source):
                raise ValueError("work and result roots must remain outside the source data root")


def _implementation_commit(repository_root: Path) -> str:
    override = os.environ.get("BARNABUS_SCIENTIFIC_IMPLEMENTATION_COMMIT")
    if override:
        return override.strip()
    identity = repository_root / "config" / "scientific-implementation-commit.txt"
    if identity.is_file():
        return identity.read_text(encoding="utf-8").strip()
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository_root, text=True
    ).strip()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _unique_index(
    rows: Sequence[dict[str, str]], key: str, label: str
) -> dict[str, dict[str, str]]:
    output: dict[str, dict[str, str]] = {}
    for row in rows:
        value = row.get(key, "").strip().upper()
        if not value:
            raise ValueError(f"{label} contains a blank {key}")
        if value in output:
            raise ValueError(f"{label} violates unique grain at {key}={value!r}")
        output[value] = row
    return output


def _bounded_float(value: str, label: str, lower: float, upper: float) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < lower or parsed > upper:
        raise ValueError(f"{label} must be finite and within [{lower}, {upper}]")
    return parsed


def _assert_declared_grains(
    tables: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    missing = set(tables) - set(TABLE_GRAINS)
    if missing:
        raise ValueError(f"tables lack declared grain: {sorted(missing)}")
    contracts: list[dict[str, Any]] = []
    for table, rows in sorted(tables.items()):
        keys = TABLE_GRAINS[table]
        seen: set[tuple[Any, ...]] = set()
        for row in rows:
            if any(key not in row or row[key] is None or row[key] == "" for key in keys):
                raise ValueError(f"{table} has null grain key in {keys}")
            grain = tuple(row[key] for key in keys)
            if grain in seen:
                raise ValueError(f"{table} violates declared grain {keys}: {grain}")
            seen.add(grain)
        contracts.append(
            {
                "table": table,
                "declared_grain": ";".join(keys),
                "rows": len(rows),
                "duplicate_grains": 0,
                "null_grain_keys": 0,
                "passed": True,
                "quantity_status": "recomputed_contract",
            }
        )
    return contracts


def _evaluation_ledger(tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    sources = (
        "judge_evaluation",
        "model_operating_point",
        "model_subgroups",
        "decision_curve",
        "uplift_curve",
        "uplift_summary",
    )
    rows: list[dict[str, Any]] = []
    for source in sources:
        for source_row_index, row in enumerate(tables[source], start=1):
            rows.append(
                {
                    "comparison_id": f"F_evaluation-{len(rows) + 1:04d}",
                    "family": "F_evaluation",
                    "source_table": source,
                    "source_row": source_row_index,
                    "comparison": "|".join(
                        str(row.get(key, ""))
                        for key in (
                            "label_source",
                            "context",
                            "dimension",
                            "group",
                            "strategy",
                            "metric",
                            "threshold",
                            "population_fraction",
                        )
                        if key in row
                    ),
                    "formal_null_test_run": False,
                    "raw_p_value": None,
                    "adjusted_p_value": None,
                    "correction_rule": "Benjamini_Yekutieli_FDR_0.05_if_formal_tests_are_run",
                    "reason_no_adjustment": "frozen_sensitivity_rule_estimates_and_intervals_only_no_new_null_tests",
                    "quantity_status": "comparison_registered_no_formal_test",
                }
            )
    return rows


def _design_parameters(config: dict[str, Any]) -> list[dict[str, Any]]:
    study = config["prospective_study"]
    monitoring = config["monitoring"]
    return [
        {"parameter": "study_weeks", "value": int(study["weeks"]), "unit": "weeks", "quantity_status": "assumed_design_parameter"},
        {"parameter": "wash_in_weeks", "value": int(study["wash_in_weeks"]), "unit": "weeks", "quantity_status": "assumed_design_parameter"},
        {"parameter": "primary_endpoint_maturation_days", "value": int(study["primary_endpoint_maturation_days"]), "unit": "days", "quantity_status": "assumed_design_parameter"},
        {"parameter": "target_power", "value": float(study["target_power"]), "unit": "proportion", "quantity_status": "assumed_design_parameter"},
        {"parameter": "futility_conditional_power", "value": float(study["futility_conditional_power"]), "unit": "proportion", "quantity_status": "assumed_design_parameter"},
        {"parameter": "harm_risk_difference", "value": float(study["harm_risk_difference"]), "unit": "risk_difference", "quantity_status": "assumed_design_parameter"},
        {"parameter": "statistical_alert_budget_per_week", "value": int(monitoring["statistical_alert_budget_per_week"]), "unit": "incident_bundles", "quantity_status": "assumed_operating_capacity"},
        {"parameter": "human_weekly_statistical_alert_hours", "value": float(monitoring["human_weekly_statistical_alert_hours"]), "unit": "hours", "quantity_status": "assumed_operating_capacity"},
    ]


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"invalid boolean value in typed input: {value!r}")
    return normalized == "true"


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _metric_interval(
    records: Sequence[dict[str, Any]],
    metric: Callable[[Sequence[dict[str, Any]]], float],
    draws: int,
    seed: int,
) -> tuple[float | None, float | None, float | None, int]:
    estimate, lower, upper, clusters = service_cluster_bootstrap(
        records, metric, draws, seed
    )
    return _finite(estimate), _finite(lower), _finite(upper), clusters


def _component_index(components: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    mapping: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for index, services in enumerate(components["components"], start=1):
        component_id = f"IC-{index:02d}"
        site = str(services[0]).split("-", 1)[0]
        for service in services:
            mapping[str(service)] = component_id
        rows.append(
            {
                "component_id": component_id,
                "site": site,
                "services": ";".join(services),
                "service_count": len(services),
                "quantity_status": "observed_design_input",
            }
        )
    return mapping, rows


def _label_tables(
    data_root: Path,
    episodes: Sequence[Episode],
    draws: int,
    seed: int,
    config: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    episode_index = {episode.case_id: episode for episode in episodes}
    pair_rows = _unique_index(_read_csv(data_root / "labels_pairs.csv"), "pair_id", "labels_pairs")
    judge_rows = _unique_index(
        _read_csv(data_root / "llm_judge_scores.csv"), "pair_id", "llm_judge_scores"
    )
    reviewers = _read_csv(data_root / "labels_reviewers.csv")
    reviewer_index = _unique_index(reviewers, "pair_id", "labels_reviewers")
    if set(judge_rows) != set(pair_rows):
        raise ValueError("judge/pair tables violate one-to-one pair coverage")
    if not set(reviewer_index).issubset(pair_rows):
        raise ValueError("reviewer labels contain pair IDs absent from labels_pairs")
    records: list[dict[str, Any]] = []
    adjudicated_nonblank = 0
    unmatched_cases = 0
    for row in reviewers:
        pair_id = row["pair_id"].strip().upper()
        pair = pair_rows.get(pair_id)
        judge = judge_rows.get(pair_id)
        if pair is None or judge is None:
            raise ValueError(f"reviewer pair lacks pair/judge record: {pair_id}")
        if row.get("adjudicated", "").strip():
            adjudicated_nonblank += 1
            if row["adjudicated"].strip().upper() not in {"PROCEED", "HOLD", "UNRESOLVED"}:
                raise ValueError("adjudicated label violates allowed enum")
        reviewer_1 = row["reviewer_1"].strip().upper()
        reviewer_2 = row["reviewer_2"].strip().upper()
        if reviewer_1 not in {"PROCEED", "HOLD"} or reviewer_2 not in {"PROCEED", "HOLD"}:
            raise ValueError("reviewer label violates PROCEED/HOLD enum")
        episode = episode_index.get(pair["case_id"].strip().upper())
        unmatched_cases += episode is None
        service = episode.service_code if episode is not None else "UNKNOWN"
        verdict = judge["llm_judge_verdict"].strip().upper()
        if verdict not in {"PROCEED", "HOLD"}:
            raise ValueError("LLM judge verdict violates PROCEED/HOLD enum")
        confidence = _bounded_float(
            judge["llm_judge_score"], "llm_judge_score", 0.0, 1.0
        )
        probability = confidence if verdict == "PROCEED" else 1.0 - confidence
        records.append(
            {
                "pair_id": pair_id,
                "case_id": pair["case_id"].strip().upper(),
                "service_code": service,
                "reviewer_1": int(reviewer_1 == "PROCEED"),
                "reviewer_2": int(reviewer_2 == "PROCEED"),
                "judge_probability": probability,
                "judge_verdict": verdict,
            }
        )

    agreement_rows: list[dict[str, Any]] = []
    for metric_index, metric_name in enumerate(
        ("raw_agreement", "chance_excess", "cohen_kappa", "gwet_ac1")
    ):
        def metric(sample: Sequence[dict[str, Any]], name: str = metric_name) -> float:
            values = agreement_metrics(
                [int(item["reviewer_1"]) for item in sample],
                [int(item["reviewer_2"]) for item in sample],
            )
            return float(values[name])

        estimate, lower, upper, clusters = _metric_interval(
            records, metric, draws, seed + metric_index
        )
        agreement_rows.append(
            {
                "comparison": "reviewer_1_vs_reviewer_2",
                "metric": metric_name,
                "estimate": estimate,
                "lower": lower,
                "upper": upper,
                "interval_method": "service_cluster_percentile_bootstrap",
                "pairs": len(records),
                "service_clusters": clusters,
                "adjudicated_nonblank": adjudicated_nonblank,
                "quantity_status": "supplied_clinician_labels_observed",
                "interpretation": "agreement_not_ground_truth_no_adjudication",
            }
        )

    classification_counts: dict[str, int] = defaultdict(int)
    for record in records:
        if record["reviewer_1"] == record["reviewer_2"] == 1:
            category = "candidate_created_agreement_proceed"
        elif record["reviewer_1"] == record["reviewer_2"] == 0:
            category = "candidate_created_agreement_hold"
        else:
            category = "candidate_created_reviewer_disagreement"
        classification_counts[category] += 1
    label_provenance = [
        {
            "field_or_classification": "reviewer_1",
            "source_type": "supplied_clinician_label",
            "count": len(records),
            "ground_truth_status": "reviewer_specific_not_adjudicated",
            "quantity_status": "observed",
        },
        {
            "field_or_classification": "reviewer_2",
            "source_type": "supplied_clinician_label",
            "count": len(records),
            "ground_truth_status": "reviewer_specific_not_adjudicated",
            "quantity_status": "observed",
        },
        {
            "field_or_classification": "adjudicated",
            "source_type": "supplied_empty_field",
            "count": adjudicated_nonblank,
            "ground_truth_status": "no_adjudication_supplied",
            "quantity_status": "observed",
        },
    ]
    for category, count in sorted(classification_counts.items()):
        label_provenance.append(
            {
                "field_or_classification": category,
                "source_type": "candidate_created_descriptive_classification",
                "count": count,
                "ground_truth_status": "not_a_clinical_label",
                "quantity_status": "derived_observed",
            }
        )

    adjudication_protocol = [
        {
            "step": 1,
            "procedure": "Pre-register label definitions, source documents, abstention criteria, and conflict rules with clinical governance approval.",
            "actor": "clinical_governance_lead",
            "blinding": "before_any_judge_score_or_candidate_classification",
            "output": "versioned_label_manual",
        },
        {
            "step": 2,
            "procedure": "Have two qualified clinicians label independently from the same immutable case packet, blinded to each other and to all model outputs.",
            "actor": "reviewer_1_and_reviewer_2",
            "blinding": "mutual_and_model_blinded",
            "output": "two_signed_labels_plus_abstain_reason",
        },
        {
            "step": 3,
            "procedure": "Send disagreements and prespecified quality-control agreements to an independent third clinician who was not involved in model development.",
            "actor": "authorized_third_clinician",
            "blinding": "blind_to_reviewer_identity_model_judge_and_candidate",
            "output": "adjudicator_label_and_rationale_code",
        },
        {
            "step": 4,
            "procedure": "Permit an explicit UNRESOLVED outcome; never force a binary label when source evidence is insufficient.",
            "actor": "third_clinician",
            "blinding": "maintained",
            "output": "PROCEED_HOLD_or_UNRESOLVED",
        },
        {
            "step": 5,
            "procedure": "Lock the packet hash, label-manual version, reviewer credentials, timestamps, and adjudication audit trail before evaluating the judge.",
            "actor": "data_steward",
            "blinding": "unblind_only_after_label_lock",
            "output": "adjudicated_label_version",
        },
    ]

    judge_rows_output: list[dict[str, Any]] = []
    judge_threshold = float(config["labels"]["judge_operating_threshold"])
    label_sets: list[tuple[str, list[dict[str, Any]], Callable[[dict[str, Any]], int]]] = [
        ("reviewer_1", records, lambda row: int(row["reviewer_1"])),
        ("reviewer_2", records, lambda row: int(row["reviewer_2"])),
        (
            "agreement_only_consensus",
            [row for row in records if row["reviewer_1"] == row["reviewer_2"]],
            lambda row: int(row["reviewer_1"]),
        ),
    ]
    key_metrics = (
        "sensitivity",
        "specificity",
        "ppv",
        "npv",
        "balanced_accuracy",
        "auc",
        "brier",
        "log_loss",
    )
    for set_index, (label_source, sample, label_getter) in enumerate(label_sets):
        labels = [label_getter(row) for row in sample]
        probabilities = [float(row["judge_probability"]) for row in sample]
        intervals = service_cluster_classification_intervals(
            sample,
            label_getter=label_getter,
            score_getter=lambda row: float(row["judge_probability"]),
            threshold=judge_threshold,
            draws=draws,
            seed=seed + 100 + set_index * 20,
        )
        for metric_index, metric_name in enumerate(key_metrics):
            estimate, lower, upper, clusters = intervals[metric_name]
            judge_rows_output.append(
                {
                    "label_source": label_source,
                    "family_relation": config["labels"]["family_relation"],
                    "metric": metric_name,
                    "estimate": estimate,
                    "lower": lower,
                    "upper": upper,
                    "pairs": len(sample),
                    "service_clusters": clusters,
                    "threshold": judge_threshold,
                    "interval_method": "service_cluster_percentile_bootstrap",
                    "quantity_status": "assumed_probability_mapping"
                    if metric_name in {"auc", "brier", "log_loss"}
                    else "observed_reviewer_specific",
                    "interpretation": "same_family_dependent_no_adjudicated_ground_truth",
                }
            )
        for extra_index, (metric_name, metric_function) in enumerate(
            (
                (
                    "average_precision",
                    lambda bootstrap_sample, getter=label_getter: average_precision(
                        [getter(row) for row in bootstrap_sample],
                        [float(row["judge_probability"]) for row in bootstrap_sample],
                    ),
                ),
                (
                    "weighted_agreement_binary_kappa",
                    lambda bootstrap_sample, getter=label_getter: agreement_metrics(
                        [getter(row) for row in bootstrap_sample],
                        [
                            int(float(row["judge_probability"]) >= judge_threshold)
                            for row in bootstrap_sample
                        ],
                    )["cohen_kappa"],
                ),
            )
        ):
            estimate, lower, upper, clusters = _metric_interval(
                sample,
                metric_function,
                draws,
                seed + 170 + set_index * 20 + extra_index,
            )
            judge_rows_output.append(
                {
                    "label_source": label_source,
                    "family_relation": config["labels"]["family_relation"],
                    "metric": metric_name,
                    "estimate": estimate,
                    "lower": lower,
                    "upper": upper,
                    "pairs": len(sample),
                    "service_clusters": clusters,
                    "threshold": judge_threshold,
                    "interval_method": "service_cluster_percentile_bootstrap",
                    "quantity_status": "assumed_probability_mapping"
                    if metric_name == "average_precision"
                    else "observed_reviewer_specific",
                    "interpretation": "same_family_dependent_no_adjudicated_ground_truth",
                }
            )
        calibration_intercept, calibration_slope = logistic_calibration(labels, probabilities)
        for metric_name, value in (
            ("calibration_intercept", calibration_intercept),
            ("calibration_slope", calibration_slope),
        ):
            judge_rows_output.append(
                {
                    "label_source": label_source,
                    "family_relation": config["labels"]["family_relation"],
                    "metric": metric_name,
                    "estimate": _finite(value),
                    "lower": None,
                    "upper": None,
                    "pairs": len(sample),
                    "service_clusters": len({str(row["service_code"]) for row in sample}),
                    "threshold": judge_threshold,
                    "interval_method": "not_estimable_when_logistic_fit_singular",
                    "quantity_status": "assumed_probability_mapping",
                    "interpretation": "same_family_dependent_no_adjudicated_ground_truth",
                }
            )

    family_dependence = [
        {
            "family_relation": config["labels"]["family_relation"],
            "pairs": len(records),
            "available": True,
            "independent_validation": False,
            "interpretation": "shared_family_errors_can_be_correlated",
            "quantity_status": "supplied_design_fact",
        },
        {
            "family_relation": "different_model_family",
            "pairs": 0,
            "available": False,
            "independent_validation": None,
            "interpretation": "not_estimable_no_different_family_judge_scores",
            "quantity_status": "not_estimable",
        },
    ]
    by_key = {(row["label_source"], row["metric"]): row for row in judge_rows_output}
    sensitivity_lower = by_key[("reviewer_1", "sensitivity")]["lower"]
    specificity_lower = by_key[("reviewer_1", "specificity")]["lower"]
    judge_gates = [
        {"gate": "unadjusted_sensitivity_lower_at_least_0.90", "passed": bool(sensitivity_lower is not None and sensitivity_lower >= float(config["labels"]["autonomous_minimum_sensitivity_lower"])), "evidence": sensitivity_lower, "quantity_status": "decision_rule_input"},
        {"gate": "unadjusted_specificity_lower_at_least_0.90", "passed": bool(specificity_lower is not None and specificity_lower >= float(config["labels"]["autonomous_minimum_specificity_lower"])), "evidence": specificity_lower, "quantity_status": "decision_rule_input"},
        {"gate": "authorized_adjudicated_ground_truth_available", "passed": adjudicated_nonblank > 0, "evidence": adjudicated_nonblank, "quantity_status": "observed"},
        {"gate": "independent_different_family_validation_available", "passed": False, "evidence": 0, "quantity_status": "not_supplied"},
        {"gate": "zero_authorization_violations_verified", "passed": False, "evidence": None, "quantity_status": "not_estimable_authorization_model_not_opened"},
        {"gate": "multiplicity_adjusted_accuracy_bounds_available", "passed": False, "evidence": None, "quantity_status": "not_estimable_no_formal_accuracy_tests"},
    ]
    autonomous_fit = all(bool(row["passed"]) for row in judge_gates)
    judge_use = [
        {
            "use_case": "autonomous_clinical_scoring",
            "decision": "fit" if autonomous_fit else "not_fit",
            "reason": "fails_frozen_accuracy_independence_and_adjudication_gates",
            "candidate_created_decision": True,
            "quantity_status": "decision_rule_output",
        },
        {
            "use_case": "clinical_triage_support",
            "decision": "not_fit_on_supplied_evidence",
            "reason": "poor_discrimination_and_specificity_no_independent_ground_truth",
            "candidate_created_decision": True,
            "quantity_status": "decision_rule_output",
        },
        {
            "use_case": "offline_research_error_sampling",
            "decision": "narrowly_defensible_with_human_review",
            "reason": "non_patient_facing_sampling_only_no_label_override",
            "candidate_created_decision": True,
            "quantity_status": "decision_rule_output",
        },
    ]
    return {
        "reviewer_agreement": agreement_rows,
        "label_provenance": label_provenance,
        "adjudication_protocol": adjudication_protocol,
        "judge_evaluation": judge_rows_output,
        "judge_family_dependence": family_dependence,
        "judge_use_decision": judge_use,
        "judge_use_gates": judge_gates,
        "label_flow": [
            {"flow": "supplied_pairs", "rows": len(pair_rows), "quantity_status": "observed"},
            {"flow": "supplied_judge_scores", "rows": len(judge_rows), "quantity_status": "observed"},
            {"flow": "double_reviewed_pairs", "rows": len(reviewers), "quantity_status": "observed"},
            {"flow": "reviewed_pairs_unmatched_to_episode", "rows": unmatched_cases, "quantity_status": "observed"},
            {"flow": "candidate_created_adjudications", "rows": 0, "quantity_status": "none_created"},
        ],
    }


def _temporal_category(episode: Episode, scored_date: date) -> str:
    if scored_date < episode.referral_date:
        return "pre_referral_time_zero_misalignment"
    candidate_days = [
        value
        for value in (
            episode.days_to_recommendation,
            episode.days_to_ready,
            episode.days_to_close,
            episode.days_to_complete,
        )
        if value is not None
    ]
    first_post_date = (
        episode.referral_date + timedelta(days=math.floor(min(candidate_days)))
        if candidate_days
        else None
    )
    if first_post_date is not None and scored_date > first_post_date:
        return "explicitly_after_recommendation_or_endpoint"
    if scored_date == episode.referral_date:
        return "same_referral_day_order_unknown"
    return "post_referral_feature_window_unknown"


def _model_tables(
    data_root: Path,
    episodes: Sequence[Episode],
    draws: int,
    seed: int,
    config: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    episode_index = {episode.case_id: episode for episode in episodes}
    records: list[dict[str, Any]] = []
    raw_rows = _read_csv(data_root / "model_scores.csv")
    _unique_index(raw_rows, "case_id", "model_scores")
    raw_thresholds: set[float] = set()
    unmatched_cases = 0
    unobserved_proxy_outcomes = 0
    for row in raw_rows:
        case_id = row["case_id"].strip().upper()
        score_batch = _bounded_float(row["score_batch"], "score_batch", 0.0, 1.0)
        score_live = _bounded_float(row["score_live"], "score_live", 0.0, 1.0)
        threshold_used = _bounded_float(row["threshold_used"], "threshold_used", 0.0, 1.0)
        if threshold_used in {0.0, 1.0}:
            raise ValueError("threshold_used must be strictly between zero and one")
        raw_thresholds.add(threshold_used)
        episode = episode_index.get(case_id)
        if episode is None:
            unmatched_cases += 1
            continue
        observed, outcome = _c1_observation(episode)
        if not observed or outcome is None:
            unobserved_proxy_outcomes += 1
            continue
        scored_date = date.fromisoformat(row["scored_ts"].strip())
        records.append(
            {
                "case_id": case_id,
                "service_code": episode.service_code,
                "site": episode.site,
                "sex": episode.sex or "UNKNOWN",
                "age_band": "age_lt_40"
                if episode.age is not None and episode.age < 40
                else "age_40_64"
                if episode.age is not None and episode.age < 65
                else "age_ge_65"
                if episode.age is not None
                else "UNKNOWN",
                "referral_date": episode.referral_date,
                "score_batch": score_batch,
                "score": score_live,
                "feature_visit_null": _boolean(row["feature_visit_null"]),
                "threshold": threshold_used,
                "scored_date": scored_date,
                "temporal_category": _temporal_category(episode, scored_date),
                "outcome": int(outcome),
            }
        )
    if not records:
        raise ValueError("no recommendation model rows align to an observed proxy outcome")
    thresholds = sorted(raw_thresholds)
    if len(thresholds) != 1:
        raise ValueError(f"scientific-v1 expects one actually used threshold, found {thresholds}")
    actual_threshold = thresholds[0]
    contexts = [
        ("all_observed_proxy_outcomes", records),
        (
            "same_referral_day_only",
            [row for row in records if row["temporal_category"] == "same_referral_day_order_unknown"],
        ),
    ]
    operating_rows: list[dict[str, Any]] = []
    calibration_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    consequence_rows: list[dict[str, Any]] = []
    performance_metrics = (
        "sensitivity",
        "specificity",
        "ppv",
        "npv",
        "balanced_accuracy",
        "auc",
        "brier",
        "log_loss",
    )
    for context_index, (context, sample) in enumerate(contexts):
        if not sample:
            continue
        intervals = service_cluster_classification_intervals(
            sample,
            label_getter=lambda row: int(row["outcome"]),
            score_getter=lambda row: float(row["score"]),
            threshold=actual_threshold,
            draws=draws,
            seed=seed + 400 + context_index * 30,
        )
        for metric_index, metric_name in enumerate(performance_metrics):
            estimate, lower, upper, clusters = intervals[metric_name]
            operating_rows.append(
                {
                    "context": context,
                    "metric": metric_name,
                    "estimate": estimate,
                    "lower": lower,
                    "upper": upper,
                    "rows": len(sample),
                    "service_clusters": clusters,
                    "threshold_used": actual_threshold,
                    "interval_method": "service_cluster_percentile_bootstrap",
                    "validity": config["recommendation_model"]["performance_disposition"],
                    "quantity_status": "invalidated_sensitivity_only",
                    "limitation": "feature_lineage_and_authorized_target_definition_absent",
                }
            )
        labels = [int(row["outcome"]) for row in sample]
        scores = [float(row["score"]) for row in sample]
        predictions = [int(score >= actual_threshold) for score in scores]
        true_positive = sum(
            label == 1 and prediction == 1
            for label, prediction in zip(labels, predictions, strict=True)
        )
        false_positive = sum(
            label == 0 and prediction == 1
            for label, prediction in zip(labels, predictions, strict=True)
        )
        false_negative = sum(
            label == 1 and prediction == 0
            for label, prediction in zip(labels, predictions, strict=True)
        )
        true_negative = sum(
            label == 0 and prediction == 0
            for label, prediction in zip(labels, predictions, strict=True)
        )
        consequence_rows.append(
            {
                "context": context,
                "threshold_used": actual_threshold,
                "rows": len(sample),
                "true_positive_proxy": true_positive,
                "false_positive_proxy": false_positive,
                "false_negative_proxy": false_negative,
                "true_negative_proxy": true_negative,
                "flagged_per_100": 100.0 * (true_positive + false_positive) / len(sample),
                "missed_proxy_outcomes_per_100": 100.0 * false_negative / len(sample),
                "unnecessary_flags_per_100": 100.0 * false_positive / len(sample),
                "threshold_implied_false_positive_to_true_positive_cost_ratio": actual_threshold
                / (1.0 - actual_threshold),
                "validity": config["recommendation_model"]["performance_disposition"],
                "quantity_status": "invalidated_sensitivity_only",
                "limitation": "counts_use_unauthorized_proxy_and_do_not_measure_actual_clinical_consequences",
            }
        )
        calibration_intercept, calibration_slope = logistic_calibration(labels, scores)
        for metric_name, value in (
            ("calibration_intercept", calibration_intercept),
            ("calibration_slope", calibration_slope),
            ("predicted_positive_fraction", mean([float(score >= actual_threshold) for score in scores])),
            ("threshold_harm_to_benefit_ratio", actual_threshold / (1.0 - actual_threshold)),
            ("net_benefit_at_used_threshold", net_benefit(labels, scores, actual_threshold)),
        ):
            operating_rows.append(
                {
                    "context": context,
                    "metric": metric_name,
                    "estimate": _finite(value),
                    "lower": None,
                    "upper": None,
                    "rows": len(sample),
                    "service_clusters": len({str(row["service_code"]) for row in sample}),
                    "threshold_used": actual_threshold,
                    "interval_method": "point_only",
                    "validity": config["recommendation_model"]["performance_disposition"],
                    "quantity_status": "invalidated_sensitivity_only",
                    "limitation": "threshold_consequence_ratio_assumes_score_is_calibrated_probability",
                }
            )
        for row in service_cluster_reliability_bins(
            sample,
            int(config["recommendation_model"]["reliability_bins"]),
            draws,
            seed + 600 + context_index,
        ):
            calibration_rows.append(
                {
                    "context": context,
                    **row,
                    "validity": config["recommendation_model"]["performance_disposition"],
                    "quantity_status": "invalidated_sensitivity_only",
                    "limitation": "outcome_proxy_and_feature_lineage_not_clinically_authorized",
                }
            )
        prevalence = mean([float(value) for value in labels])
        for threshold in [float(value) for value in config["recommendation_model"]["decision_curve_thresholds"]]:
            decision_rows.extend(
                [
                    {
                        "context": context,
                        "strategy": "model",
                        "threshold": threshold,
                        "net_benefit": net_benefit(labels, scores, threshold),
                        "rows": len(sample),
                        "validity": config["recommendation_model"]["performance_disposition"],
                        "quantity_status": "invalidated_sensitivity_only",
                    },
                    {
                        "context": context,
                        "strategy": "treat_all",
                        "threshold": threshold,
                        "net_benefit": prevalence - (1.0 - prevalence) * threshold / (1.0 - threshold),
                        "rows": len(sample),
                        "validity": config["recommendation_model"]["performance_disposition"],
                        "quantity_status": "invalidated_sensitivity_only",
                    },
                    {
                        "context": context,
                        "strategy": "treat_none",
                        "threshold": threshold,
                        "net_benefit": 0.0,
                        "rows": len(sample),
                        "validity": config["recommendation_model"]["performance_disposition"],
                        "quantity_status": "invalidated_sensitivity_only",
                    },
                ]
            )

    subgroup_rows: list[dict[str, Any]] = []
    subgroup_definitions = (
        ("site", lambda row: str(row["site"])),
        ("sex", lambda row: str(row["sex"])),
        ("age_band", lambda row: str(row["age_band"])),
        ("service", lambda row: str(row["service_code"])),
    )
    for dimension_index, (dimension, getter) in enumerate(subgroup_definitions):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            groups[getter(record)].append(record)
        for group_index, (group, sample) in enumerate(sorted(groups.items())):
            intervals = service_cluster_classification_intervals(
                sample,
                label_getter=lambda row: int(row["outcome"]),
                score_getter=lambda row: float(row["score"]),
                threshold=actual_threshold,
                draws=draws,
                seed=seed + 800 + dimension_index * 200 + group_index * 10,
                requested_metrics=("sensitivity", "specificity", "brier", "net_benefit"),
            )
            for metric_index, metric_name in enumerate(
                ("sensitivity", "specificity", "brier", "net_benefit")
            ):
                estimate, lower, upper, clusters = intervals[metric_name]
                subgroup_rows.append(
                    {
                        "dimension": dimension,
                        "group": group,
                        "metric": metric_name,
                        "estimate": estimate,
                        "lower": lower,
                        "upper": upper,
                        "rows": len(sample),
                        "service_clusters": clusters,
                        "interval_method": "service_cluster_percentile_bootstrap"
                        if clusters >= 5 and len(sample) >= 20
                        else "suppressed_fewer_than_five_service_clusters_or_twenty_episodes",
                        "inferential_status": "reported"
                        if clusters >= 5 and len(sample) >= 20
                        else "suppressed",
                        "validity": config["recommendation_model"]["performance_disposition"],
                        "quantity_status": "invalidated_sensitivity_only",
                    }
                )
                if clusters < 5 or len(sample) < 20:
                    subgroup_rows[-1]["lower"] = None
                    subgroup_rows[-1]["upper"] = None

    temporal_counts: dict[str, int] = defaultdict(int)
    for record in records:
        temporal_counts[str(record["temporal_category"])] += 1
    leakage_rows = [
        {
            "check": category,
            "affected_rows": count,
            "total_rows": len(records),
            "affected_fraction": count / len(records),
            "result": "invalidates_affected_performance",
            "quantity_status": "observed",
        }
        for category, count in sorted(temporal_counts.items())
    ]
    batch_live_changed = sum(
        not math.isclose(float(row["score_batch"]), float(row["score"]), abs_tol=1e-15)
        for row in records
    )
    null_changed = sum(
        bool(row["feature_visit_null"])
        and not math.isclose(float(row["score_batch"]), float(row["score"]), abs_tol=1e-15)
        for row in records
    )
    leakage_rows.extend(
        [
            {
                "check": "batch_live_score_changed",
                "affected_rows": batch_live_changed,
                "total_rows": len(records),
                "affected_fraction": batch_live_changed / len(records),
                "result": "transport_mismatch",
                "quantity_status": "observed",
            },
            {
                "check": "feature_visit_null_and_score_changed",
                "affected_rows": null_changed,
                "total_rows": len(records),
                "affected_fraction": null_changed / len(records),
                "result": "null_handling_shift",
                "quantity_status": "observed",
            },
            {
                "check": "feature_lineage_available",
                "affected_rows": len(records),
                "total_rows": len(records),
                "affected_fraction": 1.0,
                "result": "absent_invalidates_all_clinical_performance",
                "quantity_status": "not_supplied",
            },
            {
                "check": "target_and_threshold_selection_period_available",
                "affected_rows": len(records),
                "total_rows": len(records),
                "affected_fraction": 1.0,
                "result": "absent_invalidates_all_clinical_performance",
                "quantity_status": "not_supplied",
            },
        ]
    )
    return {
        "model_operating_point": operating_rows,
        "model_calibration": calibration_rows,
        "decision_curve": decision_rows,
        "threshold_consequences": consequence_rows,
        "model_subgroups": subgroup_rows,
        "leakage_audit": leakage_rows,
        "model_flow": [
            {"flow": "supplied_score_rows", "rows": len(raw_rows), "quantity_status": "observed"},
            {"flow": "unmatched_case_rows", "rows": unmatched_cases, "quantity_status": "observed"},
            {"flow": "proxy_outcome_not_observed", "rows": unobserved_proxy_outcomes, "quantity_status": "observed"},
            {"flow": "evaluated_proxy_rows", "rows": len(records), "quantity_status": "observed"},
            {"flow": "distinct_thresholds_across_all_supplied_rows", "rows": len(raw_thresholds), "quantity_status": "observed"},
        ],
        "_model_records": records,
    }


def _observed_uplift(sample: Sequence[dict[str, Any]]) -> float:
    targeted = [int(row["outcome"]) for row in sample if bool(row["targeted"])]
    not_targeted = [int(row["outcome"]) for row in sample if not bool(row["targeted"])]
    if not targeted or not not_targeted:
        return math.nan
    # Positive means fewer proxy cancellations among targeted cases.
    return mean([float(value) for value in not_targeted]) - mean(
        [float(value) for value in targeted]
    )


def _trapezoid(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 2:
        return math.nan
    return math.fsum(
        (right_x - left_x) * (left_y + right_y) / 2.0
        for (left_x, left_y), (right_x, right_y) in zip(points, points[1:])
    )


def _uplift_tables(
    data_root: Path,
    episodes: Sequence[Episode],
    draws: int,
    seed: int,
    config: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    episode_index = {episode.case_id: episode for episode in episodes}
    records: list[dict[str, Any]] = []
    supplied_auc_values: set[float] = set()
    raw_rows = _read_csv(data_root / "uplift_targeting.csv")
    _unique_index(raw_rows, "case_id", "uplift_targeting")
    unmatched_cases = 0
    unobserved_proxy_outcomes = 0
    for row in raw_rows:
        case_id = row["case_id"].strip().upper()
        uplift_score = float(row["uplift_score"])
        supplied_auc = _bounded_float(
            row["model_auc_reported"], "model_auc_reported", 0.0, 1.0
        )
        if not math.isfinite(uplift_score):
            raise ValueError("uplift_score must be finite")
        targeted_value = _boolean(row["targeted"])
        episode = episode_index.get(case_id)
        if episode is None:
            unmatched_cases += 1
            continue
        observed, outcome = _c1_observation(episode)
        if not observed or outcome is None:
            unobserved_proxy_outcomes += 1
            continue
        supplied_auc_values.add(supplied_auc)
        records.append(
            {
                "case_id": case_id,
                "service_code": episode.service_code,
                "site": episode.site,
                "referral_date": episode.referral_date,
                "uplift_score": uplift_score,
                "targeted": targeted_value,
                "outcome": int(outcome),
            }
        )
    if not records:
        raise ValueError("no uplift rows align to an observed proxy outcome")
    ordered = sorted(records, key=lambda row: (-float(row["uplift_score"]), str(row["case_id"])))
    full_effect = _observed_uplift(ordered)
    curve_rows: list[dict[str, Any]] = []
    supported_points: list[tuple[float, float]] = [(0.0, 0.0)]
    for fraction in [float(value) for value in config["uplift"]["fractions"]]:
        top_count = max(1, int(math.ceil(fraction * len(ordered))))
        sample = ordered[:top_count]
        targeted = [row for row in sample if bool(row["targeted"])]
        not_targeted = [row for row in sample if not bool(row["targeted"])]
        effect = _observed_uplift(sample)
        gain = effect * top_count / len(ordered) if math.isfinite(effect) else math.nan
        random_gain = full_effect * fraction if math.isfinite(full_effect) else math.nan
        qini_gain = gain - random_gain if math.isfinite(gain) and math.isfinite(random_gain) else math.nan
        if math.isfinite(gain):
            supported_points.append((fraction, gain))
        curve_rows.append(
            {
                "population_fraction": fraction,
                "top_rows": top_count,
                "targeted_rows": len(targeted),
                "not_targeted_rows": len(not_targeted),
                "mean_uplift_score": mean([float(row["uplift_score"]) for row in sample]),
                "observed_not_targeted_minus_targeted_proxy_risk": _finite(effect),
                "cumulative_gain_per_total_population": _finite(gain),
                "random_targeting_gain": _finite(random_gain),
                "diagnostic_qini_gain": _finite(qini_gain),
                "overlap_supported": bool(targeted and not_targeted),
                "causal_validity": "invalidated_not_causally_identified",
                "quantity_status": "diagnostic_observed_association",
                "limitation": ";".join(config["uplift"]["invalidation_reasons"]),
            }
        )

    overlap_rows: list[dict[str, Any]] = []
    bin_count = int(config["uplift"]["calibration_bins"])
    ascending = sorted(records, key=lambda row: (float(row["uplift_score"]), str(row["case_id"])))
    for bin_index in range(bin_count):
        lower = bin_index * len(ascending) // bin_count
        upper = (bin_index + 1) * len(ascending) // bin_count
        sample = ascending[lower:upper]
        targeted = [row for row in sample if bool(row["targeted"])]
        not_targeted = [row for row in sample if not bool(row["targeted"])]
        overlap_rows.append(
            {
                "score_bin": bin_index + 1,
                "rows": len(sample),
                "score_min": min(float(row["uplift_score"]) for row in sample),
                "score_max": max(float(row["uplift_score"]) for row in sample),
                "mean_score": mean([float(row["uplift_score"]) for row in sample]),
                "targeted_rows": len(targeted),
                "not_targeted_rows": len(not_targeted),
                "targeted_fraction": len(targeted) / len(sample),
                "observed_not_targeted_minus_targeted_proxy_risk": _finite(_observed_uplift(sample)),
                "overlap_supported": bool(targeted and not_targeted),
                "score_scale_calibration_status": "not_interpretable_scale_not_defined_as_effect",
                "quantity_status": "diagnostic_observed_association",
            }
        )

    supported_points = sorted(set(supported_points))
    supported_auuc = _trapezoid(supported_points)
    qini_points = [
        (
            float(row["population_fraction"]),
            float(row["diagnostic_qini_gain"]),
        )
        for row in curve_rows
        if row["diagnostic_qini_gain"] is not None
    ]
    if qini_points and qini_points[0][0] > 0.0:
        qini_points.insert(0, (0.0, 0.0))
    diagnostic_qini = _trapezoid(qini_points)
    top_30 = ordered[: max(1, int(math.ceil(0.30 * len(ordered))))]
    top_estimate, top_lower, top_upper, top_clusters = _metric_interval(
        top_30, _observed_uplift, draws, seed + 1200
    )
    targeted_fraction = mean([float(bool(row["targeted"])) for row in records])
    summary_rows = [
        {
            "metric": "targeted_fraction",
            "estimate": targeted_fraction,
            "lower": None,
            "upper": None,
            "rows": len(records),
            "service_clusters": len({str(row["service_code"]) for row in records}),
            "causal_validity": "descriptive_only",
            "quantity_status": "observed",
        },
        {
            "metric": "diagnostic_ranked_association_area_supported_range",
            "estimate": _finite(supported_auuc),
            "lower": None,
            "upper": None,
            "rows": len(records),
            "service_clusters": len({str(row["service_code"]) for row in records}),
            "causal_validity": "invalidated_not_causally_identified",
            "quantity_status": "diagnostic_observed_association",
        },
        {
            "metric": "diagnostic_qini_shaped_association_coefficient",
            "estimate": _finite(diagnostic_qini),
            "lower": None,
            "upper": None,
            "rows": len(records),
            "service_clusters": len({str(row["service_code"]) for row in records}),
            "causal_validity": "invalidated_not_causally_identified",
            "quantity_status": "diagnostic_observed_association",
        },
        {
            "metric": "top_30_percent_observed_targeting_association",
            "estimate": top_estimate,
            "lower": top_lower,
            "upper": top_upper,
            "rows": len(top_30),
            "service_clusters": top_clusters,
            "causal_validity": "invalidated_not_causally_identified",
            "quantity_status": "diagnostic_observed_association"
            if top_estimate is not None and math.isfinite(top_estimate)
            else "not_estimable_no_targeted_not_targeted_overlap",
        },
        {
            "metric": "causal_auuc_qini_or_policy_value",
            "estimate": None,
            "lower": None,
            "upper": None,
            "rows": len(records),
            "service_clusters": len({str(row["service_code"]) for row in records}),
            "causal_validity": "not_estimable",
            "quantity_status": "not_estimable",
        },
        {
            "metric": "supplied_classification_auc_not_uplift_validation",
            "estimate": next(iter(supplied_auc_values)) if len(supplied_auc_values) == 1 else None,
            "lower": None,
            "upper": None,
            "rows": len(records),
            "service_clusters": len({str(row["service_code"]) for row in records}),
            "causal_validity": "invalid_metric_unreproduced",
            "quantity_status": "unreproduced_supplied_number",
        },
    ]
    return {
        "uplift_curve": curve_rows,
        "uplift_overlap_calibration": overlap_rows,
        "uplift_summary": summary_rows,
        "uplift_flow": [
            {"flow": "supplied_targeting_rows", "rows": len(raw_rows), "quantity_status": "observed"},
            {"flow": "unmatched_case_rows", "rows": unmatched_cases, "quantity_status": "observed"},
            {"flow": "proxy_outcome_not_observed", "rows": unobserved_proxy_outcomes, "quantity_status": "observed"},
            {"flow": "diagnostic_rows", "rows": len(records), "quantity_status": "observed"},
        ],
    }


def _prospective_components(
    connection: duckdb.DuckDBPyConnection,
    artifact_directory: Path,
    episodes: Sequence[Episode],
    switch: date,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    clinician_rows = connection.execute(
        """
        SELECT clinician_id, list_sort(list_distinct(list(service_code))) services
        FROM read_parquet(?)
        WHERE cast(event_ts_local AS DATE) < ? AND is_workflow_event
        GROUP BY 1 ORDER BY 1
        """,
        [str(artifact_directory / "canonical_events.parquet"), switch],
    ).fetchall()
    services = sorted(
        {
            episode.service_code
            for episode in episodes
            if episode.referral_date < switch and episode.service_code != "UNKNOWN"
        }
    )
    parent = {service: service for service in services}

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        if left not in parent or right not in parent:
            return
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for _, linked_services in clinician_rows:
        for service in linked_services[1:]:
            union(str(linked_services[0]), str(service))
    ward_services: dict[str, set[str]] = defaultdict(set)
    for episode in episodes:
        if episode.referral_date < switch:
            ward_services[episode.ward_id].add(episode.service_code)
    for linked_services in ward_services.values():
        ordered = sorted(linked_services)
        for service in ordered[1:]:
            union(ordered[0], service)
    components: dict[str, list[str]] = defaultdict(list)
    for service in services:
        components[find(service)].append(service)
    mapping: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for index, linked_services in enumerate(
        (sorted(values) for _, values in sorted(components.items())), start=1
    ):
        component_id = f"IC-{index:02d}"
        sites = sorted({service.split("-", 1)[0] for service in linked_services})
        for service in linked_services:
            mapping[service] = component_id
        rows.append(
            {
                "component_id": component_id,
                "site": ";".join(sites),
                "services": ";".join(linked_services),
                "service_count": len(linked_services),
                "component_source": "candidate_inferred_pre_switch_clinician_plus_ward_graph",
                "independence_status": "provisional_requires_operational_validation",
                "quantity_status": "derived_design_input",
            }
        )
    return mapping, rows


def _prospective_tables(
    episodes: Sequence[Episode],
    component_mapping: dict[str, str],
    component_rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    study = config["prospective_study"]
    seed = int(config["inference"]["seed"])
    by_site: dict[str, list[str]] = defaultdict(list)
    for row in component_rows:
        by_site[str(row["site"])].append(str(row["component_id"]))
    if sorted(by_site) != ["A", "B"] or any(
        len(values) != int(study["sequences"]) for values in by_site.values()
    ):
        raise ValueError(
            "site-stratified four-sequence design is infeasible under inferred component graph"
        )
    generator = random.Random(seed + 2000)
    for values in by_site.values():
        values.sort()
        generator.shuffle(values)
    rollout_base = [int(value) for value in study["rollout_weeks_zero_based"]]
    wash_in = int(study["wash_in_weeks"])
    assignment: dict[str, tuple[int, int]] = {}
    for sequence in range(int(study["sequences"])):
        for site in sorted(by_site):
            assignment[by_site[site][sequence]] = (sequence + 1, rollout_base[sequence])

    episode_groups: dict[str, list[Episode]] = defaultdict(list)
    for episode in episodes:
        component = component_mapping.get(episode.service_code)
        if component is not None:
            episode_groups[component].append(episode)
    assignment_rows: list[dict[str, Any]] = []
    base_rates: list[float] = []
    weekly_counts: list[int] = []
    rollout_for_power: list[int] = []
    ordered_components = sorted(assignment)
    for component in ordered_components:
        sample = episode_groups[component]
        observations = [_c1_observation(episode) for episode in sample]
        outcomes = [int(outcome) for observed, outcome in observations if observed and outcome is not None]
        if not outcomes:
            raise ValueError(f"component lacks observed planning outcomes: {component}")
        sequence, rollout = assignment[component]
        services = next(row["services"] for row in component_rows if row["component_id"] == component)
        site = next(row["site"] for row in component_rows if row["component_id"] == component)
        base_rate = mean([float(value) for value in outcomes])
        mean_weekly = max(10, int(round(len(sample) / 52.0)))
        base_rates.append(base_rate)
        weekly_counts.append(mean_weekly)
        rollout_for_power.append(rollout)
        assignment_rows.append(
            {
                "component_id": component,
                "site": site,
                "services": services,
                "sequence": sequence,
                "rollout_week": rollout,
                "full_exposure_week": rollout + wash_in,
                "planning_proxy_rate": base_rate,
                "planning_mean_weekly_referrals": mean_weekly,
                "planning_rows": len(sample),
                "assignment_status": "illustrative_candidate_seed_not_operational_randomization",
                "quantity_status": "simulated_design_input_from_unauthorized_proxy",
            }
        )

    strata = [str(next(row["site"] for row in component_rows if row["component_id"] == component)) for component in ordered_components]
    power_rows: list[dict[str, Any]] = []
    for icc_index, icc in enumerate(float(value) for value in study["icc_grid"]):
        null_statistics = simulate_staggered_statistics(
            base_rates=base_rates,
            mean_weekly_counts=weekly_counts,
            rollout_weeks=rollout_for_power,
            strata=strata,
            weeks=int(study["weeks"]),
            effect=0.0,
            icc=icc,
            simulations=int(study["simulations"]),
            seed=seed + 2900 + icc_index,
            seasonality_amplitude=float(study["seasonality_amplitude"]),
            wash_in_weeks=wash_in,
            serial_correlation=float(study["serial_correlation"]),
            serial_shock_sd=float(study["serial_shock_sd"]),
            policy_week=int(study["scheduling_policy_week_zero_based"]),
            policy_risk_difference=float(study["scheduling_policy_common_risk_difference"]),
            site_time_differential_over_study=float(study["site_time_differential_over_study"]),
        )
        if not null_statistics:
            raise ValueError("prospective null simulation produced no finite statistics")
        lower_critical = quantile(
            [statistic for _, statistic in null_statistics], 0.025
        )
        for effect_index, effect in enumerate(
            float(value) for value in study["effect_grid_risk_difference"]
        ):
            statistics = simulate_staggered_statistics(
                base_rates=base_rates,
                mean_weekly_counts=weekly_counts,
                rollout_weeks=rollout_for_power,
                strata=strata,
                weeks=int(study["weeks"]),
                effect=effect,
                icc=icc,
                simulations=int(study["simulations"]),
                seed=seed + 3000 + icc_index * 100,
                seasonality_amplitude=float(study["seasonality_amplitude"]),
                wash_in_weeks=wash_in,
                serial_correlation=float(study["serial_correlation"]),
                serial_shock_sd=float(study["serial_shock_sd"]),
                policy_week=int(study["scheduling_policy_week_zero_based"]),
                policy_risk_difference=float(study["scheduling_policy_common_risk_difference"]),
                site_time_differential_over_study=float(study["site_time_differential_over_study"]),
            )
            power = calibrated_favorable_tail_power(statistics, lower_critical)
            detections = sum(
                estimate < 0.0 and statistic <= lower_critical
                for estimate, statistic in statistics
            )
            power_lower, power_upper = wilson_interval(detections, len(statistics))
            power_rows.append(
                {
                    "icc": icc,
                    "risk_difference": effect,
                    "simulated_power": power,
                    "simulated_power_lower": power_lower,
                    "simulated_power_upper": power_upper,
                    "monte_carlo_standard_error": math.sqrt(
                        power * (1.0 - power) / len(statistics)
                    ),
                    "successful_simulations": len(statistics),
                    "favorable_detections": detections,
                    "simulations": int(study["simulations"]),
                    "independent_components": len(ordered_components),
                    "weeks": int(study["weeks"]),
                    "empirical_lower_critical_value": lower_critical,
                    "analysis": "two_way_fixed_effects_cluster_robust_statistic_empirically_calibrated_under_null",
                    "quantity_status": "provisional_simulated_planning_only",
                    "limitation": "normal_approximate_binomial_persistent_component_and_AR1_working_correlation_proxy_baseline_fixed_final_analysis_and_inferred_components",
                }
            )
    mde_rows: list[dict[str, Any]] = []
    for icc in [float(value) for value in study["icc_grid"]]:
        candidates = sorted(
            (
                abs(float(row["risk_difference"])),
                float(row["simulated_power"]),
                float(row["simulated_power_lower"]),
            )
            for row in power_rows
            if math.isclose(float(row["icc"]), icc)
            and float(row["risk_difference"]) < 0.0
            and float(row["simulated_power_lower"]) >= float(study["target_power"])
        )
        minimum = candidates[0] if candidates else None
        mde_rows.append(
            {
                "icc": icc,
                "target_power": float(study["target_power"]),
                "minimum_detectable_risk_difference": -minimum[0] if minimum else None,
                "power_at_mde": minimum[1] if minimum else None,
                "power_lower_at_mde": minimum[2] if minimum else None,
                "search_limit_absolute_risk_difference": max(
                    abs(float(value)) for value in study["effect_grid_risk_difference"]
                ),
                "independent_components": len(ordered_components),
                "quantity_status": "provisional_simulated_planning_only",
                "interpretation": "conservative_grid_MDE_requires_MC_lower_bound_at_target_not_continuous_components_not_authoritative_and_sequential_operating_characteristics_not_simulated",
            }
        )
    planning_icc = 0.03
    desired_effect = float(study["primary_clinical_difference"])
    desired_row = next(
        row
        for row in power_rows
        if math.isclose(float(row["icc"]), planning_icc)
        and math.isclose(float(row["risk_difference"]), desired_effect)
    )
    feasible = float(desired_row["simulated_power_lower"]) >= float(study["target_power"])
    feasibility_rows = [
        {
            "decision": "desired_two_point_claim_feasible_with_current_units",
            "value": feasible,
            "planning_icc": planning_icc,
            "desired_risk_difference": desired_effect,
            "simulated_power": float(desired_row["simulated_power"]),
            "simulated_power_lower": float(desired_row["simulated_power_lower"]),
            "simulated_power_upper": float(desired_row["simulated_power_upper"]),
            "target_power": float(study["target_power"]),
            "independent_components": len(ordered_components),
            "conclusion": "feasible_in_simulation" if feasible else "infeasible_with_eight_components_under_planning_scenario",
            "quantity_status": "provisional_simulated_decision_support",
        }
    ]

    design_rows = [
        {
            "element": "randomization_unit",
            "specification": "clinician_cross_cover_interference_component",
            "rationale": "services_connected_by_shared_clinicians_cannot_be_independent",
        },
        {
            "element": "allocation",
            "specification": "four_site_stratified_staggered_sequences_two_components_each;published_assignment_is_illustrative_only;independent_auditable_randomization_required_at_launch",
            "rationale": "no_patient_randomization_and_all_components_eventually_receive_intervention_without_treating_candidate_RNG_as_real_allocation",
        },
        {
            "element": "interference_exposure_mapping",
            "specification": "assigned_on_after_component_rollout_plus_two_week_wash_in; record cross_component_clinician_and_referral_contact_fraction",
            "rationale": "intention_to_treat_assignment_effect_under_measured_spillover",
        },
        {
            "element": "primary_endpoint",
            "specification": "clinically_adjudicated_day_of_surgery_cancellation_with_90_day_referral_followup",
            "rationale": "new_endpoint_instrument_required_current_proxy_not_acceptable",
        },
        {
            "element": "key_secondary_endpoint",
            "specification": "referral_to_readiness_RMST_90_days_with_cancellation_competing_state",
            "rationale": "retains_censoring_and_terminal_state_definition",
        },
        {
            "element": "measurement_endpoint",
            "specification": "census_of_displayed_recommendations_with_two_blinded_reviewers_and_third_clinician_adjudication",
            "rationale": "separates_agreement_from_correctness_and_persuasion",
        },
        {
            "element": "analysis_population",
            "specification": "all_first_eligible_referrals_by_assigned_component_and_referral_time_regardless_of_assessment_or_use",
            "rationale": "cluster_level_intention_to_treat_prevents_survival_to_assessment_bias",
        },
        {
            "element": "primary_analysis",
            "specification": "equal_component_cluster_period_estimator_with_period_and_component_fixed_effects_and_exact_constrained_randomization_inference",
            "rationale": "eight_independent_units_make_patient_level_intervals_invalid",
        },
        {
            "element": "outcome_maturation",
            "specification": f"primary_final_analysis_at_least_{int(study['primary_endpoint_maturation_days'])}_days_after_last_accrued_referral;interims_use_only_mature_endpoints",
            "rationale": "prevents_incomplete_followup_from_becoming_informative_censoring",
        },
        {
            "element": "randomization_inference",
            "specification": "enumerate_all_site_stratified_sequence_assignments_allowed_by_the_locked_constraint_at_final_analysis",
            "rationale": "small_number_of_components_requires_design_based_inference_not_asymptotic_patient_level_intervals",
        },
        {
            "element": "scheduling_policy_cointervention",
            "specification": "freeze_or_document_policy_version_and_effective_time;collect_adherence;include_period_effects_and_prespecified_site_by_time_sensitivity",
            "rationale": "a_concurrent_policy_change_can_otherwise_be_mistaken_for_intervention_effect",
        },
        {
            "element": "prospective_estimand",
            "specification": "component_equal_weight_intention_to_treat_risk_difference_in_90_day_adjudicated_day_of_surgery_cancellation_for_all_first_eligible_referrals",
            "rationale": "defines_population_assignment_time_zero_followup_outcome_and_cluster_level_contrast",
        },
    ]
    sequential_rows: list[dict[str, Any]] = []
    z_final = 1.959963984540054
    harm_z = 2.3263478740408408
    for look_index, information in enumerate(
        float(value) for value in study["interim_information_fractions"]
    ):
        efficacy_magnitude = z_final / math.sqrt(information)
        harm_magnitude = harm_z / math.sqrt(information)
        sequential_rows.append(
            {
                "look": look_index + 1,
                "information_fraction": information,
                "benefit_boundary_z": -efficacy_magnitude,
                "benefit_direction": "negative_risk_difference_fewer_cancellations",
                "cumulative_two_sided_alpha_spent": 2.0 * (1.0 - normal_cdf(efficacy_magnitude)),
                "futility_rule": f"nonbinding_conditional_power_below_{float(study['futility_conditional_power']):.2f}",
                "harm_boundary_z": harm_magnitude,
                "harm_direction": "positive_risk_difference_more_cancellations",
                "cumulative_harm_one_sided_alpha_spent": 1.0 - normal_cdf(harm_magnitude),
                "harm_minimum_risk_difference": float(study["harm_risk_difference"]),
                "benefit_action": "independent_DSMB_may_recommend_stop_only_if_primary_crosses_and_key_secondary_has_no_harm",
                "harm_action": "immediate_pause_and_independent_review",
                "quantity_status": "prospectively_assumed_design_rule",
            }
        )
    governance_rows = [
        {
            "domain": "ethics",
            "requirement": "document_equipoise_standard_care_fallback_and_IRB_determination_of_cluster_and_patient_consent_or_waiver",
            "authority": "IRB_and_clinical_governance",
            "stop_power": "IRB_or_clinical_safety_officer",
        },
        {
            "domain": "safety",
            "requirement": "independent_DSMB_charter_closed_unblinded_reports_and_prespecified_benefit_futility_harm_rules",
            "authority": "independent_DSMB",
            "stop_power": "DSMB_recommends_sponsor_and_clinical_safety_officer_can_pause_immediately",
        },
        {
            "domain": "operations",
            "requirement": "activate_every_service_in_a_component_together_and_log_training_fidelity_overrides_and_spillover",
            "authority": "site_operations_lead",
            "stop_power": "clinical_safety_officer_not_product_owner",
        },
        {
            "domain": "privacy_and_accountability",
            "requirement": "minimum_necessary_data_role_based_access_versioned_audit_logs_and_prohibition_on_unadjudicated_autonomous_action",
            "authority": "privacy_officer_and_clinical_governance",
            "stop_power": "privacy_officer_or_clinical_safety_officer",
        },
    ]
    return {
        "interference_components": component_rows,
        "study_assignment": assignment_rows,
        "study_design": design_rows,
        "power_scenarios": power_rows,
        "minimum_detectable_effect": mde_rows,
        "study_feasibility": feasibility_rows,
        "sequential_rules": sequential_rows,
        "ethics_governance": governance_rows,
    }


def _monitor_catalog(config: dict[str, Any]) -> list[dict[str, Any]]:
    monitoring = config["monitoring"]
    common_versions = "data_version;pipeline_artifact_set;model_version;threshold_version;prompt_version;judge_family_version;policy_version;monitor_config_sha;code_commit"
    rows = [
        ("DQ-01", "data_contracts", "schema_type_null_range_grain_RI", "ingestion_run", "hard_contract", "immediate", "data_steward", "data_steward", "schema drift adds/removes/changes columns;nulls invalid ranges stale inputs undeclared grains;lookup duplication multiplies facts;identifiers malformed or fail to join"),
        ("DQ-02", "data_contracts", "semantic_duplicate_and_event_id_reuse_rate", "ingestion_run", "baseline_plus_zero_incompatible_reuse", "immediate_on_reuse", "data_engineer", "data_steward", "retry duplicates change transport metadata;event_id malformed duplicated or reused;replay can duplicate or alter outputs"),
        ("DQ-03", "data_contracts", "late_revision_and_partition_freshness", "partition_day", "nine_day_revision_profile", "two_breaches_or_missing_partition", "data_engineer", "data_steward", "events arrive beyond literal nine-day rewind;arbitrary historical backfills invalidate later outputs;ingestion partitions continue beyond study window;partition filename disagrees with ingestion timestamp"),
        ("DQ-04", "data_contracts", "quarantine_and_timezone_anomaly_rate", "source_day", "source_specific_baseline", "hard_on_unknown_time_rule_else_two_breaches", "data_engineer", "clinical_safety_officer", "mixed time zones and daylight-saving boundaries;source text contains instructions or sensitive-looking content"),
        ("POP-01", "population_shift", "service_site_ward_mix", "service_week", "season_matched_26_week", "BY_q_below_0.05_two_weeks", "operations_analyst", "clinical_operations_lead", "selected treated services unavailable;clinician-service attributes conflict with event facts;snapshot or dimension keys unmatched"),
        ("POP-02", "population_shift", "age_sex_case_mix_PSI", "site_week", "season_matched_26_week", "PSI_above_0.20_two_weeks", "clinical_analyst", "clinical_operations_lead", "Simpson's paradox and case-mix shift;local workload smaller than evaluation workload"),
        ("MOD-01", "model_behavior", "score_live_PSI_and_threshold_version", "model_site_week", "last_approved_model_season", "PSI_above_0.20_two_weeks_or_threshold_unregistered", "ML_owner", "clinical_safety_officer", "model score drift;threshold selection period unavailable;hidden sealed mutation behavior underspecified"),
        ("MOD-02", "model_behavior", "batch_live_delta_by_null_pattern", "model_service_week", "approved_shadow_baseline", "BY_q_below_0.05_two_weeks", "ML_owner", "clinical_safety_officer", "batch/live null handling differs;feature lineage missing;temporal leakage"),
        ("CAL-01", "calibration", "Brier_log_loss_ECE_calibration_slope", "model_component_month", "locked_external_validation", "minimum_200_cases_and_two_breaches", "ML_owner", "clinical_safety_officer", "recommendation model miscalibration;post-outcome leakage invalidates performance"),
        ("ACT-01", "clinician_action", "recommendation_display_accept_override_abstain", "component_week", "season_matched_26_week", "BY_q_below_0.05_two_weeks", "clinical_operations", "clinical_safety_officer", "interference and clinician behavior;survival-to-assessment selection"),
        ("LAB-01", "calibration", "reviewer_agreement_kappa_AC1_and_unresolved_rate", "label_batch", "adjudicated_validation_set", "lower_bound_below_approved_floor", "label_governance_lead", "clinical_governance", "reviewer disagreement;missing adjudication;nonrandom label sampling"),
        ("LLM-01", "model_behavior", "judge_sensitivity_specificity_by_family", "judge_family_label_batch", "different_family_external_validation", "either_lower_bound_below_0.90", "AI_assurance_lead", "clinical_safety_officer", "same-family judge dependence;LLM judge false approval or false hold"),
        ("OUT-01", "clinical_outcomes", "day_of_surgery_cancellation_rate", "component_week", "hierarchical_seasonal_beta_binomial", "BY_q_below_0.05_same_direction_two_weeks", "clinical_outcomes_analyst", "clinical_safety_officer", "small segment false alert;regression to mean;seasonality;general cancellation endpoint substituted for day-of-surgery"),
        ("OUT-02", "clinical_outcomes", "referral_to_readiness_RMST_and_censoring", "component_month", "season_matched_component", "harm_boundary_or_two_breaches", "clinical_outcomes_analyst", "clinical_safety_officer", "administrative censoring;nonrandom missingness;scheduling-policy cointervention"),
        ("UPL-01", "model_behavior", "uplift_overlap_Qini_AUUC_policy_value", "model_component_quarter", "randomized_or_logged_assignment", "positivity_failure_or_negative_policy_value", "causal_inference_lead", "clinical_governance", "uplift AUC misuse;treatment assignment mechanism absent;targeting positivity violation"),
        ("PROV-01", "data_contracts", "full_version_tuple_and_artifact_hash", "deployment_and_alert", "exact_match", "immediate", "release_manager", "data_steward", "provenance failure;volatile metadata breaks byte identity;partial failure publishes mixed generation"),
        ("AUTH-01", "data_contracts", "authorization_boundary_violation", "request", "zero_tolerance", "immediate", "clinical_safety_officer", "clinical_safety_officer", "authorization violation or untrusted note instruction;unauthorized autonomous use"),
        ("SCALE-01", "data_contracts", "sealed_scale_runtime_memory_and_mutation_qualification", "release_candidate", "sealed_400M_gate", "hard_release_gate", "release_manager", "data_steward", "local workload smaller than evaluation workload;hidden sealed mutation behavior underspecified"),
    ]
    hard_stop_ids = {"DQ-01", "DQ-02", "DQ-03", "DQ-04", "PROV-01", "AUTH-01", "SCALE-01"}
    output: list[dict[str, Any]] = []
    for monitor_id, domain, metric, grain, baseline, alert_rule, owner, stop_authority, defects in rows:
        hard_stop = monitor_id in hard_stop_ids
        outcome_maturity = monitor_id.startswith(("OUT-", "CAL-", "UPL-"))
        output.append({
            "monitor_id": monitor_id,
            "domain": domain,
            "metric": metric,
            "grain": grain,
            "baseline": baseline,
            "alert_rule": alert_rule,
            "alert_class": "hard_stop_budget_exempt" if hard_stop else "statistical_warning",
            "small_segment_control": "not_applicable_hard_stop"
            if hard_stop
            else f"suppress_below_{int(monitoring['minimum_segment_cases'])}_and_shrink_toward_parent",
            "minimum_independent_components": int(monitoring["minimum_independent_components"]),
            "data_maturation": "not_applicable_immediate_contract_check"
            if hard_stop
            else f"wait_{int(config['prospective_study']['primary_endpoint_maturation_days'] if outcome_maturity else monitoring['data_maturation_days'])}_days_before_statistical_alerting",
            "regression_to_mean_control": "not_applicable_hard_stop"
            if hard_stop
            else "two_consecutive_breaches",
            "seasonality_control": "not_applicable_hard_stop"
            if hard_stop
            else "compare_to_matching_calendar_period_or_period_fixed_effect",
            "multiple_testing": "not_applicable_hard_stop"
            if hard_stop
            else "weekly_Benjamini_Yekutieli",
            "alert_budget": None
            if hard_stop
            else int(monitoring["statistical_alert_budget_per_week"]),
            "incident_deduplication": "bundle_same_root_cause_across_segments_and_metrics",
            "escalation_sla": "L1_next_business_day;L2_four_hours;L3_immediate_pause",
            "owner": owner,
            "stop_authority": stop_authority,
            "restart_rule": ";".join(str(value) for value in monitoring["restart_requires"]),
            "required_versions": common_versions,
            "defects_caught": defects,
            "quantity_status": "prospective_monitor_specification",
        })
    return output


def _monitor_replay(
    data_root: Path, config: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    monitoring = config["monitoring"]
    source = _read_csv(data_root / "segment_weekly.csv")
    parsed: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, date]] = set()
    for row in source:
        cases = int(row["cases"])
        cancels = int(row["cancels"])
        week = date.fromisoformat(row["week"])
        if cases < 0 or cancels < 0 or cancels > cases:
            raise ValueError("weekly monitoring source violates count ranges")
        service = row["service_code"].strip().upper()
        key = (service, week)
        if key in seen_keys:
            raise ValueError("segment_weekly violates service_code/week grain")
        seen_keys.add(key)
        parsed.append(
            {
                "site": row["site"].strip().upper(),
                "service_code": service,
                "week": week,
                "cases": cases,
                "cancels": cancels,
                "supplied_rate": float(row["cancel_rate"]),
                "supplied_alert": _boolean(row["alert_fired"]),
            }
        )
    prior_cases = float(monitoring["shrinkage_prior_cases"])
    minimum_cases = int(monitoring["minimum_segment_cases"])
    minimum_baseline_weeks = int(monitoring["minimum_baseline_weeks"])
    service_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    service_weeks: dict[str, int] = defaultdict(int)
    month_totals: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    global_cases = 0
    global_cancels = 0
    by_source_week: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in parsed:
        by_source_week[row["week"]].append(row)
    for week in sorted(by_source_week):
        rows_this_week = by_source_week[week]
        for row in rows_this_week:
            service = str(row["service_code"])
            baseline_available = global_cases > 0 and service_weeks[service] >= minimum_baseline_weeks
            if baseline_available:
                global_rate = global_cancels / global_cases
                service_cases, service_cancels = service_totals[service]
                service_rate = service_cancels / service_cases
                month_cases, month_cancels = month_totals[week.month]
                month_rate = month_cancels / month_cases if month_cases else global_rate
                expected = min(max(service_rate + month_rate - global_rate, 0.001), 0.999)
                posterior = (int(row["cancels"]) + prior_cases * expected) / (
                    int(row["cases"]) + prior_cases
                )
                standard_error = math.sqrt(
                    expected * (1.0 - expected) / (int(row["cases"]) + prior_cases)
                )
                z_value = (posterior - expected) / standard_error if standard_error > 0 else 0.0
                p_value = 2.0 * (1.0 - normal_cdf(abs(z_value)))
            else:
                expected = posterior = z_value = p_value = None
            row.update({
                "reconciled_rate": int(row["cancels"]) / int(row["cases"])
                if int(row["cases"])
                else None,
                "rate_reconciliation_error": abs(
                    float(row["supplied_rate"])
                    - int(row["cancels"]) / int(row["cases"])
                )
                if int(row["cases"])
                else None,
                "seasonal_expected_rate": expected,
                "shrunk_rate": posterior,
                "z_value": z_value,
                "p_value": p_value,
                "baseline_prior_weeks": service_weeks[service],
                "baseline_available": baseline_available,
                "baseline_rule": "strictly_prior_weeks_expanding_service_plus_calendar_month_shrinkage",
                "maturity_status": "historical_assumed_mature_no_as_of_timestamp",
                "minimum_denominator_met": int(row["cases"]) >= minimum_cases,
                "direction": "higher" if z_value is not None and z_value > 0 else "lower" if z_value is not None and z_value < 0 else "none",
            })
        for row in rows_this_week:
            service = str(row["service_code"])
            service_totals[service][0] += int(row["cases"])
            service_totals[service][1] += int(row["cancels"])
            service_weeks[service] += 1
            month_totals[week.month][0] += int(row["cases"])
            month_totals[week.month][1] += int(row["cancels"])
            global_cases += int(row["cases"])
            global_cancels += int(row["cancels"])
    by_week: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in parsed:
        by_week[row["week"]].append(row)
    for rows in by_week.values():
        eligible = [
            row
            for row in rows
            if bool(row["minimum_denominator_met"])
            and bool(row["baseline_available"])
            and row["p_value"] is not None
        ]
        adjusted = by_adjust([float(row["p_value"]) for row in eligible])
        for row, q_value in zip(eligible, adjusted, strict=True):
            row["q_value_by"] = q_value
        for row in rows:
            row.setdefault("q_value_by", None)
    previous: dict[str, dict[str, Any]] = {}
    for row in sorted(parsed, key=lambda item: (item["week"], item["service_code"])):
        candidate = bool(
            row["minimum_denominator_met"]
            and row["q_value_by"] is not None
            and float(row["q_value_by"]) < float(monitoring["by_fdr"])
        )
        prior = previous.get(str(row["service_code"]))
        persistent = bool(
            candidate
            and prior is not None
            and bool(prior.get("candidate"))
            and row["week"] - prior["week"] == timedelta(days=7)
            and row["direction"] == prior["direction"]
        )
        row["candidate"] = candidate
        row["persistent_breach"] = persistent
        previous[str(row["service_code"])] = row
    budget = int(monitoring["statistical_alert_budget_per_week"])
    for rows in by_week.values():
        ranked = sorted(
            [row for row in rows if bool(row["persistent_breach"])],
            key=lambda row: (-abs(float(row["z_value"])), str(row["service_code"])),
        )
        selected = {id(row) for row in ranked[:budget]}
        for row in rows:
            row["reproducible_alert"] = id(row) in selected
            row["budget_suppressed"] = bool(row["persistent_breach"]) and id(row) not in selected
            row["supplied_alert_reproduced"] = bool(row["supplied_alert"]) == bool(
                row["reproducible_alert"]
            )
            row["quantity_status"] = "recomputed_monitor_replay"
            row["limitation"] = "strictly_prior_replay_avoids_future_leakage_but_one_year_cannot_validate_seasonality_and_no_as_of_timestamp_proves_maturity"
    replay_rows = [
        {
            **row,
            "week": row["week"].isoformat(),
        }
        for row in sorted(parsed, key=lambda item: (item["week"], item["service_code"]))
    ]
    summary_rows = [
        {
            "metric": "supplied_alerts",
            "value": sum(bool(row["supplied_alert"]) for row in parsed),
            "quantity_status": "observed",
        },
        {
            "metric": "reproducible_budgeted_alerts",
            "value": sum(bool(row["reproducible_alert"]) for row in parsed),
            "quantity_status": "recomputed_monitor_replay",
        },
        {
            "metric": "supplied_alert_disagreements",
            "value": sum(not bool(row["supplied_alert_reproduced"]) for row in parsed),
            "quantity_status": "recomputed_monitor_replay",
        },
        {
            "metric": "small_segments_suppressed",
            "value": sum(not bool(row["minimum_denominator_met"]) for row in parsed),
            "quantity_status": "recomputed_monitor_replay",
        },
        {
            "metric": "persistent_alerts_budget_suppressed",
            "value": sum(bool(row["budget_suppressed"]) for row in parsed),
            "quantity_status": "recomputed_monitor_replay",
        },
        {
            "metric": "rate_reconciliation_failures",
            "value": sum(
                row["rate_reconciliation_error"] is not None
                and float(row["rate_reconciliation_error"]) > 0.00011
                for row in parsed
            ),
            "quantity_status": "recomputed_monitor_replay",
        },
        {
            "metric": "weekly_human_statistical_alert_budget",
            "value": budget,
            "quantity_status": "prospective_monitor_specification",
        },
        {
            "metric": "weekly_human_triage_hours",
            "value": float(monitoring["human_weekly_statistical_alert_hours"]),
            "quantity_status": "assumed_operating_capacity",
        },
    ]
    return replay_rows, summary_rows


def _category_share_shift(
    before: Sequence[str], after: Sequence[str]
) -> float:
    categories = sorted(set(before) | set(after))
    if not before or not after:
        return math.nan
    return max(
        abs(before.count(category) / len(before) - after.count(category) / len(after))
        for category in categories
    )


def _monitor_snapshot(
    episodes: Sequence[Episode],
    model_records: Sequence[dict[str, Any]],
    pipeline_manifest: dict[str, Any],
    artifact_directory: Path,
    replay_summary: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    switch = date(2026, 5, 1)
    before_model = [row for row in model_records if row["referral_date"] < switch]
    after_model = [row for row in model_records if row["referral_date"] >= switch]
    before_episodes = [episode for episode in episodes if episode.referral_date < switch]
    after_episodes = [episode for episode in episodes if episode.referral_date >= switch]
    artifact_rows = {row["path"]: int(row["rows"]) for row in pipeline_manifest["artifacts"]}
    contracts = json.loads((artifact_directory / "contract_results.json").read_text(encoding="utf-8"))

    def proxy_rate(sample: Sequence[Episode]) -> float:
        values = [
            int(outcome)
            for observed, outcome in (_c1_observation(episode) for episode in sample)
            if observed and outcome is not None
        ]
        return mean([float(value) for value in values])

    def brier(sample: Sequence[dict[str, Any]]) -> float:
        return mean(
            [
                (int(row["outcome"]) - float(row["score"])) ** 2
                for row in sample
            ]
        )

    summary_index = {str(row["metric"]): row["value"] for row in replay_summary}
    return [
        {
            "monitor_id": "DQ-01",
            "metric": "pipeline_contracts_passed",
            "value": sum(bool(row["passed"]) for row in contracts["results"]),
            "reference": len(contracts["results"]),
            "status": "pass" if bool(contracts["all_passed"]) else "hard_stop",
            "quantity_status": "observed",
        },
        {
            "monitor_id": "DQ-03",
            "metric": "late_revision_rows",
            "value": artifact_rows["late_arrival_revisions.parquet"],
            "reference": None,
            "status": "visible_requires_baseline",
            "quantity_status": "observed",
        },
        {
            "monitor_id": "DQ-04",
            "metric": "quarantine_rows",
            "value": artifact_rows["quarantine.parquet"],
            "reference": 0,
            "status": "contained_not_silent",
            "quantity_status": "observed",
        },
        {
            "monitor_id": "POP-01",
            "metric": "maximum_service_share_shift_pre_post",
            "value": _category_share_shift(
                [episode.service_code for episode in before_episodes],
                [episode.service_code for episode in after_episodes],
            ),
            "reference": 0.0,
            "status": "descriptive_no_validated_threshold",
            "quantity_status": "observed",
        },
        {
            "monitor_id": "POP-02",
            "metric": "maximum_sex_share_shift_pre_post",
            "value": _category_share_shift(
                [episode.sex or "UNKNOWN" for episode in before_episodes],
                [episode.sex or "UNKNOWN" for episode in after_episodes],
            ),
            "reference": 0.0,
            "status": "descriptive_no_validated_threshold",
            "quantity_status": "observed",
        },
        {
            "monitor_id": "MOD-01",
            "metric": "score_live_PSI_pre_post",
            "value": population_stability_index(
                [float(row["score"]) for row in before_model],
                [float(row["score"]) for row in after_model],
            ),
            "reference": 0.20,
            "status": "screening_threshold_not_release_gate",
            "quantity_status": "observed",
        },
        {
            "monitor_id": "MOD-02",
            "metric": "feature_null_rate_shift_post_minus_pre",
            "value": mean([float(bool(row["feature_visit_null"])) for row in after_model])
            - mean([float(bool(row["feature_visit_null"])) for row in before_model]),
            "reference": 0.0,
            "status": "descriptive_transport_diagnostic",
            "quantity_status": "observed",
        },
        {
            "monitor_id": "CAL-01",
            "metric": "proxy_Brier_shift_post_minus_pre",
            "value": brier(after_model) - brier(before_model),
            "reference": 0.0,
            "status": "invalidated_outcome_and_leakage_not_clinical_calibration",
            "quantity_status": "invalidated_sensitivity_only",
        },
        {
            "monitor_id": "OUT-01",
            "metric": "proxy_cancellation_shift_post_minus_pre",
            "value": proxy_rate(after_episodes) - proxy_rate(before_episodes),
            "reference": 0.0,
            "status": "descriptive_not_causal_or_day_of_surgery",
            "quantity_status": "sensitivity_only",
        },
        {
            "monitor_id": "OUT-01",
            "metric": "reproducible_budgeted_alerts",
            "value": summary_index["reproducible_budgeted_alerts"],
            "reference": summary_index["supplied_alerts"],
            "status": "supplied_rule_unreproduced",
            "quantity_status": "recomputed_monitor_replay",
        },
    ]


def _monitor_defect_map(catalog: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for monitor in catalog:
        for defect in str(monitor["defects_caught"]).split(";"):
            rows.append(
                {
                    "monitor_id": monitor["monitor_id"],
                    "defect": defect,
                    "detection_mechanism": monitor["metric"],
                    "escalation": monitor["alert_rule"],
                    "stop_authority": monitor["stop_authority"],
                    "coverage_status": "would_detect_or_contain_if_deployed",
                }
            )
    return rows


def _escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _line_svg(
    path: Path,
    title: str,
    subtitle: str,
    series: Sequence[tuple[str, str, Sequence[tuple[float, float]]]],
    x_label: str,
    y_label: str,
    reference_y: float | None = None,
) -> None:
    points = [point for _, _, values in series for point in values]
    if not points:
        raise ValueError(f"figure has no points: {title}")
    x_min = min(point[0] for point in points)
    x_max = max(point[0] for point in points)
    y_min = min(point[1] for point in points)
    y_max = max(point[1] for point in points)
    if reference_y is not None:
        y_min, y_max = min(y_min, reference_y), max(y_max, reference_y)
    if math.isclose(x_min, x_max):
        x_min, x_max = x_min - 0.5, x_max + 0.5
    if math.isclose(y_min, y_max):
        y_min, y_max = y_min - 0.05, y_max + 0.05
    y_padding = max((y_max - y_min) * 0.10, 0.01)
    y_min -= y_padding
    y_max += y_padding
    width, height = 960, 500
    left, right, top, bottom = 105, 900, 95, 420

    def x(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * (right - left)

    def y(value: float) -> float:
        return bottom - (value - y_min) / (y_max - y_min) * (bottom - top)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<metadata>scientific supplement; generated by src/barnabus/scientific.py</metadata>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="28" y="32" font-family="Arial" font-size="20" font-weight="bold" fill="#172033">{_escape(title)}</text>',
        f'<text x="28" y="56" font-family="Arial" font-size="12" fill="#8b1e3f">{_escape(subtitle)}</text>',
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#61708a"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#61708a"/>',
        f'<text x="{(left + right) / 2 - 40}" y="475" font-family="Arial" font-size="12">{_escape(x_label)}</text>',
        f'<text x="18" y="{(top + bottom) / 2}" transform="rotate(-90 18 {(top + bottom) / 2})" font-family="Arial" font-size="12">{_escape(y_label)}</text>',
        f'<text x="{left - 10}" y="{bottom + 20}" font-family="Arial" font-size="10">{x_min:.3f}</text>',
        f'<text x="{right - 35}" y="{bottom + 20}" font-family="Arial" font-size="10">{x_max:.3f}</text>',
        f'<text x="{left - 70}" y="{bottom}" font-family="Arial" font-size="10">{y_min:.3f}</text>',
        f'<text x="{left - 70}" y="{top + 5}" font-family="Arial" font-size="10">{y_max:.3f}</text>',
    ]
    if reference_y is not None:
        lines.append(
            f'<line x1="{left}" y1="{y(reference_y)}" x2="{right}" y2="{y(reference_y)}" stroke="#7d8597" stroke-dasharray="5 5"/>'
        )
    for series_index, (name, color, values) in enumerate(series):
        ordered = sorted(values)
        coordinates = " ".join(f"{x(px):.2f},{y(py):.2f}" for px, py in ordered)
        lines.append(
            f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="3"/>'
        )
        for px, py in ordered:
            lines.append(f'<circle cx="{x(px):.2f}" cy="{y(py):.2f}" r="4" fill="{color}"/>')
        legend_x = left + series_index * 235
        lines.extend(
            [
                f'<line x1="{legend_x}" y1="78" x2="{legend_x + 25}" y2="78" stroke="{color}" stroke-width="3"/>',
                f'<text x="{legend_x + 32}" y="82" font-family="Arial" font-size="11">{_escape(name)}</text>',
            ]
        )
    lines.append("</svg>")
    _write_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))


def _reviewer_svg(rows: Sequence[dict[str, Any]], path: Path) -> None:
    selected = [
        row
        for row in rows
        if row["metric"] in {"raw_agreement", "cohen_kappa", "gwet_ac1"}
    ]
    width, height = 900, 310
    left, right = 240, 850

    def x(value: float) -> float:
        return left + (value + 1.0) / 2.0 * (right - left)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<metadata>service-cluster bootstrap intervals; no adjudicated ground truth</metadata>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="28" y="32" font-family="Arial" font-size="20" font-weight="bold" fill="#172033">Reviewer agreement</text>',
        '<text x="28" y="55" font-family="Arial" font-size="12" fill="#8b1e3f">Supplied reviewer labels; candidate did not adjudicate disagreements.</text>',
        f'<line x1="{x(0.0)}" y1="75" x2="{x(0.0)}" y2="260" stroke="#7d8597" stroke-dasharray="5 5"/>',
        f'<line x1="{left}" y1="270" x2="{right}" y2="270" stroke="#61708a"/>',
        f'<text x="{left - 10}" y="292" font-family="Arial" font-size="10">-1</text>',
        f'<text x="{right - 5}" y="292" font-family="Arial" font-size="10">1</text>',
    ]
    for index, row in enumerate(selected):
        y = 105 + index * 62
        estimate = float(row["estimate"])
        lower = float(row["lower"])
        upper = float(row["upper"])
        lines.extend(
            [
                f'<text x="28" y="{y + 5}" font-family="Arial" font-size="13">{_escape(row["metric"])}</text>',
                f'<line x1="{x(lower)}" y1="{y}" x2="{x(upper)}" y2="{y}" stroke="#16697a" stroke-width="5"/>',
                f'<circle cx="{x(estimate)}" cy="{y}" r="7" fill="#16697a"/>',
            ]
        )
    lines.append("</svg>")
    _write_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))


def _monitor_svg(summary: Sequence[dict[str, Any]], path: Path) -> None:
    indexed = {str(row["metric"]): float(row["value"]) for row in summary}
    values = [
        ("Supplied", indexed["supplied_alerts"], "#8b1e3f"),
        ("Recomputed", indexed["reproducible_budgeted_alerts"], "#16697a"),
        ("Disagree", indexed["supplied_alert_disagreements"], "#c56a1a"),
    ]
    maximum = max(value for _, value, _ in values) or 1.0
    width, height = 820, 360
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<metadata>supplied alert logic is unreproduced; recomputation is candidate-designed</metadata>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="28" y="32" font-family="Arial" font-size="20" font-weight="bold" fill="#172033">Weekly alert replay</text>',
        '<text x="28" y="55" font-family="Arial" font-size="12" fill="#8b1e3f">Counts are not comparable validation targets because the supplied rule is absent.</text>',
    ]
    for index, (label, value, color) in enumerate(values):
        x = 105 + index * 220
        bar_height = value / maximum * 220
        lines.extend(
            [
                f'<rect x="{x}" y="{300 - bar_height}" width="100" height="{bar_height}" fill="{color}"/>',
                f'<text x="{x}" y="325" font-family="Arial" font-size="12">{label}</text>',
                f'<text x="{x + 35}" y="{290 - bar_height}" font-family="Arial" font-size="12">{value:.0f}</text>',
            ]
        )
    lines.append("</svg>")
    _write_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))


def _tag(row: dict[str, Any], field: str, digits: int = 3) -> str:
    value = row.get(field)
    if value is None:
        return "not estimable"
    number_id = row.get(f"{field}_number_id")
    rendered = f"{float(value):.{digits}f}" if isinstance(value, float) else f"{value}"
    return f"{rendered} [{number_id}]" if number_id else rendered


def _scientific_report(
    tables: dict[str, list[dict[str, Any]]], config: dict[str, Any]
) -> str:
    agreement = {row["metric"]: row for row in tables["reviewer_agreement"]}
    judge = {
        (row["label_source"], row["metric"]): row for row in tables["judge_evaluation"]
    }
    model = {
        (row["context"], row["metric"]): row for row in tables["model_operating_point"]
    }
    uplift = {row["metric"]: row for row in tables["uplift_summary"]}
    feasibility = tables["study_feasibility"][0]
    mde = next(
        row
        for row in tables["minimum_detectable_effect"]
        if math.isclose(float(row["icc"]), 0.03)
    )
    monitoring = {row["metric"]: row for row in tables["monitor_summary"]}
    leakage = {row["check"]: row for row in tables["leakage_audit"]}
    top_targeting = uplift["top_30_percent_observed_targeting_association"]
    top_targeting_statement = (
        "The configured top-ranked slice has no targeted/not-targeted overlap, so its association and service-cluster interval are not estimable."
        if top_targeting["estimate"] is None
        else f"The configured top-ranked slice association is {_tag(top_targeting, 'estimate')} with service-cluster interval {_tag(top_targeting, 'lower')} to {_tag(top_targeting, 'upper')}."
    )
    return f"""# Scientific Supplement - v1

Status: scripted, content-addressed supplement to the immutable locked statistical result. Data-derived and simulated quantities cited from the result tables carry a `number_id`; design constants are versioned in the configuration and `design_parameters.csv`. The registry links numeric cells to script, input/config fingerprints, and implementation commit.

## 1. Human labels and LLM judge

The two supplied clinicians agree on {_tag(agreement['raw_agreement'], 'estimate')} of double-reviewed pairs. Chance-corrected agreement is {_tag(agreement['cohen_kappa'], 'estimate')} by Cohen's kappa and {_tag(agreement['gwet_ac1'], 'estimate')} by Gwet's AC1, with service-cluster bootstrap intervals in `reviewer_agreement.csv`. These are agreement measures, not correctness. The supplied `adjudicated` field contains {_tag(agreement['raw_agreement'], 'adjudicated_nonblank', 0)} completed adjudications.

No adjudication was invented. `adjudication_protocol.csv` specifies a valid future process: independent blinded reviewers, an authorized third clinician for disagreements and quality-control agreements, an explicit UNRESOLVED option, and an immutable audit/version lock before judge evaluation. `label_provenance.csv` distinguishes supplied clinician labels from candidate-created agreement/disagreement categories and fit decisions.

Against reviewer 1, the judge sensitivity is {_tag(judge[('reviewer_1', 'sensitivity')], 'estimate')} and specificity is {_tag(judge[('reviewer_1', 'specificity')], 'estimate')}; reviewer-2 and agreement-only results are also retained. All available judge rows are from the same-family condition stated in the frozen plan, while the different-family stratum has zero rows and is not estimable. The judge is **not fit for autonomous use or clinical triage on these data**. The only narrow defensible use is offline, non-patient-facing error sampling with mandatory human review; it cannot create or override a clinical label.

## 2. Recommendation and uplift models

The actually recorded recommendation threshold is {_tag(model[('all_observed_proxy_outcomes', 'brier')], 'threshold_used')}. Operating-point metrics, Brier score ({_tag(model[('all_observed_proxy_outcomes', 'brier')], 'estimate')}), log loss, reliability bins, calibration fit, consequence ratio, decision curves, net benefit, and service-cluster subgroup intervals were all generated. They are **invalidated for clinical-performance claims**, not merely adjusted: feature lineage is absent for {_tag(leakage['feature_lineage_available'], 'affected_rows', 0)} evaluated rows, the target definition and threshold-selection period are absent, score dates are inconsistent with referral time, and the available outcome is not an authorized day-of-surgery cancellation endpoint.

The uplift artifact is an observed targeted-versus-not-targeted association diagnostic ordered by `uplift_score`. It supplies cumulative gain/Qini-shaped diagnostic curves and explicit overlap by score bin. {top_targeting_statement} Those shapes are not called causal uplift evidence. Causal AUUC, Qini, uplift calibration, and policy value remain **not estimable** because treatment delivery/timing/assignment, a defensible propensity contract, an authorized outcome, and the uplift-score scale are missing. The supplied classification AUC is retained only as an unreproduced, invalid uplift metric; it is never used to validate targeting.

## 3. Strongest feasible prospective study

The proposed design is a site-stratified, constrained randomized staggered rollout at the clinician-plus-ward interference-component level. All services in a component switch together; exposure begins after the logged rollout plus a two-week wash-in. Cross-component clinician contact and referral crossover are measured rather than ignored. The population is every first eligible referral classified by assigned component and referral time, regardless of assessment or system use.

The current graph yields {_tag(feasibility, 'independent_components', 0)} provisional components, not patient-level independent units. At the planning ICC of {_tag(feasibility, 'planning_icc')}, simulated fixed-final power for the configured risk difference of {_tag(feasibility, 'desired_risk_difference')} is {_tag(feasibility, 'simulated_power')} with Monte Carlo interval {_tag(feasibility, 'simulated_power_lower')} to {_tag(feasibility, 'simulated_power_upper')}; the conservative grid minimum detectable effect, requiring its Monte Carlo lower bound to meet target power, is {_tag(mde, 'minimum_detectable_risk_difference')}. The plain conclusion is **{feasibility['conclusion']}**. This MDE is provisional: component membership is inferred rather than operationally certified, the baseline uses an unauthorized proxy, and sequential operating characteristics were not simulated. A confirmatory launch requires authoritative component/exposure mapping and endpoint instrumentation.

The protocol uses an authorized day-of-surgery cancellation endpoint, 90-day readiness RMST, full 90-day outcome maturation after accrual, exact constrained-randomization inference at final analysis, and independent DSMB review. Benefit uses O'Brien-Fleming-style spending; futility is nonbinding conditional power below 10%; harm triggers an immediate pause for a risk increase of at least two percentage points with the prespecified one-sided boundary. Hard authorization, provenance, or safety violations bypass statistical spending.

## 4. Monitoring

The catalog covers contracts, freshness/replay, population shift, model behavior, clinician action, calibration, labels/judge, uplift validity, clinical outcomes, interference, provenance, authorization, and sealed-scale qualification. Statistical alerts require mature data, minimum denominators, hierarchical shrinkage, seasonal comparison, weekly BY correction, and two consecutive breaches. Humans receive at most {_tag(monitoring['weekly_human_statistical_alert_budget'], 'value', 0)} new statistical incident bundles per week, budgeted at {_tag(monitoring['weekly_human_triage_hours'], 'value')} hours; hard stops are never suppressed.

The supplied weekly flags are not ground truth because their rule and versions were not supplied. The candidate replay uses only strictly prior weeks for its baseline, never future rows, but one annual cycle cannot validate seasonality and the file has no as-of timestamp proving endpoint maturity. It found {_tag(monitoring['supplied_alerts'], 'value', 0)} supplied flags and {_tag(monitoring['reproducible_budgeted_alerts'], 'value', 0)} candidate-rule alerts, with {_tag(monitoring['supplied_alert_disagreements'], 'value', 0)} disagreements. This comparison characterizes non-reproducibility; it does not claim the candidate rule is correct. `monitor_defect_map.csv` links every monitor to defects it would catch.

## Honest remaining gaps

See `gaps.csv`. The decisive gaps are no adjudicated truth, no different-family judge evaluation, no authorized model target or feature lineage, no causal uplift assignment/propensity contract, no authoritative interference-component activation map, too few provisional independent units for the desired effect under the planning scenario, only one seasonal cycle, no accepted organizational stop/restart charter, and no sealed-scale/container execution. None of this supplement upgrades the three locked claim verdicts.

Method references: [cluster-randomized trial reporting]({config['sources']['cluster_trial_reporting']}), [decision-curve analysis]({config['sources']['decision_curve_method']}), [FDA adaptive-design guidance]({config['sources']['adaptive_design_guidance']}), and [NIH data-safety monitoring guidance]({config['sources']['data_safety_monitoring']}).
"""


def _gaps() -> list[dict[str, Any]]:
    return [
        {"gap_id": "G-01", "gap": "No authorized third-clinician adjudication exists.", "impact": "No single ground-truth label or judge accuracy claim.", "status": "open"},
        {"gap_id": "G-02", "gap": "No different-family judge scores or family identifier are supplied.", "impact": "Same-family dependence cannot be quantified or contrasted.", "status": "open"},
        {"gap_id": "G-03", "gap": "Recommendation target definition, feature lineage, and threshold-selection history are absent.", "impact": "All apparent model performance is invalid for clinical use.", "status": "open"},
        {"gap_id": "G-04", "gap": "The available cancellation outcome is not authorized as day-of-surgery cancellation.", "impact": "Calibration, decision curves, uplift, monitoring, and power use a sensitivity proxy only.", "status": "open"},
        {"gap_id": "G-05", "gap": "Uplift treatment timing, assignment mechanism, propensity contract, and effect-scale definition are absent.", "impact": "Causal Qini, AUUC, calibration, and policy value are not estimable.", "status": "open"},
        {"gap_id": "G-06", "gap": "Interference components are inferred from clinician/ward history rather than certified operationally.", "impact": "Independent-unit count, allocation, power, and MDE are provisional.", "status": "open"},
        {"gap_id": "G-07", "gap": "Only eight provisional independent components are available.", "impact": "Desired small clinical effects can be infeasible despite many patient rows.", "status": "open"},
        {"gap_id": "G-08", "gap": "The supplied alert rule/version is absent and only about one seasonal cycle is available.", "impact": "Historic alerts are unreproduced and false-alert calibration is unproved.", "status": "open"},
        {"gap_id": "G-09", "gap": "The stop/restart authority matrix is candidate-designed and not organizationally accepted.", "impact": "Governance cannot be considered operational until signed.", "status": "open"},
        {"gap_id": "G-10", "gap": "The sealed workload and container image were not executed locally.", "impact": "Scale and environment gates remain open.", "status": "open"},
    ]


def _artifact_inventory(directory: Path, table_rows: dict[str, int]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for path in sorted(item for item in directory.rglob("*") if item.is_file() and item.name != "manifest.json"):
        relative = path.relative_to(directory).as_posix()
        artifacts.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "rows": table_rows.get(relative),
            }
        )
    return artifacts


def run_scientific(paths: ScientificPaths) -> dict[str, Any]:
    paths.validate()
    config = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
    implementation_commit = _implementation_commit(paths.repository_root)
    locked_commit = str(config["locked_results"]["commit"])
    prespec_commit = subprocess.check_output(
        ["git", "rev-parse", f"{config['locked_results']['prespec_tag']}^{{commit}}"],
        cwd=paths.repository_root,
        text=True,
    ).strip()
    if prespec_commit != str(config["locked_results"]["prespec_commit"]):
        raise ValueError("prespec tag target differs from the recorded frozen commit")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", locked_commit, implementation_commit],
        cwd=paths.repository_root,
        check=False,
    ).returncode != 0:
        raise ValueError("locked statistical-results commit is not an ancestor of supplement implementation")
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", prespec_commit, locked_commit],
        cwd=paths.repository_root,
        check=False,
    ).returncode != 0:
        raise ValueError("prespecification commit is not an ancestor of locked results")
    locked_id = str(config["locked_results"]["analysis_id"])
    locked_directory = paths.locked_result_root / "v1" / locked_id
    verify_analysis_results(locked_directory)
    locked_manifest_path = locked_directory / "manifest.json"
    locked_manifest_repository_path = (
        f"results/v1/{locked_id}/manifest.json"
    )
    committed_locked_manifest = subprocess.check_output(
        ["git", "show", f"{locked_commit}:{locked_manifest_repository_path}"],
        cwd=paths.repository_root,
    )
    if hashlib.sha256(committed_locked_manifest).hexdigest() != sha256_file(locked_manifest_path):
        raise ValueError("locked result manifest differs from the blob at the locked commit")
    identity_file = paths.repository_root / "config" / "scientific-implementation-commit.txt"
    if identity_file.is_file() and not os.environ.get("BARNABUS_SCIENTIFIC_IMPLEMENTATION_COMMIT"):
        for code_path in (SCRIPT_PATH, STATS_PATH, "config/scientific-supplement-v1.yaml"):
            if subprocess.run(
                ["git", "diff", "--quiet", implementation_commit, "--", code_path],
                cwd=paths.repository_root,
                check=False,
            ).returncode != 0:
                raise ValueError(f"working file differs from scientific implementation commit: {code_path}")

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

    input_names = [
        "labels_pairs.csv",
        "labels_reviewers.csv",
        "llm_judge_scores.csv",
        "model_scores.csv",
        "uplift_targeting.csv",
        "segment_weekly.csv",
        "patients.csv",
    ]
    inputs = [
        {
            "path": name,
            "sha256": sha256_file(paths.data_root / name),
            "size_bytes": (paths.data_root / name).stat().st_size,
            "role": "direct_scientific_input",
        }
        for name in input_names
    ]
    inputs.extend(
        [
            {
                "path": "pipeline/artifact_manifest.json",
                "sha256": sha256_file(pipeline_manifest_path),
                "size_bytes": pipeline_manifest_path.stat().st_size,
                "role": "canonical_event_pipeline_input",
            },
            {
                "path": f"locked_results/v1/{locked_id}/manifest.json",
                "sha256": sha256_file(locked_manifest_path),
                "size_bytes": locked_manifest_path.stat().st_size,
                "role": "immutable_parent_result",
            },
        ]
    )
    input_fingerprint = _sha256_payload(inputs)
    config_fingerprint = _sha256_payload(
        {
            "config": config,
            "config_sha256": sha256_file(paths.config_path),
            "frozen_plan_sha256": sha256_file(paths.repository_root / "config" / "analysis-plan-v1.yaml"),
        }
    )
    result_id = _sha256_payload(
        {
            "schema_version": SCHEMA_VERSION,
            "input_fingerprint": input_fingerprint,
            "config_fingerprint": config_fingerprint,
            "implementation_commit": implementation_commit,
            "locked_result_id": locked_id,
        }
    )
    version_root = paths.result_root / str(config["result_version"])
    final_directory = version_root / result_id
    if final_directory.exists():
        return verify_scientific_results(final_directory)
    staging = version_root / ".staging" / uuid.uuid4().hex
    staging.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect()
    try:
        episodes = _load_episodes(
            connection,
            paths.data_root,
            artifact_directory,
            date(2026, 7, 31),
        )
        component_mapping, component_rows = _prospective_components(
            connection, artifact_directory, episodes, date(2026, 5, 1)
        )
        draws = int(config["inference"]["cluster_bootstrap_draws"])
        seed = int(config["inference"]["seed"])
        label_tables = _label_tables(paths.data_root, episodes, draws, seed, config)
        model_tables = _model_tables(paths.data_root, episodes, draws, seed, config)
        model_records = model_tables.pop("_model_records")
        uplift_tables = _uplift_tables(paths.data_root, episodes, draws, seed, config)
        prospective_tables = _prospective_tables(
            episodes, component_mapping, component_rows, config
        )
        catalog = _monitor_catalog(config)
        replay, replay_summary = _monitor_replay(paths.data_root, config)
        monitor_snapshot = _monitor_snapshot(
            episodes, model_records, pipeline_manifest, artifact_directory, replay_summary
        )
        tables: dict[str, list[dict[str, Any]]] = {
            **label_tables,
            **model_tables,
            **uplift_tables,
            **prospective_tables,
            "monitor_catalog": catalog,
            "monitor_replay": replay,
            "monitor_summary": replay_summary,
            "monitor_snapshot": monitor_snapshot,
            "monitor_defect_map": _monitor_defect_map(catalog),
            "gaps": _gaps(),
            "design_parameters": _design_parameters(config),
        }
        tables["evaluation_comparison_ledger"] = _evaluation_ledger(tables)
        tables["table_contracts"] = _assert_declared_grains(tables)
        tables["table_contracts"].append(
            {
                "table": "table_contracts",
                "declared_grain": "table",
                "rows": len(tables["table_contracts"]) + 1,
                "duplicate_grains": 0,
                "null_grain_keys": 0,
                "passed": True,
                "quantity_status": "recomputed_contract",
            }
        )
        if len({row["table"] for row in tables["table_contracts"]}) != len(
            tables["table_contracts"]
        ):
            raise ValueError("table_contracts violates table grain")
        registry = NumberRegistry(input_fingerprint, config_fingerprint, implementation_commit)
        for name, rows in tables.items():
            _register_table_numbers(registry, name, rows, script=SCRIPT_PATH)
            _write_csv(staging / "tables" / f"{name}.csv", rows)

        figures = staging / "figures"
        figures.mkdir(parents=True, exist_ok=True)
        _reviewer_svg(tables["reviewer_agreement"], figures / "reviewer_agreement.svg")
        calibration_all = [
            row for row in tables["model_calibration"] if row["context"] == "all_observed_proxy_outcomes"
        ]
        _line_svg(
            figures / "recommendation_reliability.svg",
            "Recommendation-model reliability",
            "Invalidated for clinical performance: proxy target and feature lineage are not authorized.",
            [("Observed", "#16697a", [(float(row["mean_predicted"]), float(row["observed_rate"])) for row in calibration_all]),
             ("Ideal", "#7d8597", [(0.0, 0.0), (1.0, 1.0)])],
            "Mean predicted risk",
            "Observed proxy risk",
        )
        decision_all = [
            row for row in tables["decision_curve"] if row["context"] == "all_observed_proxy_outcomes"
        ]
        _line_svg(
            figures / "recommendation_decision_curve.svg",
            "Recommendation-model decision curve",
            "Sensitivity-only net benefit; not valid for deployment decisions.",
            [
                (strategy, color, [(float(row["threshold"]), float(row["net_benefit"])) for row in decision_all if row["strategy"] == strategy])
                for strategy, color in (("model", "#16697a"), ("treat_all", "#c56a1a"), ("treat_none", "#7d8597"))
            ],
            "Threshold probability",
            "Net benefit",
            reference_y=0.0,
        )
        uplift_points = [
            (float(row["population_fraction"]), float(row["diagnostic_qini_gain"]))
            for row in tables["uplift_curve"]
            if row["diagnostic_qini_gain"] is not None
        ]
        _line_svg(
            figures / "uplift_qini_diagnostic.svg",
            "Observed targeting-association diagnostic",
            "Not a causal Qini curve: treatment delivery and assignment are not identified.",
            [("Qini-shaped association", "#8b1e3f", uplift_points)],
            "Population fraction ranked by uplift score",
            "Observed association above random targeting",
            reference_y=0.0,
        )
        power_series = []
        for icc, color in ((0.01, "#16697a"), (0.03, "#c56a1a"), (0.05, "#8b1e3f")):
            power_series.append(
                (
                    f"ICC {icc:.2f}",
                    color,
                    [
                        (abs(float(row["risk_difference"])), float(row["simulated_power"]))
                        for row in tables["power_scenarios"]
                        if math.isclose(float(row["icc"]), icc)
                    ],
                )
            )
        _line_svg(
            figures / "prospective_power.svg",
            "Provisional staggered-rollout power",
            "Eight inferred components; proxy baseline and fixed-final simulation only.",
            power_series,
            "Absolute cancellation risk reduction",
            "Simulated favorable-tail power",
            reference_y=float(config["prospective_study"]["target_power"]),
        )
        _monitor_svg(tables["monitor_summary"], figures / "monitor_alert_replay.svg")

        report = _scientific_report(tables, config)
        _write_bytes(staging / "scientific-supplement-v1.md", report.encode("utf-8"))
        results_payload = {
            "schema_version": SCHEMA_VERSION,
            "result_id": result_id,
            "locked_result_id": locked_id,
            "locked_result_commit": locked_commit,
            "implementation_commit": implementation_commit,
            "judge_use": tables["judge_use_decision"],
            "recommendation_model_validity": config["recommendation_model"]["performance_disposition"],
            "uplift_causal_identification": config["uplift"]["causal_identification"],
            "study_feasibility": tables["study_feasibility"],
            "gaps": tables["gaps"],
        }
        _write_json(staging / "results.json", results_payload)
        _write_csv(staging / "scientific_number_registry.csv", registry.rows)
        table_rows = {
            f"tables/{name}.csv": len(rows) for name, rows in tables.items()
        }
        table_rows["scientific_number_registry.csv"] = len(registry.rows)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "result_version": config["result_version"],
            "result_id": result_id,
            "implementation_commit": implementation_commit,
            "locked_results": {
                "commit": locked_commit,
                "analysis_id": locked_id,
                "manifest_sha256": sha256_file(locked_manifest_path),
                "immutable": True,
            },
            "prespec_tag": config["locked_results"]["prespec_tag"],
            "input_fingerprint": input_fingerprint,
            "config_fingerprint": config_fingerprint,
            "inputs": inputs,
            "scripts": [
                {"path": code_path, "sha256": sha256_file(paths.repository_root / code_path)}
                for code_path in (
                    SCRIPT_PATH,
                    STATS_PATH,
                    "src/barnabus/analysis.py",
                    "src/barnabus/stats.py",
                    "src/barnabus/analyst_reproduction.py",
                    "src/barnabus/pipeline.py",
                    "src/barnabus/config.py",
                )
            ],
            "config": {"path": "config/scientific-supplement-v1.yaml", "sha256": sha256_file(paths.config_path)},
            "pipeline_artifact_set_id": pipeline_manifest["artifact_set_id"],
            "pipeline_data_version": pipeline_manifest["data_version"],
            "number_registry": {
                "path": "scientific_number_registry.csv",
                "rows": len(registry.rows),
                "rule": "every_numeric_cell_in_reported_tables_has_a_number_id",
            },
            "quantity_label_rule": "observed_assumed_simulated_candidate_created_invalidated_unreproduced_and_not_estimable_are_explicit",
            "untrusted_inputs_not_used": [
                "clinical_notes.csv",
                "questions.csv",
                "authorization_model.json",
                "analyst_query.sql",
            ],
            "artifacts": _artifact_inventory(staging, table_rows),
        }
        _write_json(staging / "manifest.json", manifest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        connection.close()
    final_directory.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, final_directory)
    _write_bytes(version_root / "CURRENT", (result_id + "\n").encode("ascii"))
    return verify_scientific_results(final_directory)


def verify_scientific_results(directory: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        path = directory / artifact["path"]
        if not path.is_file() or sha256_file(path) != artifact["sha256"]:
            raise ValueError(f"scientific artifact verification failed: {artifact['path']}")
    with (directory / manifest["number_registry"]["path"]).open(
        newline="", encoding="utf-8"
    ) as handle:
        registry_rows = sum(1 for _ in csv.DictReader(handle))
    if registry_rows != int(manifest["number_registry"]["rows"]):
        raise ValueError("scientific number registry row count mismatch")
    return {
        "result_id": manifest["result_id"],
        "directory": str(directory),
        "implementation_commit": manifest["implementation_commit"],
        "verified_artifacts": len(manifest["artifacts"]),
        "registered_numbers": registry_rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Barnabus scientific supplement pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--data-root", type=Path, required=True)
    run.add_argument("--work-root", type=Path, default=Path("work"))
    run.add_argument("--pipeline-output-root", type=Path, default=Path("outputs"))
    run.add_argument("--locked-result-root", type=Path, default=Path("results"))
    run.add_argument("--result-root", type=Path, default=Path("results"))
    run.add_argument(
        "--config", type=Path, default=Path("config/scientific-supplement-v1.yaml")
    )
    verify = subparsers.add_parser("verify")
    verify.add_argument("--result-directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            print(json.dumps(verify_scientific_results(args.result_directory), sort_keys=True))
            return 0
        repository_root = Path.cwd().resolve()
        result = run_scientific(
            ScientificPaths(
                repository_root=repository_root,
                data_root=args.data_root.resolve(),
                work_root=args.work_root.resolve(),
                pipeline_output_root=args.pipeline_output_root.resolve(),
                locked_result_root=args.locked_result_root.resolve(),
                result_root=args.result_root.resolve(),
                config_path=args.config.resolve(),
            )
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, duckdb.Error) as exc:
        print(json.dumps({"event": "scientific_supplement_failed", "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
