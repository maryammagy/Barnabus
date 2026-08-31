"""Deterministic small-cluster and evaluation helpers.

The functions intentionally operate on service-level sufficient statistics.
They never construct patient-level confidence intervals for a service-level
intervention.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean requires at least one value")
    return math.fsum(values) / len(values)


def sample_sd(values: Sequence[float]) -> float:
    if len(values) < 2:
        return math.nan
    center = mean(values)
    return math.sqrt(math.fsum((value - center) ** 2 for value in values) / (len(values) - 1))


def quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be in [0, 1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def holm_adjust(p_values: Sequence[float | None]) -> list[float | None]:
    valid = [(index, value) for index, value in enumerate(p_values) if value is not None]
    ordered = sorted(valid, key=lambda item: (item[1], item[0]))
    adjusted: list[float | None] = [None] * len(p_values)
    running = 0.0
    count = len(ordered)
    for rank, (index, value) in enumerate(ordered):
        running = max(running, min(1.0, (count - rank) * value))
        adjusted[index] = running
    return adjusted


def benjamini_yekutieli_adjust(p_values: Sequence[float | None]) -> list[float | None]:
    valid = [(index, value) for index, value in enumerate(p_values) if value is not None]
    ordered = sorted(valid, key=lambda item: (item[1], item[0]))
    count = len(ordered)
    adjusted: list[float | None] = [None] * len(p_values)
    if count == 0:
        return adjusted
    harmonic = math.fsum(1.0 / rank for rank in range(1, count + 1))
    running = 1.0
    for reverse_rank in range(count - 1, -1, -1):
        index, value = ordered[reverse_rank]
        rank = reverse_rank + 1
        running = min(running, value * count * harmonic / rank, 1.0)
        adjusted[index] = running
    return adjusted


WEBB_WEIGHTS = (
    -math.sqrt(1.5),
    -1.0,
    -math.sqrt(0.5),
    math.sqrt(0.5),
    1.0,
    math.sqrt(1.5),
)


def webb_service_inference(
    effects: Sequence[float], draws: int, seed: int, alpha: float = 0.05
) -> dict[str, float | int]:
    """Wild service-cluster bounds and p-value around an equal-service mean."""

    if len(effects) < 2:
        return {
            "estimate": mean(effects),
            "lower": math.nan,
            "upper": math.nan,
            "p_value": math.nan,
            "service_clusters": len(effects),
            "df": max(0, len(effects) - 1),
        }
    estimate = mean(effects)
    residuals = [value - estimate for value in effects]
    generator = random.Random(seed)
    null_means: list[float] = []
    for _ in range(draws):
        null_means.append(
            math.fsum(
                residual * WEBB_WEIGHTS[generator.randrange(len(WEBB_WEIGHTS))]
                for residual in residuals
            )
            / len(residuals)
        )
    lower_error = quantile(null_means, alpha / 2)
    upper_error = quantile(null_means, 1 - alpha / 2)
    p_value = (1 + sum(abs(value) >= abs(estimate) for value in null_means)) / (
        draws + 1
    )
    return {
        "estimate": estimate,
        "lower": estimate - upper_error,
        "upper": estimate - lower_error,
        "p_value": p_value,
        "service_clusters": len(effects),
        "df": len(effects) - 1,
    }


def project_simplex(values: Sequence[float]) -> list[float]:
    """Euclidean projection onto non-negative weights summing to one."""

    if not values:
        raise ValueError("simplex projection requires values")
    ordered = sorted(values, reverse=True)
    cumulative = 0.0
    rho = 0
    for index, value in enumerate(ordered, start=1):
        cumulative += value
        threshold = (cumulative - 1.0) / index
        if value - threshold > 0:
            rho = index
    theta = (math.fsum(ordered[:rho]) - 1.0) / rho
    projected = [max(value - theta, 0.0) for value in values]
    total = math.fsum(projected)
    return [value / total for value in projected]


def synthetic_weights(
    target: Sequence[float],
    donors: Sequence[Sequence[float]],
    ridge: float,
    iterations: int,
) -> list[float]:
    if not donors or not target:
        raise ValueError("synthetic control requires target and donors")
    if any(len(donor) != len(target) for donor in donors):
        raise ValueError("donor and target histories must align")
    count = len(donors)
    weights = [1.0 / count] * count
    # Conservative Lipschitz bound for projected gradient descent.
    column_energy = [math.fsum(value * value for value in donor) for donor in donors]
    step = 1.0 / max(1.0, 2.0 * math.fsum(column_energy) + 2.0 * ridge)
    for _ in range(iterations):
        fitted = [
            math.fsum(weights[j] * donors[j][t] for j in range(count))
            for t in range(len(target))
        ]
        residual = [fitted[t] - target[t] for t in range(len(target))]
        gradient = [
            2.0 * math.fsum(donors[j][t] * residual[t] for t in range(len(target)))
            + 2.0 * ridge * weights[j]
            for j in range(count)
        ]
        updated = project_simplex([weights[j] - step * gradient[j] for j in range(count)])
        if max(abs(updated[j] - weights[j]) for j in range(count)) < 1e-12:
            weights = updated
            break
        weights = updated
    return weights


def synthetic_effect(
    target_pre: Sequence[float],
    target_post: Sequence[float],
    donors_pre: Sequence[Sequence[float]],
    donors_post: Sequence[Sequence[float]],
    ridge: float,
    iterations: int,
) -> tuple[float, list[float], float]:
    weights = synthetic_weights(target_pre, donors_pre, ridge, iterations)
    pre_residuals = [
        target_pre[t]
        - math.fsum(weights[j] * donors_pre[j][t] for j in range(len(weights)))
        for t in range(len(target_pre))
    ]
    post_residuals = [
        target_post[t]
        - math.fsum(weights[j] * donors_post[j][t] for j in range(len(weights)))
        for t in range(len(target_post))
    ]
    pre_bias = mean(pre_residuals)
    effect = mean(post_residuals) - pre_bias
    rmse = math.sqrt(mean([(value - pre_bias) ** 2 for value in pre_residuals]))
    return effect, weights, rmse


def km_rmst(records: Sequence[tuple[float, bool]], horizon: float = 90.0) -> float:
    """Kaplan-Meier restricted mean time not ready through ``horizon``."""

    if not records:
        return math.nan
    normalized = [(min(max(time, 0.0), horizon), bool(event)) for time, event in records]
    event_times = sorted({time for time, event in normalized if event and time <= horizon})
    survival = 1.0
    area = 0.0
    prior = 0.0
    for time in event_times:
        area += survival * (time - prior)
        at_risk = sum(observed_time >= time for observed_time, _ in normalized)
        events = sum(observed_time == time and event for observed_time, event in normalized)
        if at_risk:
            survival *= 1.0 - events / at_risk
        prior = time
    area += survival * (horizon - prior)
    return area


def odds_shift(probability: float, multiplier: float) -> float:
    probability = min(max(probability, 1e-12), 1 - 1e-12)
    odds = probability / (1.0 - probability)
    shifted = odds * multiplier
    return shifted / (1.0 + shifted)


def binary_agreement(
    recommendation_positive: float,
    action_positive: float,
    agreement: float,
) -> dict[str, float]:
    expected = (
        recommendation_positive * action_positive
        + (1.0 - recommendation_positive) * (1.0 - action_positive)
    )
    chance_excess = agreement - expected
    kappa = (agreement - expected) / (1.0 - expected) if expected < 1.0 else math.nan
    average_positive = (recommendation_positive + action_positive) / 2.0
    ac1_expected = 2.0 * average_positive * (1.0 - average_positive)
    ac1 = (
        (agreement - ac1_expected) / (1.0 - ac1_expected)
        if ac1_expected < 1.0
        else math.nan
    )
    return {
        "raw_agreement": agreement,
        "expected_agreement": expected,
        "chance_excess": chance_excess,
        "cohen_kappa": kappa,
        "gwet_ac1": ac1,
    }


def auc(labels: Sequence[int], scores: Sequence[float]) -> float:
    positives = [score for label, score in zip(labels, scores, strict=True) if label == 1]
    negatives = [score for label, score in zip(labels, scores, strict=True) if label == 0]
    if not positives or not negatives:
        return math.nan
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            wins += 1.0 if positive > negative else 0.5 if positive == negative else 0.0
    return wins / (len(positives) * len(negatives))


def binary_metrics(
    labels: Sequence[int], scores: Sequence[float], thresholds: Sequence[float]
) -> dict[str, float]:
    if not labels or len(labels) != len(scores) or len(labels) != len(thresholds):
        raise ValueError("labels, scores, and thresholds must align and be non-empty")
    predictions = [int(score >= threshold) for score, threshold in zip(scores, thresholds, strict=True)]
    tp = sum(label == 1 and prediction == 1 for label, prediction in zip(labels, predictions, strict=True))
    tn = sum(label == 0 and prediction == 0 for label, prediction in zip(labels, predictions, strict=True))
    fp = sum(label == 0 and prediction == 1 for label, prediction in zip(labels, predictions, strict=True))
    fn = sum(label == 1 and prediction == 0 for label, prediction in zip(labels, predictions, strict=True))

    def safe(numerator: float, denominator: float) -> float:
        return numerator / denominator if denominator else math.nan

    clipped = [min(max(score, 1e-12), 1 - 1e-12) for score in scores]
    return {
        "n": float(len(labels)),
        "prevalence": mean([float(value) for value in labels]),
        "auc": auc(labels, scores),
        "sensitivity": safe(tp, tp + fn),
        "specificity": safe(tn, tn + fp),
        "ppv": safe(tp, tp + fp),
        "npv": safe(tn, tn + fn),
        "balanced_accuracy": mean([safe(tp, tp + fn), safe(tn, tn + fp)]),
        "brier": mean([(label - score) ** 2 for label, score in zip(labels, scores, strict=True)]),
        "log_loss": -mean(
            [
                label * math.log(score) + (1 - label) * math.log(1 - score)
                for label, score in zip(labels, clipped, strict=True)
            ]
        ),
    }


def logistic_calibration(labels: Sequence[int], scores: Sequence[float]) -> tuple[float, float]:
    """Logistic calibration intercept and slope by deterministic Newton steps."""

    if not labels or len(labels) != len(scores):
        return math.nan, math.nan
    logits = [
        math.log(min(max(score, 1e-9), 1 - 1e-9) / (1 - min(max(score, 1e-9), 1 - 1e-9)))
        for score in scores
    ]
    intercept, slope = 0.0, 1.0
    for _ in range(100):
        probabilities = [
            1.0 / (1.0 + math.exp(-max(min(intercept + slope * value, 35.0), -35.0)))
            for value in logits
        ]
        g0 = math.fsum(label - probability for label, probability in zip(labels, probabilities, strict=True))
        g1 = math.fsum(
            (label - probability) * value
            for label, probability, value in zip(labels, probabilities, logits, strict=True)
        )
        w = [probability * (1.0 - probability) for probability in probabilities]
        h00 = math.fsum(w)
        h01 = math.fsum(weight * value for weight, value in zip(w, logits, strict=True))
        h11 = math.fsum(weight * value * value for weight, value in zip(w, logits, strict=True))
        determinant = h00 * h11 - h01 * h01
        if determinant <= 1e-12:
            return math.nan, math.nan
        step0 = (g0 * h11 - g1 * h01) / determinant
        step1 = (g1 * h00 - g0 * h01) / determinant
        intercept += step0
        slope += step1
        if max(abs(step0), abs(step1)) < 1e-10:
            break
    return intercept, slope


def e_value(risk_ratio: float) -> float:
    if not math.isfinite(risk_ratio) or risk_ratio <= 0:
        return math.nan
    harmful_scale = risk_ratio if risk_ratio >= 1 else 1.0 / risk_ratio
    return harmful_scale + math.sqrt(harmful_scale * (harmful_scale - 1.0))


def risk_ratio_bias_factor(exposure_rr: float, outcome_rr: float) -> float:
    return exposure_rr * outcome_rr / (exposure_rr + outcome_rr - 1.0)


def finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value
