from __future__ import annotations

from copy import deepcopy

import duckdb
import pytest
import yaml
from pydantic import ValidationError

from barnabus.config import PipelineConfig, load_config
from barnabus.contracts import assert_many_to_one_join
from barnabus.errors import ContractViolation
from barnabus.sql import case_id_expression

from conftest import CONFIG_PATH


def test_production_config_is_strict_and_complete() -> None:
    config, raw = load_config(CONFIG_PATH)
    assert raw
    assert config.version == 1
    assert config.study_window.start == "2025-08-01"
    assert config.study_window.end == "2026-07-31"
    assert config.grains["canonical_events"] == "semantic_event_key"
    assert config.grains["case_workflow"] == "case_id"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"unexpected_setting": True}),
        lambda payload: payload["schema"]["operational_event_types"].append(
            payload["schema"]["workflow_event_order"][0]
        ),
        lambda payload: payload["event_time"].update({"convention": "local"}),
    ],
)
def test_invalid_config_fails_validation(mutation) -> None:  # type: ignore[no-untyped-def]
    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    invalid = deepcopy(payload)
    mutation(invalid)
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(invalid)


def test_case_identifier_normalization_is_explicit() -> None:
    connection = duckdb.connect()
    try:
        expression = case_id_expression("raw_id")
        rows = connection.execute(
            f"SELECT raw_id, {expression} AS normalized FROM "
            "(VALUES (' 42 '), ('c000000007'), ('C000000009'), ('not-a-key')) t(raw_id)"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [
        (" 42 ", "C000000042"),
        ("c000000007", "C000000007"),
        ("C000000009", "C000000009"),
        ("not-a-key", None),
    ]


def test_many_to_one_contract_fails_loudly_on_duplicate_dimension_key() -> None:
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TABLE facts(id INTEGER); INSERT INTO facts VALUES (1), (2)")
        connection.execute("CREATE TABLE dimension(id INTEGER); INSERT INTO dimension VALUES (1), (1), (2)")
        with pytest.raises(ContractViolation, match="grain_unique"):
            assert_many_to_one_join(
                connection, "facts", "dimension", ["id"], "facts_to_dimension"
            )
    finally:
        connection.close()


def test_many_to_one_contract_fails_loudly_on_unmatched_key() -> None:
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TABLE facts(id INTEGER); INSERT INTO facts VALUES (1), (2)")
        connection.execute("CREATE TABLE dimension(id INTEGER); INSERT INTO dimension VALUES (1)")
        with pytest.raises(ContractViolation, match="referential_integrity"):
            assert_many_to_one_join(
                connection, "facts", "dimension", ["id"], "facts_to_dimension"
            )
    finally:
        connection.close()
