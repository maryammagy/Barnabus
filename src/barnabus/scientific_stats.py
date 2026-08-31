"""Deterministic statistics for the scientific supplement."""

from __future__ import annotations

import math
import random
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

from barnabus.stats import mean, quantile


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def wilson_interval(successes: int, total: int, alpha: float = 0.05) -> tuple[float, float]:
    if total <= 0:
        return math.nan, math.nan
    # z=1.959963984540054 is Phi^-1(0.975); alpha is fixed at 0.05 in v1.
    if not math.isclose(alpha, 0.05):
        raise ValueError("scientific-v1 Wilson intervals require alpha=0.05")
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    half = z * math.sqrt(
        proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)
    ) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def agreement_metrics(left: Sequence[int], right: Sequence[int]) -> dict[str, float]:
    if not left or len(left) != len(right):
        raise ValueError("agreement labels must align and be non-empty")
    total = len(left)
    agreement = sum(a == b for a, b in zip(left, right, strict=True)) / total
    left_positive = sum(left) / total
    right_positive = sum(right) / total
    expected = left_positive * right_positive + (1.0 - left_positive) * (
        1.0 - right_positive
    )
    average_positive = (left_positive + right_positive) / 2.0
    ac1_expected = 2.0 * average_positive * (1.0 - average_positive)
    return {
        "raw_agreement": agreement,
        "chance_excess": agreement - expected,
        "cohen_kappa": (agreement - expected) / (1.0 - expected)
        if expected < 1.0
        else math.nan,
        "gwet_ac1": (agreement - ac1_expected) / (1.0 - ac1_expected)
        if ac1_expected < 1.0
        else math.nan,
    }


