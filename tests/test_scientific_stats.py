from __future__ import annotations

import math

from barnabus.scientific_stats import (
    agreement_metrics,
    by_adjust,
    net_benefit,
    population_stability_index,
    rank_auc,
    service_cluster_bootstrap,
    service_cluster_classification_intervals,
    service_cluster_reliability_bins,
    simulate_staggered_statistics,
    two_way_fixed_effect_t,
    wilson_interval,
)


def test_reviewer_agreement_reports_distinct_chance_corrections() -> None:
    left = [1] * 8 + [0] * 2
    right = [1] * 7 + [0, 1, 0]
    metrics = agreement_metrics(left, right)
    assert math.isclose(metrics["raw_agreement"], 0.8)
    assert -1.0 <= metrics["cohen_kappa"] <= 1.0
    assert -1.0 <= metrics["gwet_ac1"] <= 1.0
    assert metrics["cohen_kappa"] != metrics["gwet_ac1"]


def test_wilson_interval_contains_observed_proportion() -> None:
    lower, upper = wilson_interval(67, 100)
    assert lower < 0.67 < upper
    assert 0.0 <= lower < upper <= 1.0


def test_service_bootstrap_is_deterministic_and_resamples_services() -> None:
    records = [
        {"service_code": "A", "outcome": 1},
        {"service_code": "A", "outcome": 0},
        {"service_code": "B", "outcome": 0},
        {"service_code": "C", "outcome": 1},
    ]

    def metric(rows: object) -> float:
        sample = list(rows)  # type: ignore[arg-type]
        return sum(int(row["outcome"]) for row in sample) / len(sample)

    first = service_cluster_bootstrap(records, metric, draws=199, seed=42)
    second = service_cluster_bootstrap(records, metric, draws=199, seed=42)
    assert first == second
    assert first[3] == 3
    assert first[1] <= first[0] <= first[2]


def test_decision_curve_net_benefit_and_by_are_bounded() -> None:
    labels = [1, 0, 1, 0]
    scores = [0.9, 0.8, 0.7, 0.1]
    assert math.isclose(net_benefit(labels, scores, 0.5), 0.25)
    adjusted = by_adjust([0.01, 0.04, 0.03])
    assert len(adjusted) == 3
    assert all(0.0 <= value <= 1.0 for value in adjusted)


def test_rank_auc_handles_ties_exactly() -> None:
    assert math.isclose(rank_auc([1, 0, 1, 0], [0.5, 0.5, 0.9, 0.1]), 0.875)


def test_joint_classification_bootstrap_is_deterministic() -> None:
    records = [
        {"service_code": "A", "label": 1, "score": 0.9},
        {"service_code": "A", "label": 0, "score": 0.6},
        {"service_code": "B", "label": 1, "score": 0.7},
        {"service_code": "B", "label": 0, "score": 0.2},
        {"service_code": "C", "label": 0, "score": 0.1},
    ]
    arguments = dict(
        records=records,
        label_getter=lambda row: int(row["label"]),
        score_getter=lambda row: float(row["score"]),
        threshold=0.5,
        draws=199,
        seed=7,
    )
    first = service_cluster_classification_intervals(**arguments)
    second = service_cluster_classification_intervals(**arguments)
    assert first == second
    assert set(first) == {
        "sensitivity", "specificity", "ppv", "npv", "balanced_accuracy",
        "auc", "brier", "log_loss", "net_benefit",
    }
    assert first["auc"][3] == 3


def test_reliability_intervals_resample_services_not_patients() -> None:
    records = [
        {"case_id": f"{service}-{index}", "service_code": service, "score": score, "outcome": outcome}
        for service, score, outcome in (
            ("A", 0.1, 0), ("A", 0.2, 1), ("B", 0.7, 0),
            ("B", 0.8, 1), ("C", 0.9, 1), ("C", 0.6, 1),
        )
        for index in range(2)
    ]
    rows = service_cluster_reliability_bins(records, bins=2, draws=199, seed=11)
    assert len(rows) == 2
    assert all(row["service_clusters"] == 3 for row in rows)
    assert all(row["interval_method"] == "service_cluster_percentile_bootstrap" for row in rows)


def test_population_stability_is_zero_for_identical_samples() -> None:
    values = [float(index) for index in range(100)]
    assert math.isclose(population_stability_index(values, values), 0.0, abs_tol=1e-12)


def test_two_way_fixed_effect_recovers_known_cluster_period_effect() -> None:
    treatment = [
        [0.0, 0.0, 1.0, 1.0, 1.0],
        [0.0, 0.0, 0.0, 1.0, 1.0],
        [0.0, 0.0, 0.0, 0.0, 1.0],
        [0.0, 1.0, 1.0, 1.0, 1.0],
    ]
    outcomes = [
        [0.20 + 0.01 * cluster + 0.005 * week - 0.04 * treatment[cluster][week]
         for week in range(5)]
        for cluster in range(4)
    ]
    estimate, _ = two_way_fixed_effect_t(outcomes, treatment)
    assert math.isclose(estimate, -0.04, abs_tol=1e-12)


def test_two_way_fixed_effect_excludes_component_specific_wash_in_cells() -> None:
    treatment = [
        [0.0, math.nan, 1.0, 1.0],
        [0.0, 0.0, math.nan, 1.0],
        [0.0, 0.0, 0.0, 1.0],
        [0.0, math.nan, 1.0, 1.0],
    ]
    outcomes = [
        [
            math.nan
            if not math.isfinite(treatment[cluster][week])
            else 0.20 + 0.01 * cluster + 0.005 * week - 0.03 * treatment[cluster][week]
            for week in range(4)
        ]
        for cluster in range(4)
    ]
    estimate, statistic = two_way_fixed_effect_t(outcomes, treatment)
    assert math.isclose(estimate, -0.03, abs_tol=1e-10)
    assert math.isfinite(statistic) or math.isnan(statistic)


def test_staggered_simulation_is_deterministic_and_stratified() -> None:
    arguments = dict(
        base_rates=[0.10, 0.11, 0.12, 0.13],
        mean_weekly_counts=[30, 30, 30, 30],
        rollout_weeks=[4, 8, 4, 8],
        strata=["A", "A", "B", "B"],
        weeks=12,
        effect=-0.03,
        icc=0.03,
        simulations=20,
        seed=123,
        seasonality_amplitude=0.01,
    )
    assert simulate_staggered_statistics(**arguments) == simulate_staggered_statistics(**arguments)
