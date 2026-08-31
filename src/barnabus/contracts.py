"""Loud data-contract and cardinality assertions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import duckdb

from barnabus.errors import ContractViolation


@dataclass(frozen=True)
class ContractResult:
    name: str
    passed: bool
    observed: int | str
    expected: int | str


def require(condition: bool, name: str, observed: object, expected: object) -> ContractResult:
    result = ContractResult(name, condition, str(observed), str(expected))
    if not condition:
        raise ContractViolation(
            f"contract {name!r} failed: observed={observed!r}, expected={expected!r}"
        )
    return result


def scalar(connection: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    if row is None or len(row) != 1:
        raise ContractViolation("contract query did not return exactly one scalar")
    return int(row[0])


def assert_unique(
    connection: duckdb.DuckDBPyConnection,
    relation: str,
    columns: Sequence[str],
    label: str,
) -> ContractResult:
    group = ", ".join(columns)
    duplicate_groups = scalar(
        connection,
        f"SELECT count(*) FROM (SELECT {group} FROM {relation} "
        f"GROUP BY {group} HAVING count(*) > 1)",
    )
    return require(duplicate_groups == 0, f"{label}.grain_unique", duplicate_groups, 0)


def assert_many_to_one_join(
    connection: duckdb.DuckDBPyConnection,
    left_relation: str,
    right_relation: str,
    keys: Sequence[str],
    label: str,
    require_match: bool = True,
) -> list[ContractResult]:
    right_unique = assert_unique(connection, right_relation, keys, f"{label}.right")
    condition = " AND ".join(f"l.{key} = r.{key}" for key in keys)
    first_key = keys[0]
    left_count = scalar(connection, f"SELECT count(*) FROM {left_relation}")
    joined_count, unmatched = connection.execute(
        f"SELECT count(*), count(*) FILTER (WHERE r.{first_key} IS NULL) "
        f"FROM {left_relation} l LEFT JOIN {right_relation} r ON {condition}"
    ).fetchone()
    results = [
        right_unique,
        require(
            int(joined_count) == left_count,
            f"{label}.row_preservation",
            int(joined_count),
            left_count,
        ),
    ]
    if require_match:
        results.append(require(int(unmatched) == 0, f"{label}.referential_integrity", int(unmatched), 0))
    return results


def assert_one_to_one_join(
    connection: duckdb.DuckDBPyConnection,
    left_relation: str,
    right_relation: str,
    keys: Sequence[str],
    label: str,
) -> list[ContractResult]:
    return [
        assert_unique(connection, left_relation, keys, f"{label}.left"),
        assert_unique(connection, right_relation, keys, f"{label}.right"),
    ]