def rank_auc(labels: Sequence[int], scores: Sequence[float]) -> float:
    """Exact AUC in O(n log n), with half credit for tied scores."""

    if not labels or len(labels) != len(scores):
        return math.nan
    ordered = sorted(zip(scores, labels, strict=True), key=lambda item: item[0])
    positives = sum(label == 1 for label in labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return math.nan
    negative_seen = 0
    wins = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        group = ordered[index:end]
        group_positives = sum(label == 1 for _, label in group)
        group_negatives = len(group) - group_positives
        wins += group_positives * negative_seen + 0.5 * group_positives * group_negatives
        negative_seen += group_negatives
        index = end
    return wins / (positives * negatives)


def average_precision(labels: Sequence[int], scores: Sequence[float]) -> float:
    """Threshold-grouped average precision, with deterministic tie handling."""

    if not labels or len(labels) != len(scores):
        return math.nan
    positives = sum(label == 1 for label in labels)
    if positives == 0:
        return math.nan
    ordered = sorted(zip(scores, labels, strict=True), key=lambda item: item[0], reverse=True)
    true_positive = 0
    false_positive = 0
    previous_recall = 0.0
    result = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        group = ordered[index:end]
        true_positive += sum(label == 1 for _, label in group)
        false_positive += sum(label == 0 for _, label in group)
        recall = true_positive / positives
        precision = true_positive / (true_positive + false_positive)
        result += (recall - previous_recall) * precision
        previous_recall = recall
        index = end
    return result


def classification_metrics(labels: Sequence[int], probabilities: Sequence[float], threshold: float) -> dict[str, float]:
    if not labels or len(labels) != len(probabilities):
        raise ValueError("labels and probabilities must align")
    predictions = [int(value >= threshold) for value in probabilities]
    tp = sum(y == 1 and p == 1 for y, p in zip(labels, predictions, strict=True))
    tn = sum(y == 0 and p == 0 for y, p in zip(labels, predictions, strict=True))
    fp = sum(y == 0 and p == 1 for y, p in zip(labels, predictions, strict=True))
    fn = sum(y == 1 and p == 0 for y, p in zip(labels, predictions, strict=True))

    def safe(numerator: float, denominator: float) -> float:
        return numerator / denominator if denominator else math.nan

    clipped = [min(max(value, 1e-12), 1.0 - 1e-12) for value in probabilities]
    sensitivity = safe(tp, tp + fn)
    specificity = safe(tn, tn + fp)
    return {
        "sensitivity": sensitivity,
        "specificity": specificity,
        "ppv": safe(tp, tp + fp),
        "npv": safe(tn, tn + fn),
        "balanced_accuracy": mean([sensitivity, specificity]),
        "auc": rank_auc(labels, probabilities),
        "brier": mean([(y - p) ** 2 for y, p in zip(labels, probabilities, strict=True)]),
        "log_loss": -mean(
            [
                y * math.log(p) + (1 - y) * math.log(1.0 - p)
                for y, p in zip(labels, clipped, strict=True)
            ]
        ),
    }


def service_cluster_classification_intervals(
    records: Sequence[dict[str, Any]],
    label_getter: Callable[[dict[str, Any]], int],
    score_getter: Callable[[dict[str, Any]], float],
    threshold: float,
    draws: int,
    seed: int,
    cluster_field: str = "service_code",
    alpha: float = 0.05,
    requested_metrics: Sequence[str] | None = None,
) -> dict[str, tuple[float, float, float, int]]:
    """Exact service bootstrap for all binary metrics using sufficient statistics.

    AUC is evaluated from a precomputed service-by-service win matrix, so every
    bootstrap draw is identical to duplicating sampled service rows but costs
    O(k^2), where k is the number of services, instead of O(n^2).
    """

    if not records:
        raise ValueError("classification bootstrap records must be non-empty")
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        clusters[str(record[cluster_field])].append(record)
    names = sorted(clusters)
    labels = [label_getter(record) for record in records]
    scores = [score_getter(record) for record in records]
    point = classification_metrics(labels, scores, threshold)
    point["net_benefit"] = net_benefit(labels, scores, threshold)
    if requested_metrics is not None:
        unknown = set(requested_metrics) - set(point)
        if unknown:
            raise ValueError(f"unknown requested classification metrics: {sorted(unknown)}")
        point = {name: point[name] for name in requested_metrics}
    metrics = tuple(point)
    if len(names) < 2:
        return {
            name: (float(point[name]), math.nan, math.nan, len(names))
            for name in metrics
        }

    summaries: list[dict[str, float]] = []
    positives_by_cluster: list[list[float]] = []
    negatives_by_cluster: list[list[float]] = []
    for name in names:
        cluster_labels = [label_getter(record) for record in clusters[name]]
        cluster_scores = [score_getter(record) for record in clusters[name]]
        predictions = [int(score >= threshold) for score in cluster_scores]
        clipped = [min(max(score, 1e-12), 1.0 - 1e-12) for score in cluster_scores]
        summaries.append(
            {
                "n": float(len(cluster_labels)),
                "tp": float(sum(y == 1 and p == 1 for y, p in zip(cluster_labels, predictions, strict=True))),
                "tn": float(sum(y == 0 and p == 0 for y, p in zip(cluster_labels, predictions, strict=True))),
                "fp": float(sum(y == 0 and p == 1 for y, p in zip(cluster_labels, predictions, strict=True))),
                "fn": float(sum(y == 1 and p == 0 for y, p in zip(cluster_labels, predictions, strict=True))),
                "positive": float(sum(cluster_labels)),
                "negative": float(len(cluster_labels) - sum(cluster_labels)),
                "brier_sum": math.fsum(
                    (label - score) ** 2
                    for label, score in zip(cluster_labels, cluster_scores, strict=True)
                ),
                "log_loss_sum": -math.fsum(
                    label * math.log(score) + (1 - label) * math.log(1.0 - score)
                    for label, score in zip(cluster_labels, clipped, strict=True)
                ),
            }
        )
        positives_by_cluster.append(
            [score for label, score in zip(cluster_labels, cluster_scores, strict=True) if label == 1]
        )
        negatives_by_cluster.append(
            sorted(score for label, score in zip(cluster_labels, cluster_scores, strict=True) if label == 0)
        )

    win_matrix: list[list[float]] = []
    if "auc" in metrics:
        for positive_scores in positives_by_cluster:
            row: list[float] = []
            for negative_scores in negatives_by_cluster:
                wins = math.fsum(
                    bisect_left(negative_scores, score)
                    + 0.5 * (bisect_right(negative_scores, score) - bisect_left(negative_scores, score))
                    for score in positive_scores
                )
                row.append(wins)
            win_matrix.append(row)

    values: dict[str, list[float]] = {name: [] for name in metrics}
    generator = random.Random(seed)
    cluster_count = len(names)
    for _ in range(draws):
        multiplicities = [0] * cluster_count
        for _slot in range(cluster_count):
            multiplicities[generator.randrange(cluster_count)] += 1

        def total(field: str) -> float:
            return math.fsum(
                multiplicities[index] * summaries[index][field]
                for index in range(cluster_count)
            )

        tp, tn, fp, fn = (total(field) for field in ("tp", "tn", "fp", "fn"))

        def safe(numerator: float, denominator: float) -> float:
            return numerator / denominator if denominator else math.nan

        sensitivity = safe(tp, tp + fn)
        specificity = safe(tn, tn + fp)
        positives = total("positive")
        negatives = total("negative")
        auc_numerator = (
            math.fsum(
                multiplicities[left] * multiplicities[right] * win_matrix[left][right]
                for left in range(cluster_count)
                for right in range(cluster_count)
            )
            if "auc" in metrics
            else math.nan
        )
        n = total("n")
        draw_metrics = {
            "sensitivity": sensitivity,
            "specificity": specificity,
            "ppv": safe(tp, tp + fp),
            "npv": safe(tn, tn + fn),
            "balanced_accuracy": (sensitivity + specificity) / 2.0
            if math.isfinite(sensitivity) and math.isfinite(specificity)
            else math.nan,
            "auc": safe(auc_numerator, positives * negatives),
            "brier": safe(total("brier_sum"), n),
            "log_loss": safe(total("log_loss_sum"), n),
            "net_benefit": safe(tp, n)
            - safe(fp, n) * threshold / (1.0 - threshold),
        }
        for name in metrics:
            value = draw_metrics[name]
            if math.isfinite(value):
                values[name].append(value)

    result: dict[str, tuple[float, float, float, int]] = {}
    for name in metrics:
        samples = values[name]
        result[name] = (
            float(point[name]),
            quantile(samples, alpha / 2.0) if samples else math.nan,
            quantile(samples, 1.0 - alpha / 2.0) if samples else math.nan,
            cluster_count,
        )
    return result


def service_cluster_bootstrap(
    records: Sequence[dict[str, Any]],
    metric: Callable[[Sequence[dict[str, Any]]], float],
    draws: int,
    seed: int,
    cluster_field: str = "service_code",
    alpha: float = 0.05,
) -> tuple[float, float, float, int]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        clusters[str(record[cluster_field])].append(record)
    names = sorted(clusters)
    estimate = metric(records)
    if len(names) < 2:
        return estimate, math.nan, math.nan, len(names)
    generator = random.Random(seed)
    values: list[float] = []
    for _ in range(draws):
        sampled: list[dict[str, Any]] = []
        for _cluster_index in names:
            sampled.extend(clusters[names[generator.randrange(len(names))]])
        value = metric(sampled)
        if math.isfinite(value):
            values.append(value)
    if not values:
        return estimate, math.nan, math.nan, len(names)
    return estimate, quantile(values, alpha / 2.0), quantile(values, 1.0 - alpha / 2.0), len(names)


def reliability_bins(
    records: Sequence[dict[str, Any]], bins: int, score_field: str = "score", outcome_field: str = "outcome"
) -> list[dict[str, Any]]:
    ordered = sorted(records, key=lambda row: (float(row[score_field]), str(row.get("case_id", ""))))
    result: list[dict[str, Any]] = []
    for bin_index in range(bins):
        lower = bin_index * len(ordered) // bins
        upper = (bin_index + 1) * len(ordered) // bins
        rows = ordered[lower:upper]
        if not rows:
            continue
        outcomes = [int(row[outcome_field]) for row in rows]
        successes = sum(outcomes)
        interval = wilson_interval(successes, len(rows))
        result.append(
            {
                "bin": bin_index + 1,
                "n": len(rows),
                "score_min": min(float(row[score_field]) for row in rows),
                "score_max": max(float(row[score_field]) for row in rows),
                "mean_predicted": mean([float(row[score_field]) for row in rows]),
                "observed_rate": successes / len(rows),
                "observed_lower": interval[0],
                "observed_upper": interval[1],
            }
        )
    return result


def service_cluster_reliability_bins(
    records: Sequence[dict[str, Any]],
    bins: int,
    draws: int,
    seed: int,
    score_field: str = "score",
    outcome_field: str = "outcome",
    cluster_field: str = "service_code",
    alpha: float = 0.05,
) -> list[dict[str, Any]]:
    """Reliability bins with fixed bins and service-cluster bootstrap intervals."""

    point_rows = reliability_bins(records, bins, score_field, outcome_field)
    ordered = sorted(
        records,
        key=lambda row: (float(row[score_field]), str(row.get("case_id", ""))),
    )
    assigned: list[dict[str, Any]] = []
    for bin_index in range(len(point_rows)):
        lower = bin_index * len(ordered) // bins
        upper = (bin_index + 1) * len(ordered) // bins
        for row in ordered[lower:upper]:
            assigned.append({**row, "_reliability_bin": bin_index})
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in assigned:
        clusters[str(row[cluster_field])].append(row)
    names = sorted(clusters)
    cluster_successes: list[list[int]] = []
    cluster_totals: list[list[int]] = []
    for name in names:
        successes = [0] * len(point_rows)
        totals = [0] * len(point_rows)
        for row in clusters[name]:
            bin_index = int(row["_reliability_bin"])
            successes[bin_index] += int(row[outcome_field])
            totals[bin_index] += 1
        cluster_successes.append(successes)
        cluster_totals.append(totals)
    samples: list[list[float]] = [[] for _ in point_rows]
    if len(names) >= 2:
        generator = random.Random(seed)
        for _ in range(draws):
            multiplicities = [0] * len(names)
            for _slot in names:
                multiplicities[generator.randrange(len(names))] += 1
            successes = [
                sum(
                    multiplicities[cluster_index]
                    * cluster_successes[cluster_index][bin_index]
                    for cluster_index in range(len(names))
                )
                for bin_index in range(len(point_rows))
            ]
            totals = [
                sum(
                    multiplicities[cluster_index]
                    * cluster_totals[cluster_index][bin_index]
                    for cluster_index in range(len(names))
                )
                for bin_index in range(len(point_rows))
            ]
            for bin_index, total in enumerate(totals):
                if total:
                    samples[bin_index].append(successes[bin_index] / total)
    output: list[dict[str, Any]] = []
    for index, row in enumerate(point_rows):
        values = samples[index]
        output.append(
            {
                **row,
                "observed_lower": quantile(values, alpha / 2.0) if values else math.nan,
                "observed_upper": quantile(values, 1.0 - alpha / 2.0) if values else math.nan,
                "service_clusters": len(names),
                "interval_method": "service_cluster_percentile_bootstrap"
                if values
                else "not_estimable_fewer_than_two_service_clusters",
            }
        )
    return output


def net_benefit(labels: Sequence[int], scores: Sequence[float], threshold: float) -> float:
    if not labels or len(labels) != len(scores) or not 0.0 < threshold < 1.0:
        return math.nan
    predictions = [score >= threshold for score in scores]
    true_positive = sum(y == 1 and prediction for y, prediction in zip(labels, predictions, strict=True))
    false_positive = sum(y == 0 and prediction for y, prediction in zip(labels, predictions, strict=True))
    return true_positive / len(labels) - false_positive / len(labels) * threshold / (1.0 - threshold)


def population_stability_index(reference: Sequence[float], current: Sequence[float], bins: int = 10) -> float:
    if not reference or not current:
        return math.nan
    boundaries = [quantile(reference, index / bins) for index in range(1, bins)]

    def counts(values: Sequence[float]) -> list[int]:
        output = [0] * bins
        for value in values:
            index = sum(value > boundary for boundary in boundaries)
            output[min(index, bins - 1)] += 1
        return output

    reference_counts = counts(reference)
    current_counts = counts(current)
    total_reference = len(reference)
    total_current = len(current)
    result = 0.0
    for left, right in zip(reference_counts, current_counts, strict=True):
        reference_fraction = max(left / total_reference, 1e-6)
        current_fraction = max(right / total_current, 1e-6)
        result += (current_fraction - reference_fraction) * math.log(
            current_fraction / reference_fraction
        )
    return result


def by_adjust(p_values: Sequence[float]) -> list[float]:
    count = len(p_values)
    if count == 0:
        return []
    harmonic = math.fsum(1.0 / index for index in range(1, count + 1))
    ordered = sorted(enumerate(p_values), key=lambda item: (item[1], item[0]))
    adjusted = [1.0] * count
    running = 1.0
    for reverse_index in range(count - 1, -1, -1):
        original, value = ordered[reverse_index]
        rank = reverse_index + 1
        running = min(running, value * count * harmonic / rank, 1.0)
        adjusted[original] = running
    return adjusted


def two_way_fixed_effect_t(
    outcomes: Sequence[Sequence[float]], treatment: Sequence[Sequence[float]]
) -> tuple[float, float]:
    clusters = len(outcomes)
    periods = len(outcomes[0]) if outcomes else 0
    if clusters < 3 or periods < 2 or any(
        len(row) != periods for row in list(outcomes) + list(treatment)
    ):
        return math.nan, math.nan
    observed = [
        [math.isfinite(float(outcomes[i][j])) and math.isfinite(float(treatment[i][j])) for j in range(periods)]
        for i in range(clusters)
    ]

    def residualize(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
        result = [
            [float(matrix[i][j]) if observed[i][j] else math.nan for j in range(periods)]
            for i in range(clusters)
        ]
        # Alternating projections give exact two-way demeaning for the unbalanced
        # panel produced when component-specific wash-in periods are omitted.
        for _ in range(200):
            largest = 0.0
            for i in range(clusters):
                values = [result[i][j] for j in range(periods) if observed[i][j]]
                if values:
                    shift = mean(values)
                    largest = max(largest, abs(shift))
                    for j in range(periods):
                        if observed[i][j]:
                            result[i][j] -= shift
            for j in range(periods):
                values = [result[i][j] for i in range(clusters) if observed[i][j]]
                if values:
                    shift = mean(values)
                    largest = max(largest, abs(shift))
                    for i in range(clusters):
                        if observed[i][j]:
                            result[i][j] -= shift
            if largest < 1e-12:
                break
        return result

    x = residualize(treatment)
    y = residualize(outcomes)
    xx = math.fsum(
        x[i][j] * x[i][j]
        for i in range(clusters)
        for j in range(periods)
        if observed[i][j]
    )
    if xx <= 1e-12:
        return math.nan, math.nan
    beta = math.fsum(
        x[i][j] * y[i][j]
        for i in range(clusters)
        for j in range(periods)
        if observed[i][j]
    ) / xx
    scores: list[float] = []
    for i in range(clusters):
        score = math.fsum(
            x[i][j] * (y[i][j] - beta * x[i][j])
            for j in range(periods)
            if observed[i][j]
        )
        scores.append(score)
    variance = clusters / (clusters - 1.0) * math.fsum(value * value for value in scores) / (xx * xx)
    standard_error = math.sqrt(max(variance, 0.0))
    return beta, beta / standard_error if standard_error > 0 else math.nan


def simulate_staggered_statistics(
    base_rates: Sequence[float],
    mean_weekly_counts: Sequence[int],
    rollout_weeks: Sequence[int],
    strata: Sequence[str],
    weeks: int,
    effect: float,
    icc: float,
    simulations: int,
    seed: int,
    seasonality_amplitude: float,
    wash_in_weeks: int = 0,
    serial_correlation: float = 0.0,
    serial_shock_sd: float = 0.0,
    policy_week: int | None = None,
    policy_risk_difference: float = 0.0,
    site_time_differential_over_study: float = 0.0,
) -> list[tuple[float, float]]:
    if not (
        len(base_rates)
        == len(mean_weekly_counts)
        == len(rollout_weeks)
        == len(strata)
    ):
        raise ValueError("power inputs must align by component")
    generator = random.Random(seed)
    stratum_indices: dict[str, list[int]] = defaultdict(list)
    for index, stratum in enumerate(strata):
        stratum_indices[stratum].append(index)
    rollout_template = {
        stratum: sorted(rollout_weeks[index] for index in indices)
        for stratum, indices in stratum_indices.items()
    }
    statistics: list[tuple[float, float]] = []
    for _ in range(simulations):
        randomized_rollouts = list(rollout_weeks)
        for stratum, indices in sorted(stratum_indices.items()):
            assigned = list(rollout_template[stratum])
            generator.shuffle(assigned)
            for index, rollout in zip(sorted(indices), assigned, strict=True):
                randomized_rollouts[index] = rollout
        treatment = [
            [
                math.nan
                if randomized_rollouts[index] <= week < randomized_rollouts[index] + wash_in_weeks
                else float(week >= randomized_rollouts[index] + wash_in_weeks)
                for week in range(weeks)
            ]
            for index in range(len(base_rates))
        ]
        outcomes: list[list[float]] = []
        for index, base in enumerate(base_rates):
            row: list[float] = []
            persistent_sd = math.sqrt(max(icc, 0.0) * base * (1.0 - base))
            component_shift = generator.normalvariate(0.0, persistent_sd)
            component_time_slope = generator.normalvariate(0.0, math.sqrt(max(icc, 0.0)) * 0.02)
            serial_shift = generator.normalvariate(0.0, serial_shock_sd)
            for week in range(weeks):
                if not math.isfinite(treatment[index][week]):
                    row.append(math.nan)
                    continue
                serial_shift = (
                    serial_correlation * serial_shift
                    + generator.normalvariate(
                        0.0,
                        serial_shock_sd * math.sqrt(max(1.0 - serial_correlation**2, 0.0)),
                    )
                )
                expected = base + effect * treatment[index][week]
                expected += seasonality_amplitude * math.sin(2.0 * math.pi * week / 52.0)
                expected += component_shift + serial_shift
                expected += component_time_slope * (
                    week / max(weeks - 1, 1) - 0.5
                )
                if policy_week is not None and week >= policy_week:
                    expected += policy_risk_difference
                site_direction = -0.5 if str(strata[index]) == sorted(set(strata))[0] else 0.5
                expected += (
                    site_direction
                    * site_time_differential_over_study
                    * week
                    / max(weeks - 1, 1)
                )
                expected = min(max(expected, 0.005), 0.995)
                count = max(10, int(round(mean_weekly_counts[index])))
                standard_deviation = math.sqrt(count * expected * (1.0 - expected))
                events = int(round(count * expected + generator.normalvariate(0.0, standard_deviation)))
                events = min(max(events, 0), count)
                row.append(events / count)
            outcomes.append(row)
        estimate, statistic = two_way_fixed_effect_t(outcomes, treatment)
        if math.isfinite(statistic) and math.isfinite(estimate):
            statistics.append((estimate, statistic))
    return statistics


def calibrated_favorable_tail_power(
    statistics: Sequence[tuple[float, float]], lower_critical_value: float
) -> float:
    if not statistics:
        return math.nan
    return sum(
        estimate < 0.0 and statistic <= lower_critical_value
        for estimate, statistic in statistics
    ) / len(statistics)
