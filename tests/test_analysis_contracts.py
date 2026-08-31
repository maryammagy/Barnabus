from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from barnabus.analysis import (
    Episode,
    NumberRegistry,
    _c1_observation,
    _c2_observation,
    _register_table_numbers,
    _service_effects,
)
from barnabus.analyst_reproduction import REVIEWED_QUERY


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _episode(**changes: object) -> Episode:
    values: dict[str, object] = {
        "case_id": "C000000001",
        "site": "A",
        "service_code": "A-CARD",
        "specialty": "CARD",
        "ward_id": "A-W1",
        "referral_date": date(2026, 5, 1),
        "referral_week": date(2026, 4, 27),
        "sex": "F",
        "age": 55,
        "cancellation_proxy": False,
        "snapshot_cancelled": None,
        "snapshot_readiness_days": None,
        "days_to_ready": None,
        "days_to_close": None,
        "days_to_complete": None,
        "days_to_recommendation": None,
        "admin_followup_days": 30.0,
        "recommendation_action_eligible": False,
        "has_assessment": False,
    }
    values.update(changes)
    return Episode(**values)  # type: ignore[arg-type]


def test_immortal_time_rule_never_requires_assessment() -> None:
    episode = _episode(
        cancellation_proxy=True,
        days_to_close=2.0,
        has_assessment=False,
    )
    assert _c1_observation(episode) == (True, 1)


def test_cancellation_before_readiness_is_terminal_not_ready() -> None:
    episode = _episode(
        cancellation_proxy=True,
        days_to_close=5.0,
        days_to_ready=10.0,
    )
    assert _c2_observation(episode) == (90.0, False, True)


def test_service_effect_helper_runs_without_legacy_comparison_state() -> None:
    episodes: list[Episode] = []
    for site in ("A", "B"):
        for period_date, cancelled in (
            (date(2026, 4, 1), False),
            (date(2026, 6, 1), True),
        ):
            episodes.append(
                _episode(
                    case_id=f"{site}-{period_date.isoformat()}",
                    site=site,
                    service_code=f"{site}-CARD",
                    referral_date=period_date,
                    referral_week=period_date,
                    cancellation_proxy=cancelled,
                    days_to_close=1.0 if cancelled else None,
                    admin_followup_days=100.0,
                )
            )
    effects = _service_effects(episodes, date(2026, 5, 1), "c1")
    assert len(effects) == 1
    assert effects[0]["specialty"] == "CARD"


def test_execution_config_declares_missing_primary_exposure() -> None:
    config = yaml.safe_load(
        (REPOSITORY_ROOT / "config" / "analysis-execution-v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert config["frozen_tag"] == "prespec-v1"
    assert config["exposure"]["authoritative_selected_service_list_available"] is False
    assert config["exposure"]["primary_status"] == "not_identified"
    assert "sensitivity_only" in config["exposure"]["sensitivity_proxy_label"]


def test_analyst_sql_is_reviewed_literal_not_source_execution() -> None:
    assert "assessment_generated" in REVIEWED_QUERY
    assert "JOIN snapshot_cases" in REVIEWED_QUERY
    source = (REPOSITORY_ROOT / "src" / "barnabus" / "analyst_reproduction.py").read_text(
        encoding="utf-8"
    )
    assert "supplied_query_path.read_text" not in source
    assert "connection.execute(supplied" not in source


def test_every_numeric_table_cell_receives_provenance_id() -> None:
    registry = NumberRegistry("input", "config", "0" * 40)
    rows = [{"claim": "c1", "estimate": -0.01, "n": 12, "quantity_status": "sensitivity_only"}]
    _register_table_numbers(registry, "claims", rows)
    assert rows[0]["estimate_number_id"]
    assert rows[0]["n_number_id"]
    assert len(registry.rows) == 2
    assert all(row["implementation_commit"] == "0" * 40 for row in registry.rows)


def test_reproduction_path_contains_no_notebook() -> None:
    assert not list(REPOSITORY_ROOT.rglob("*.ipynb"))
