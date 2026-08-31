from __future__ import annotations

from pathlib import Path

import yaml

import pytest

from barnabus.scientific import (
    _assert_declared_grains,
    _bounded_float,
    _gaps,
    _monitor_catalog,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict[str, object]:
    return yaml.safe_load(
        (REPOSITORY_ROOT / "config" / "scientific-supplement-v1.yaml").read_text(
            encoding="utf-8"
        )
    )


def test_scientific_config_preserves_locked_parent_and_invalidations() -> None:
    config = _config()
    assert config["locked_results"]["commit"] == "87a183979b6b019d916c05bd4775120b4269d6cf"  # type: ignore[index]
    assert config["labels"]["adjudication_status"] == "not_supplied"  # type: ignore[index]
    assert config["recommendation_model"]["feature_lineage_available"] is False  # type: ignore[index]
    assert config["recommendation_model"]["target_definition_available"] is False  # type: ignore[index]
    assert config["uplift"]["causal_identification"] is False  # type: ignore[index]


def test_monitor_catalog_covers_required_domains_and_operational_controls() -> None:
    config = _config()
    catalog = _monitor_catalog(config)
    domains = {row["domain"] for row in catalog}
    assert set(config["monitoring"]["source_domains"]).issubset(domains)  # type: ignore[index]
    assert any(row["monitor_id"] == "ACT-01" for row in catalog)
    assert any(row["monitor_id"] == "SCALE-01" for row in catalog)
    assert next(row for row in catalog if row["monitor_id"] == "ACT-01")["data_maturation"] == "wait_9_days_before_statistical_alerting"
    assert next(row for row in catalog if row["monitor_id"] == "OUT-01")["data_maturation"] == "wait_90_days_before_statistical_alerting"
    assert next(row for row in catalog if row["monitor_id"] == "AUTH-01")["alert_budget"] is None
    assert all("shadow_validation" in row["restart_rule"] for row in catalog)
    assert all(row["alert_budget"] in {None, 3} for row in catalog)


def test_open_gaps_prevent_overclaiming() -> None:
    gaps = {row["gap_id"]: row for row in _gaps()}
    assert gaps["G-01"]["status"] == "open"
    assert "feature lineage" in gaps["G-03"]["gap"]
    assert "assignment mechanism" in gaps["G-05"]["gap"]
    assert "sealed workload" in gaps["G-10"]["gap"]


def test_scientific_input_ranges_and_declared_grains_fail_loudly() -> None:
    with pytest.raises(ValueError, match="within"):
        _bounded_float("1.2", "score", 0.0, 1.0)
    duplicate = {
        "reviewer_agreement": [
            {"comparison": "r1_vs_r2", "metric": "kappa"},
            {"comparison": "r1_vs_r2", "metric": "kappa"},
        ]
    }
    with pytest.raises(ValueError, match="violates declared grain"):
        _assert_declared_grains(duplicate)


def test_scientific_reproduction_path_contains_no_notebook_or_manual_result_literal() -> None:
    assert not list(REPOSITORY_ROOT.rglob("*.ipynb"))
    source = (REPOSITORY_ROOT / "src" / "barnabus" / "scientific.py").read_text(
        encoding="utf-8"
    )
    assert "clinical_notes.csv" in source
    assert "untrusted_inputs_not_used" in source
    assert "supplied_auc" in source
    assert "unreproduced_supplied_number" in source
