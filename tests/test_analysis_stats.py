from __future__ import annotations

import math

from barnabus.stats import (
    benjamini_yekutieli_adjust,
    binary_agreement,
    holm_adjust,
    km_rmst,
    project_simplex,
    synthetic_effect,
    webb_service_inference,
)


def test_holm_and_by_retain_every_registered_comparison() -> None:
    values = [0.01, 0.04, None, 0.03]
    holm = holm_adjust(values)
    by = benjamini_yekutieli_adjust(values)
    assert len(holm) == len(by) == len(values)
    assert holm[2] is None and by[2] is None
    assert holm == [0.03, 0.06, None, 0.06]
    assert all(value is None or 0 <= value <= 1 for value in by)


def test_webb_inference_is_deterministic_at_service_grain() -> None:
    first = webb_service_inference([-0.03, -0.02, 0.01, -0.01, 0.0, -0.04], 999, 42)
    second = webb_service_inference([-0.03, -0.02, 0.01, -0.01, 0.0, -0.04], 999, 42)
    assert first == second
    assert first["service_clusters"] == 6


def test_km_rmst_keeps_nonready_cases_in_risk_set() -> None:
    # One readiness event at day 10 and one case known not-ready through day 90.
    assert math.isclose(km_rmst([(10.0, True), (90.0, False)], 90.0), 50.0)


def test_synthetic_control_uses_nonnegative_unit_sum_weights() -> None:
    effect, weights, rmse = synthetic_effect(
        [1.0, 2.0, 3.0],
        [2.0, 3.0],
        [[1.0, 2.0, 3.0], [3.0, 2.0, 1.0]],
        [[1.0, 2.0], [3.0, 2.0]],
        ridge=1e-6,
        iterations=2000,
    )
    assert all(weight >= 0 for weight in weights)
    assert math.isclose(sum(weights), 1.0)
    assert math.isfinite(effect) and math.isfinite(rmse)
    assert project_simplex([-2.0, 3.0]) == [0.0, 1.0]


def test_binary_agreement_separates_raw_from_chance_excess() -> None:
    metrics = binary_agreement(0.9, 0.9, 0.85)
    assert math.isclose(metrics["expected_agreement"], 0.82)
    assert math.isclose(metrics["chance_excess"], 0.03)
    assert metrics["gwet_ac1"] != metrics["cohen_kappa"]
