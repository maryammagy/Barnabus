"""Security-first natural-language analytics service.

The model is deliberately outside the authorization boundary.  Trusted code
materializes a per-principal DuckDB containing only authorized rows and
columns.  Generated SQL is then parsed, planned, and executed against that
physical database through a read-only connection with external access
disabled.  Database values are returned as typed JSON and are never sent back
to a model for prose generation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import importlib
import json
import multiprocessing
import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any, Protocol, Sequence
import unicodedata
import uuid

import duckdb
import yaml


SAFE_SOURCE_TYPES: dict[str, str] = {
    "case_key": "VARCHAR",
    "site": "VARCHAR",
    "service_code": "VARCHAR",
    "referral_date": "DATE",
    "age_band": "VARCHAR",
    "cancellation_proxy": "BOOLEAN",
    "readiness_days": "DOUBLE",
    "total_cost_cad": "DOUBLE",
}
SENSITIVE_OR_FREE_TEXT_COLUMNS = {
    "clinical_notes",
    "note",
    "notes",
    "patient_id",
    "patient_name",
    "name",
    "date_of_birth",
    "dob",
    "clinician_id",
}
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
OUTPUT_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class AnalyticsServiceError(RuntimeError):
    """Base error whose message is safe to expose."""


class PolicyError(AnalyticsServiceError):
    pass


class SourceContractError(AnalyticsServiceError):
    pass


class SqlRejected(AnalyticsServiceError):
    pass


class QueryTimedOut(AnalyticsServiceError):
    pass


@dataclass(frozen=True)
class Limits:
    max_question_chars: int
    max_sql_chars: int
    max_rows: int
    max_columns: int
    max_result_cell_chars: int
    statement_timeout_ms: int
    scan_ceiling_rows: int
    max_table_scans: int
    max_concurrent_queries: int


@dataclass(frozen=True)
class PrincipalPolicy:
    principal_id: str
    scope_basis: str
    sites: tuple[str, ...]
    services: tuple[str, ...]
    columns: tuple[str, ...]
    referral_date_from: str
    referral_date_to_exclusive: str


@dataclass(frozen=True)
class AnalyticsPolicy:
    version: str
    prompt_version: str
    source_table: str
    source_columns: dict[str, str]
    public_columns: tuple[str, ...]
    categorical_patterns: dict[str, str]
    limits: Limits
    principals: dict[str, PrincipalPolicy]
    sha256: str


@dataclass(frozen=True)
class ScopeInfo:
    path: Path
    principal: PrincipalPolicy
    source_sha256: str
    scope_sha256: str
    policy_sha256: str
    row_count: int


@dataclass(frozen=True)
class ModelDecision:
    action: str
    reason: str
    sql: str | None = None
    clarification: str | None = None


class ModelProvider(Protocol):
    provider_name: str
    provider_version: str

    def generate(
        self,
        question: str,
        *,
        schema: tuple[str, ...],
        principal: PrincipalPolicy,
    ) -> ModelDecision: ...


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    principal_id: str
    question: str
    expected_action: str | None = None
    label_source: str | None = None
    rationale: str | None = None
    forbidden_markers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.expected_action is not None:
            if not self.label_source or not self.label_source.startswith("candidate-"):
                raise ValueError(
                    "expected_action requires an explicitly versioned candidate label source"
                )
            if self.expected_action not in {"answer", "refuse", "clarify"}:
                raise ValueError("expected_action must be answer, refuse, or clarify")


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{name} must be a mapping")
    return value


def _positive_int(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PolicyError(f"limits.{key} must be a positive integer")
    return value


def _valid_iso_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"{field} must be an ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise PolicyError(f"{field} must be an ISO date") from exc
    return value


def load_policy(path: Path) -> AnalyticsPolicy:
    raw = path.read_bytes()
    parsed = _as_mapping(yaml.safe_load(raw), "policy")
    source = _as_mapping(parsed.get("source_contract"), "source_contract")
    columns = _as_mapping(source.get("columns"), "source_contract.columns")
    normalized_columns = {str(k): str(v).upper() for k, v in columns.items()}
    if normalized_columns != SAFE_SOURCE_TYPES:
        raise PolicyError(
            "source contract must exactly match the code-reviewed safe analytics schema"
        )
    if source.get("table") != "analytics_cases":
        raise PolicyError("only the analytics_cases source table is supported")
    public_raw = source.get("public_columns")
    if not isinstance(public_raw, list) or not public_raw:
        raise PolicyError("source_contract.public_columns must be a non-empty list")
    public = tuple(str(item) for item in public_raw)
    if len(public) != len(set(public)):
        raise PolicyError("public columns contain duplicates")
    if not set(public) <= (set(SAFE_SOURCE_TYPES) - {"case_key"}):
        raise PolicyError("public columns include a non-approved or row-key field")
    if set(public) & SENSITIVE_OR_FREE_TEXT_COLUMNS:
        raise PolicyError("free text or direct identifiers cannot be public")

    patterns = _as_mapping(
        source.get("categorical_patterns"), "source_contract.categorical_patterns"
    )
    if set(patterns) != {"site", "service_code", "age_band"}:
        raise PolicyError("categorical contracts must cover site, service_code, age_band")
    for key, pattern in patterns.items():
        if not isinstance(pattern, str):
            raise PolicyError(f"categorical pattern for {key} must be a string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise PolicyError(f"invalid categorical pattern for {key}") from exc

    raw_limits = _as_mapping(parsed.get("limits"), "limits")
    limits = Limits(
        max_question_chars=_positive_int(raw_limits, "max_question_chars"),
        max_sql_chars=_positive_int(raw_limits, "max_sql_chars"),
        max_rows=_positive_int(raw_limits, "max_rows"),
        max_columns=_positive_int(raw_limits, "max_columns"),
        max_result_cell_chars=_positive_int(raw_limits, "max_result_cell_chars"),
        statement_timeout_ms=_positive_int(raw_limits, "statement_timeout_ms"),
        scan_ceiling_rows=_positive_int(raw_limits, "scan_ceiling_rows"),
        max_table_scans=_positive_int(raw_limits, "max_table_scans"),
        max_concurrent_queries=_positive_int(raw_limits, "max_concurrent_queries"),
    )

    raw_principals = _as_mapping(parsed.get("principals"), "principals")
    principals: dict[str, PrincipalPolicy] = {}
    for principal_id, raw_principal in raw_principals.items():
        if not isinstance(principal_id, str) or not IDENTIFIER_RE.fullmatch(principal_id):
            raise PolicyError(f"invalid principal id: {principal_id!r}")
        item = _as_mapping(raw_principal, f"principals.{principal_id}")
        scope_basis = item.get("scope_basis")
        if not isinstance(scope_basis, str) or not scope_basis:
            raise PolicyError(f"{principal_id} must state its authorization scope basis")
        sites_raw = item.get("sites")
        services_raw = item.get("services")
        principal_columns_raw = item.get("columns")
        if not all(isinstance(value, list) and value for value in (sites_raw, services_raw, principal_columns_raw)):
            raise PolicyError(f"{principal_id} requires non-empty sites/services/columns")
        sites = tuple(str(value) for value in sites_raw)
        services = tuple(str(value) for value in services_raw)
        principal_columns = tuple(str(value) for value in principal_columns_raw)
        if len(set(sites)) != len(sites) or len(set(services)) != len(services):
            raise PolicyError(f"{principal_id} contains duplicate scopes")
        if services.count("*") and services != ("*",):
            raise PolicyError(f"{principal_id} cannot mix wildcard and explicit services")
        if len(set(principal_columns)) != len(principal_columns):
            raise PolicyError(f"{principal_id} contains duplicate columns")
        if not set(principal_columns) <= set(public):
            raise PolicyError(f"{principal_id} includes a non-public column")
        if not {"site", "service_code", "referral_date"} <= set(principal_columns):
            raise PolicyError(
                f"{principal_id} must retain site, service_code, and referral_date "
                "so the physical scope can be audited"
            )
        if not all(re.fullmatch(patterns["site"], value) for value in sites):
            raise PolicyError(f"{principal_id} has malformed site scope")
        if services != ("*",) and not all(
            re.fullmatch(patterns["service_code"], value) for value in services
        ):
            raise PolicyError(f"{principal_id} has malformed service scope")
        rows = _as_mapping(item.get("rows"), f"principals.{principal_id}.rows")
        date_from = _valid_iso_date(
            rows.get("referral_date_from"),
            f"principals.{principal_id}.rows.referral_date_from",
        )
        date_to = _valid_iso_date(
            rows.get("referral_date_to_exclusive"),
            f"principals.{principal_id}.rows.referral_date_to_exclusive",
        )
        if date.fromisoformat(date_from) >= date.fromisoformat(date_to):
            raise PolicyError(f"{principal_id} has an empty date scope")
        principals[principal_id] = PrincipalPolicy(
            principal_id=principal_id,
            scope_basis=scope_basis,
            sites=sites,
            services=services,
            columns=principal_columns,
            referral_date_from=date_from,
            referral_date_to_exclusive=date_to,
        )
    if not principals:
        raise PolicyError("at least one principal is required")
    version = parsed.get("version")
    prompt_version = parsed.get("prompt_version")
    if not isinstance(version, str) or not version:
        raise PolicyError("policy version is required")
    if not isinstance(prompt_version, str) or not prompt_version:
        raise PolicyError("prompt version is required")
    return AnalyticsPolicy(
        version=version,
        prompt_version=prompt_version,
        source_table="analytics_cases",
        source_columns=normalized_columns,
        public_columns=public,
        categorical_patterns={str(k): str(v) for k, v in patterns.items()},
        limits=limits,
        principals=principals,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_empty_source(path: Path) -> Path:
    """Create the code-reviewed empty source used for a key-free clean start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    connection = duckdb.connect(str(temporary))
    try:
        columns = ", ".join(f'"{name}" {kind}' for name, kind in SAFE_SOURCE_TYPES.items())
        connection.execute(f"CREATE TABLE analytics_cases ({columns})")
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _read_only_connection(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(
        str(path),
        read_only=True,
        config={
            "enable_external_access": "false",
            "allow_unsigned_extensions": "false",
        },
    )


def _sql_path_literal(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"


class ScopedDatabaseBuilder:
    """Materialize a physical least-privilege database for each principal."""

    def __init__(self, policy: AnalyticsPolicy, source_path: Path, state_dir: Path):
        self.policy = policy
        self.source_path = source_path.resolve()
        self.state_dir = state_dir.resolve()
        if not self.source_path.is_file():
            raise SourceContractError(f"analytics source does not exist: {self.source_path}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.scope_dir = self.state_dir / "scopes"
        self.scope_dir.mkdir(parents=True, exist_ok=True)
        self.source_sha256 = _file_sha256(self.source_path)
        self._lock = threading.Lock()
        self._validate_source()

    def _validate_source(self) -> None:
        connection = _read_only_connection(self.source_path)
        try:
            rows = connection.execute(
                "SELECT table_name FROM duckdb_tables() "
                "WHERE database_name = current_database() AND schema_name = 'main'"
            ).fetchall()
            if (self.policy.source_table,) not in rows:
                raise SourceContractError("analytics_cases must be a physical base table")
            described = connection.execute(
                "SELECT column_name, column_type FROM (DESCRIBE analytics_cases)"
            ).fetchall()
            actual = {str(name): str(kind).upper() for name, kind in described}
            for name, expected_type in self.policy.source_columns.items():
                if name not in actual:
                    raise SourceContractError(f"analytics source is missing {name}")
                actual_type = actual[name]
                if actual_type != expected_type:
                    raise SourceContractError(
                        f"analytics source {name} is {actual_type}, expected {expected_type}"
                    )
            patterns = self.policy.categorical_patterns
            invalid_site, invalid_service, invalid_age, null_dates = connection.execute(
                """
                SELECT
                  count(*) FILTER (
                    WHERE site IS NULL OR NOT regexp_full_match(site, ?)
                  ),
                  count(*) FILTER (
                    WHERE service_code IS NULL OR NOT regexp_full_match(service_code, ?)
                  ),
                  count(*) FILTER (
                    WHERE age_band IS NOT NULL AND NOT regexp_full_match(age_band, ?)
                  ),
                  count(*) FILTER (WHERE referral_date IS NULL)
                FROM analytics_cases
                """,
                [patterns["site"], patterns["service_code"], patterns["age_band"]],
            ).fetchone()
            if invalid_site or invalid_service or invalid_age or null_dates:
                raise SourceContractError(
                    "analytics source contains malformed categorical text or a null date; "
                    "free text is never passed through"
                )
            self.source_row_count = int(
                connection.execute("SELECT count(*) FROM analytics_cases").fetchone()[0]
            )
        finally:
            connection.close()

    def for_principal(self, principal_id: str) -> ScopeInfo:
        principal = self.policy.principals.get(principal_id)
        if principal is None:
            raise PolicyError("unknown principal")
        identity = json.dumps(
            {
                "source": self.source_sha256,
                "policy": self.policy.sha256,
                "principal": principal_id,
                "scope": principal.__dict__,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        # Short deterministic names avoid Windows' legacy path ceiling while the
        # digest still binds principal, policy, and source.
        target = self.scope_dir / f"scope-{digest[:24]}.duckdb"
        with self._lock:
            if not target.exists():
                self._build(target, principal)
        connection = _read_only_connection(target)
        try:
            row_count = int(connection.execute("SELECT count(*) FROM cases").fetchone()[0])
            columns = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT column_name FROM (DESCRIBE cases)"
                ).fetchall()
            )
            if columns != principal.columns:
                raise SourceContractError("scoped database column contract changed")
            unauthorized_rows = connection.execute(
                "SELECT count(*) FROM cases WHERE "
                f"site NOT IN ({','.join('?' for _ in principal.sites)})",
                list(principal.sites),
            ).fetchone()[0]
            if unauthorized_rows:
                raise SourceContractError("scoped database contains an unauthorized site")
            if principal.services != ("*",):
                unauthorized_services = connection.execute(
                    "SELECT count(*) FROM cases WHERE "
                    f"service_code NOT IN ({','.join('?' for _ in principal.services)})",
                    list(principal.services),
                ).fetchone()[0]
                if unauthorized_services:
                    raise SourceContractError(
                        "scoped database contains an unauthorized service"
                    )
            cross_site_services = connection.execute(
                "SELECT count(*) FROM cases "
                "WHERE NOT starts_with(service_code, site || '-')"
            ).fetchone()[0]
            if cross_site_services:
                raise SourceContractError(
                    "scoped database contains a service outside its row-site namespace"
                )
            unauthorized_dates = connection.execute(
                "SELECT count(*) FROM cases WHERE referral_date < CAST(? AS DATE) "
                "OR referral_date >= CAST(? AS DATE)",
                [principal.referral_date_from, principal.referral_date_to_exclusive],
            ).fetchone()[0]
            if unauthorized_dates:
                raise SourceContractError("scoped database contains an unauthorized row")
        finally:
            connection.close()
        return ScopeInfo(
            path=target,
            principal=principal,
            source_sha256=self.source_sha256,
            scope_sha256=_file_sha256(target),
            policy_sha256=self.policy.sha256,
            row_count=row_count,
        )

    def _build(self, target: Path, principal: PrincipalPolicy) -> None:
        temporary = target.with_name(f".scope-{uuid.uuid4().hex[:12]}.duckdb")
        connection = duckdb.connect(str(temporary))
        attached = False
        try:
            connection.execute(
                f"ATTACH {_sql_path_literal(self.source_path)} AS source_db (READ_ONLY)"
            )
            attached = True
            selected = ", ".join(f'"{name}"' for name in principal.columns)
            conditions = [
                f"site IN ({','.join('?' for _ in principal.sites)})",
                "starts_with(service_code, site || '-')",
                "referral_date >= CAST(? AS DATE)",
                "referral_date < CAST(? AS DATE)",
            ]
            parameters: list[Any] = list(principal.sites) + [
                principal.referral_date_from,
                principal.referral_date_to_exclusive,
            ]
            if principal.services != ("*",):
                conditions.append(
                    f"service_code IN ({','.join('?' for _ in principal.services)})"
                )
                parameters.extend(principal.services)
            connection.execute(
                f"CREATE TABLE cases AS SELECT {selected} "
                "FROM source_db.analytics_cases WHERE " + " AND ".join(conditions),
                parameters,
            )
            connection.execute("DETACH source_db")
            attached = False
            connection.execute("CHECKPOINT")
        finally:
            if attached:
                try:
                    connection.execute("DETACH source_db")
                except duckdb.Error:
                    pass
            connection.close()
        try:
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()


class DeterministicTestProvider:
    """Small, disclosed provider for a runnable service without API keys."""

    provider_name = "deterministic-test-provider"
    provider_version = "1.0.0"

    _forbidden_question_terms = (
        "clinical note",
        "clinical_notes",
        "patient id",
        "patient name",
        "date of birth",
        "clinician id",
        "system prompt",
        "ignore previous",
        "drop table",
        "attach database",
    )

    def generate(
        self,
        question: str,
        *,
        schema: tuple[str, ...],
        principal: PrincipalPolicy,
    ) -> ModelDecision:
        normalized = " ".join(question.strip().split())
        lowered = normalized.casefold()
        if not normalized:
            return ModelDecision("clarify", "empty_question", clarification="What metric do you need?")
        if any(term in lowered for term in self._forbidden_question_terms):
            return ModelDecision("refuse", "unauthorized_or_adversarial_request")

        for requested_site in re.findall(r"\bsite\s+([a-z0-9_-]+)", lowered):
            if requested_site.upper() not in principal.sites:
                return ModelDecision("refuse", "site_outside_caller_scope")
        explicit_services = {
            value.upper()
            for value in re.findall(r"\b([a-z]-[a-z0-9][a-z0-9_-]*)\b", lowered)
        }
        if principal.services != ("*",) and not explicit_services <= set(principal.services):
            return ModelDecision("refuse", "service_outside_caller_scope")

        requested_columns: set[str] = set()
        term_columns = {
            "age": "age_band",
            "cancel": "cancellation_proxy",
            "readiness": "readiness_days",
            "cost": "total_cost_cad",
        }
        for term, column in term_columns.items():
            if term in lowered:
                requested_columns.add(column)
        missing = requested_columns - set(schema)
        if missing:
            return ModelDecision("refuse", "column_outside_caller_scope")

        group_columns: list[str] = []
        if "by site" in lowered:
            if "site" not in schema:
                return ModelDecision("refuse", "column_outside_caller_scope")
            group_columns.append("site")
        if "by service" in lowered:
            if "service_code" not in schema:
                return ModelDecision("refuse", "column_outside_caller_scope")
            group_columns.append("service_code")

        if "cancellation" in lowered or "cancelled" in lowered:
            if "cancellation_proxy" not in schema:
                return ModelDecision("refuse", "column_outside_caller_scope")
            expression = (
                "AVG(CAST(cancellation_proxy AS DOUBLE)) AS cancellation_rate"
            )
        elif "readiness" in lowered:
            if "readiness_days" not in schema:
                return ModelDecision("refuse", "column_outside_caller_scope")
            expression = "AVG(readiness_days) AS mean_readiness_days"
        elif "cost" in lowered:
            if "total_cost_cad" not in schema:
                return ModelDecision("refuse", "column_outside_caller_scope")
            expression = "AVG(total_cost_cad) AS mean_total_cost_cad"
        elif any(term in lowered for term in ("how many", "count", "number of", "cases", "referrals")):
            expression = "COUNT(*) AS case_count"
        elif any(term in lowered for term in ("performance", "doing", "status", "trend")):
            return ModelDecision(
                "clarify",
                "ambiguous_metric",
                clarification=(
                    "Which authorized metric—case count, cancellation rate, or readiness—"
                    "and which time grouping do you mean?"
                ),
            )
        else:
            return ModelDecision("refuse", "not_answerable_from_approved_schema")

        if group_columns:
            group = ", ".join(group_columns)
            sql = f"SELECT {group}, {expression} FROM cases GROUP BY {group} ORDER BY {group}"
        else:
            sql = f"SELECT {expression} FROM cases"
        return ModelDecision("query", "approved_template", sql=sql)


class StaticSqlProvider:
    """Test/evaluation provider that makes generated-SQL attacks reproducible."""

    provider_name = "static-sql-test-provider"
    provider_version = "1.0.0"

    def __init__(self, sql: str):
        self.sql = sql

    def generate(
        self,
        question: str,
        *,
        schema: tuple[str, ...],
        principal: PrincipalPolicy,
    ) -> ModelDecision:
        del question, schema, principal
        return ModelDecision("query", "static_test_sql", sql=self.sql)


def load_provider(specification: str | None) -> ModelProvider:
    if specification in (None, "", "deterministic"):
        return DeterministicTestProvider()
    if ":" not in specification:
        raise AnalyticsServiceError(
            "model provider must be 'deterministic' or an operator-configured module:factory"
        )
    module_name, factory_name = specification.split(":", 1)
    if not module_name or not factory_name:
        raise AnalyticsServiceError("invalid model provider specification")
    factory = getattr(importlib.import_module(module_name), factory_name)
    provider = factory()
    if not callable(getattr(provider, "generate", None)):
        raise AnalyticsServiceError("configured provider has no generate method")
    if not getattr(provider, "provider_name", None) or not getattr(
        provider, "provider_version", None
    ):
        raise AnalyticsServiceError("configured provider must expose name and version")
    return provider


class IdentityResolver:
    """Resolve a principal without accepting a caller-provided scope.

    ``fixed-development`` ignores identity/scope headers and always returns one
    operator-selected least-privilege principal.  ``bearer-sha256`` compares a
    presented bearer token to hashes loaded from a runtime secret mount.  The
    latter is required in production; raw tokens are never stored or logged.
    """

    def __init__(
        self,
        policy: AnalyticsPolicy,
        *,
        mode: str,
        fixed_principal: str | None = None,
        credential_hash_path: Path | None = None,
    ):
        self.policy = policy
        self.mode = mode
        self.fixed_principal = fixed_principal
        self._token_hashes: dict[str, str] = {}
        if mode == "fixed-development":
            if fixed_principal not in policy.principals:
                raise PolicyError("fixed development principal is not in policy")
        elif mode == "bearer-sha256":
            if credential_hash_path is None or not credential_hash_path.is_file():
                raise PolicyError("bearer identity mode requires a credential-hash file")
            try:
                loaded = json.loads(credential_hash_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PolicyError("credential-hash file is not valid JSON") from exc
            if not isinstance(loaded, dict) or not loaded:
                raise PolicyError("credential-hash file must map principals to SHA-256 hashes")
            for principal_id, token_hash in loaded.items():
                if principal_id not in policy.principals:
                    raise PolicyError("credential file names an unknown principal")
                if not isinstance(token_hash, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", token_hash
                ):
                    raise PolicyError("credential file contains a malformed SHA-256 hash")
                self._token_hashes[principal_id] = token_hash
        else:
            raise PolicyError("identity mode must be fixed-development or bearer-sha256")

    @property
    def production_capable(self) -> bool:
        return self.mode == "bearer-sha256"

    def resolve(self, authorization_header: str | None) -> str | None:
        if self.mode == "fixed-development":
            return self.fixed_principal
        prefix = "Bearer "
        if not authorization_header or not authorization_header.startswith(prefix):
            return None
        raw_token = authorization_header[len(prefix) :]
        if not raw_token or len(raw_token) > 4096:
            return None
        candidate_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        matched: str | None = None
        # Check every configured hash to avoid principal-dependent early exit.
        for principal_id, expected_hash in self._token_hashes.items():
            if hmac.compare_digest(candidate_hash, expected_hash):
                matched = principal_id
        return matched


def resolve_service_commit(
    explicit: str | None = None,
    *,
    commit_file: Path = Path("config/service-implementation-commit.txt"),
) -> str:
    candidates = [explicit, os.environ.get("BARNABUS_SERVICE_COMMIT")]
    if commit_file.is_file():
        candidates.append(commit_file.read_text(encoding="utf-8").strip())
    for candidate in candidates:
        if candidate and re.fullmatch(r"[0-9a-f]{40}", candidate):
            return candidate
    return "unavailable"


@dataclass(frozen=True)
class QueryPlan:
    estimated_rows: int
    table_scans: int
    estimated_scan_work: int


_DENIED_SQL_WORDS = re.compile(
    r"\b(attach|detach|copy|install|load|pragma|call|create|alter|drop|delete|"
    r"update|insert|merge|export|import|vacuum|checkpoint|set|reset|use)\b",
    re.IGNORECASE,
)
_DENIED_SQL_FUNCTIONS = re.compile(
    r"\b(read_csv|read_csv_auto|read_parquet|parquet_scan|csv_scan|sqlite_scan|"
    r"postgres_scan|mysql_scan|read_text|read_blob|glob|query|query_table|"
    r"duckdb_[a-z0-9_]*|pragma_[a-z0-9_]*|range|generate_series|repeat|lpad|rpad)\s*\(",
    re.IGNORECASE,
)


class SqlGuard:
    def __init__(self, limits: Limits):
        self.limits = limits

    def inspect(self, sql: str, scope: ScopeInfo) -> QueryPlan:
        if not isinstance(sql, str) or not sql.strip():
            raise SqlRejected("provider returned empty SQL")
        if len(sql) > self.limits.max_sql_chars:
            raise SqlRejected("generated SQL exceeds the length ceiling")
        normalized = unicodedata.normalize("NFKC", sql)
        if normalized != sql or any(ord(char) > 126 for char in sql):
            raise SqlRejected("generated SQL contains non-ASCII or compatibility characters")
        if any(ord(char) < 32 and char not in "\r\n\t" for char in sql):
            raise SqlRejected("generated SQL contains control characters")
        if ";" in sql or "--" in sql or "/*" in sql or "*/" in sql or "#" in sql:
            raise SqlRejected("multiple statements and SQL comments are forbidden")
        if _DENIED_SQL_WORDS.search(sql) or _DENIED_SQL_FUNCTIONS.search(sql):
            raise SqlRejected("generated SQL requests a forbidden operation or data source")
        if re.search(r"\binformation_schema\b|\bsqlite_master\b|\bpg_catalog\b", sql, re.I):
            raise SqlRejected("catalog access is forbidden")

        connection = _read_only_connection(scope.path)
        try:
            try:
                statements = connection.extract_statements(sql)
            except duckdb.Error as exc:
                raise SqlRejected("generated SQL does not parse") from exc
            if len(statements) != 1 or statements[0].type.name != "SELECT":
                raise SqlRejected("exactly one SELECT statement is required")
            try:
                tables = connection.get_table_names(sql)
            except duckdb.Error as exc:
                raise SqlRejected("generated SQL cannot be resolved") from exc
            if tables != {"cases"}:
                raise SqlRejected("queries must read only the physical scoped cases table")
            try:
                explanation = connection.execute(f"EXPLAIN {sql}").fetchall()
            except duckdb.Error as exc:
                raise SqlRejected("generated SQL references unavailable data or is invalid") from exc
        finally:
            connection.close()
        plan_text = "\n".join(str(value) for row in explanation for value in row)
        table_scans = plan_text.count("Table: cases")
        if table_scans <= 0:
            # The parser proved the only base table is cases; count a scan even if
            # the optimizer folds an empty or constant aggregate.
            table_scans = 1
        if table_scans > self.limits.max_table_scans:
            raise SqlRejected("query exceeds the table-scan ceiling")
        multipliers = {"": 1, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
        estimates = [
            int(float(value.replace(",", "")) * multipliers[(suffix or "").casefold()])
            for value, suffix in re.findall(
                r"~([0-9][0-9,]*(?:\.[0-9]+)?)\s*(thousand|million|billion)? rows",
                plan_text,
                flags=re.IGNORECASE,
            )
        ]
        estimated_rows = max(estimates, default=scope.row_count)
        estimated_work = max(scope.row_count * table_scans, estimated_rows)
        cross_joins = len(re.findall(r"\bCROSS\s+JOIN\b", sql, flags=re.IGNORECASE))
        if cross_joins:
            # A cross product's result cardinality is an unavoidable lower bound
            # on work even when the optimizer abbreviates its display estimate.
            estimated_work = max(
                estimated_work, scope.row_count ** (cross_joins + 1)
            )
        if estimated_work > self.limits.scan_ceiling_rows:
            raise SqlRejected("query exceeds the estimated scan/cost ceiling")
        return QueryPlan(
            estimated_rows=estimated_rows,
            table_scans=table_scans,
            estimated_scan_work=estimated_work,
        )


def _json_scalar(value: Any, max_chars: int) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        if len(value) > max_chars:
            raise ValueError("result cell exceeds the size ceiling")
        # Source string columns are contract-constrained.  This is still a data
        # cell, never a prompt or executable instruction.
        return value
    rendered = str(value)
    if len(rendered) > max_chars:
        raise ValueError("result cell exceeds the size ceiling")
    return rendered


def _query_worker(
    database_path: str,
    sql: str,
    max_rows: int,
    max_columns: int,
    max_cell_chars: int,
    result_queue: multiprocessing.Queue[Any],
) -> None:
    try:
        connection = duckdb.connect(
            database_path,
            read_only=True,
            config={
                "enable_external_access": "false",
                "allow_unsigned_extensions": "false",
                "threads": "1",
            },
        )
        try:
            wrapped = f"SELECT * FROM ({sql}) AS __authorized_result LIMIT {max_rows + 1}"
            started = time.perf_counter()
            cursor = connection.execute(wrapped)
            columns = [str(item[0]) for item in cursor.description]
            if len(columns) > max_columns:
                raise ValueError("result exceeds the column ceiling")
            if any(not OUTPUT_COLUMN_RE.fullmatch(column) for column in columns):
                raise ValueError("result contains an unsafe column label")
            fetched = cursor.fetchmany(max_rows + 1)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            truncated = len(fetched) > max_rows
            rows = [
                [_json_scalar(value, max_cell_chars) for value in row]
                for row in fetched[:max_rows]
            ]
            result_queue.put(
                {
                    "ok": True,
                    "columns": columns,
                    "rows": rows,
                    "truncated": truncated,
                    "execution_ms": elapsed_ms,
                }
            )
        finally:
            connection.close()
    except BaseException as exc:  # child must return a bounded, non-sensitive error
        result_queue.put({"ok": False, "error_type": type(exc).__name__})


def execute_with_timeout(
    scope: ScopeInfo, sql: str, limits: Limits
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue[Any] = context.Queue(maxsize=1)
    process = context.Process(
        target=_query_worker,
        args=(
            str(scope.path),
            sql,
            limits.max_rows,
            limits.max_columns,
            limits.max_result_cell_chars,
            result_queue,
        ),
    )
    process.start()
    process.join(limits.statement_timeout_ms / 1000.0)
    if process.is_alive():
        process.terminate()
        process.join(2.0)
        result_queue.close()
        raise QueryTimedOut("query exceeded the statement timeout")
    try:
        payload = result_queue.get(timeout=1.0)
    except queue.Empty as exc:
        raise AnalyticsServiceError("query worker returned no result") from exc
    finally:
        result_queue.close()
    if not payload.get("ok"):
        raise SqlRejected(
            f"read-only execution rejected the query ({payload.get('error_type', 'error')})"
        )
    return payload


class StructuredAuditLogger:
    def __init__(self, stream: Any | None = None):
        self.stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "level": "INFO",
            "service": "analytics-assistant",
            "event": event,
            **fields,
        }
        with self._lock:
            self.stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            self.stream.flush()


class AnalyticsAssistant:
    def __init__(
        self,
        policy: AnalyticsPolicy,
        source_path: Path,
        state_dir: Path,
        *,
        provider: ModelProvider | None = None,
        commit_sha: str = "unavailable",
        deployment_mode: str = "development",
        identity_mode: str = "fixed-development",
        data_artifact_version: str = "unavailable",
        audit_logger: StructuredAuditLogger | None = None,
    ):
        self.policy = policy
        self.provider = provider or DeterministicTestProvider()
        self.commit_sha = commit_sha
        if deployment_mode not in {"development", "production"}:
            raise PolicyError("deployment mode must be development or production")
        self.deployment_mode = deployment_mode
        self.identity_mode = identity_mode
        self.data_artifact_version = data_artifact_version
        self.audit = audit_logger or StructuredAuditLogger()
        self.builder = ScopedDatabaseBuilder(policy, source_path, state_dir)
        self.guard = SqlGuard(policy.limits)

    def health(self) -> dict[str, Any]:
        deterministic = isinstance(self.provider, DeterministicTestProvider)
        readiness_reasons: list[str] = []
        if self.builder.source_row_count == 0:
            readiness_reasons.append("empty_analytics_source")
        if self.commit_sha == "unavailable":
            readiness_reasons.append("implementation_commit_unavailable")
        if self.data_artifact_version == "unavailable":
            readiness_reasons.append("data_artifact_version_unavailable")
        if self.deployment_mode == "production" and self.identity_mode != "bearer-sha256":
            readiness_reasons.append("production_identity_not_cryptographically_verified")
        return {
            "status": "ok",
            "ready": not readiness_reasons,
            "readiness_reasons": readiness_reasons,
            "deployment_mode": self.deployment_mode,
            "identity_mode": self.identity_mode,
            "service": "analytics-assistant",
            "policy_version": self.policy.version,
            "policy_sha256": self.policy.sha256,
            "provider": self.provider.provider_name,
            "provider_version": self.provider.provider_version,
            "deterministic_test_provider": deterministic,
            "external_model_evaluation_available": not deterministic,
            "external_model_limitation": (
                "No external model is configured; responses use disclosed deterministic templates."
                if deterministic
                else None
            ),
            "commit": self.commit_sha,
            "commit_provenance_complete": self.commit_sha != "unavailable",
            "data_artifact_version": self.data_artifact_version,
            "source_rows": self.builder.source_row_count,
        }

    def _provenance(self, scope: ScopeInfo | None) -> dict[str, Any]:
        return {
            "data_sha256": scope.source_sha256 if scope else self.builder.source_sha256,
            "data_artifact_version": self.data_artifact_version,
            "scope_database_sha256": scope.scope_sha256 if scope else None,
            "model_provider": self.provider.provider_name,
            "model_version": self.provider.provider_version,
            "prompt_version": self.policy.prompt_version,
            "policy_version": self.policy.version,
            "policy_sha256": self.policy.sha256,
            "commit": self.commit_sha,
            "service_implementation_commit": self.commit_sha,
        }

    def query(
        self, principal_id: str, question: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        request_id = request_id or uuid.uuid4().hex
        started = time.perf_counter()
        question_hash = hashlib.sha256(
            question.encode("utf-8", errors="replace") if isinstance(question, str) else b""
        ).hexdigest()
        if principal_id not in self.policy.principals:
            result = {
                "request_id": request_id,
                "action": "refuse",
                "reason": "unknown_principal",
                "provenance": self._provenance(None),
            }
            self.audit.emit(
                "request_refused",
                request_id=request_id,
                principal_id=str(principal_id),
                question_sha256=question_hash,
                reason="unknown_principal",
            )
            return result
        if not isinstance(question, str) or len(question) > self.policy.limits.max_question_chars:
            reason = "invalid_or_oversized_question"
            result = {
                "request_id": request_id,
                "action": "refuse",
                "reason": reason,
                "provenance": self._provenance(None),
            }
            self.audit.emit(
                "request_refused",
                request_id=request_id,
                principal_id=principal_id,
                question_sha256=question_hash,
                reason=reason,
            )
            return result

        principal = self.policy.principals[principal_id]
        scope = self.builder.for_principal(principal_id)
        decision = self.provider.generate(
            question, schema=principal.columns, principal=principal
        )
        if decision.action in {"refuse", "clarify"}:
            result = {
                "request_id": request_id,
                "action": decision.action,
                "reason": decision.reason,
                "clarification": decision.clarification,
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "provenance": self._provenance(scope),
            }
            self.audit.emit(
                "request_refused" if decision.action == "refuse" else "clarification_requested",
                request_id=request_id,
                principal_id=principal_id,
                question_sha256=question_hash,
                reason=decision.reason,
            )
            return result
        if decision.action != "query" or decision.sql is None:
            decision = ModelDecision("refuse", "invalid_provider_decision")
            return {
                "request_id": request_id,
                "action": decision.action,
                "reason": decision.reason,
                "provenance": self._provenance(scope),
            }

        sql_hash = hashlib.sha256(decision.sql.encode("ascii", errors="replace")).hexdigest()
        try:
            plan = self.guard.inspect(decision.sql, scope)
            execution = execute_with_timeout(scope, decision.sql, self.policy.limits)
        except QueryTimedOut:
            result = {
                "request_id": request_id,
                "action": "refuse",
                "reason": "statement_timeout",
                "execution": {"attempted": True, "succeeded": False},
                "sql_sha256": sql_hash,
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "provenance": self._provenance(scope),
            }
            self.audit.emit(
                "query_refused",
                request_id=request_id,
                principal_id=principal_id,
                question_sha256=question_hash,
                sql_sha256=sql_hash,
                reason="statement_timeout",
            )
            return result
        except (SqlRejected, duckdb.Error) as exc:
            reason = "generated_sql_rejected"
            result = {
                "request_id": request_id,
                "action": "refuse",
                "reason": reason,
                "execution": {"attempted": False, "succeeded": False},
                "sql_sha256": sql_hash,
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "provenance": self._provenance(scope),
            }
            self.audit.emit(
                "query_refused",
                request_id=request_id,
                principal_id=principal_id,
                question_sha256=question_hash,
                sql_sha256=sql_hash,
                reason=reason,
            )
            return result

        latency_ms = (time.perf_counter() - started) * 1000.0
        result = {
            "request_id": request_id,
            "action": "answer",
            "message": "Authorized query completed.",
            "data": {
                "columns": execution["columns"],
                "rows": execution["rows"],
                "truncated": execution["truncated"],
            },
            "execution": {
                "attempted": True,
                "succeeded": True,
                "execution_ms": execution["execution_ms"],
                "estimated_rows": plan.estimated_rows,
                "table_scans": plan.table_scans,
                "estimated_scan_work": plan.estimated_scan_work,
                "row_limit": self.policy.limits.max_rows,
                "scan_ceiling_rows": self.policy.limits.scan_ceiling_rows,
                "statement_timeout_ms": self.policy.limits.statement_timeout_ms,
                "read_only": True,
                "external_access": False,
            },
            "sql_sha256": sql_hash,
            "latency_ms": latency_ms,
            "security": {
                "authorization_boundary": "physical_per_principal_database",
                "database_values_sent_to_model": False,
                "model_used_for_answer_rendering": False,
            },
            "provenance": self._provenance(scope),
        }
        self.audit.emit(
            "query_completed",
            request_id=request_id,
            principal_id=principal_id,
            question_sha256=question_hash,
            sql_sha256=sql_hash,
            latency_ms=round(latency_ms, 3),
            estimated_scan_work=plan.estimated_scan_work,
            returned_rows=len(execution["rows"]),
            truncated=execution["truncated"],
        )
        return result


def _nearest_rank_percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int((percentile * len(ordered) + 0.999999999)))
    return float(ordered[min(rank, len(ordered)) - 1])


def evaluate_assistant(
    assistant: AnalyticsAssistant,
    cases: Sequence[EvaluationCase],
    *,
    evaluation_version: str = "candidate-assistant-eval-v1",
) -> dict[str, Any]:
    """Return separate execution, refusal, cost, latency, and auth metrics.

    Unlabelled supplied questions contribute to execution/cost/latency only.
    Refusal precision and recall are calculated solely for cases whose labels
    explicitly identify a versioned ``candidate-*`` source.
    """
    latencies: list[float] = []
    estimated_work: list[int] = []
    execution_attempts = 0
    execution_successes = 0
    labelled = 0
    true_positive = false_positive = false_negative = 0
    violations: list[dict[str, str]] = []
    case_results: list[dict[str, Any]] = []
    for item in cases:
        result = assistant.query(
            item.principal_id, item.question, request_id=f"eval-{item.case_id}"
        )
        latency = float(result.get("latency_ms", 0.0))
        latencies.append(latency)
        execution = result.get("execution") or {}
        if execution.get("attempted"):
            execution_attempts += 1
        if execution.get("succeeded"):
            execution_successes += 1
        if execution.get("estimated_scan_work") is not None:
            estimated_work.append(int(execution["estimated_scan_work"]))
        canonical = json.dumps(result, sort_keys=True).casefold()
        for marker in item.forbidden_markers:
            if marker.casefold() in canonical:
                violations.append({"case_id": item.case_id, "marker_sha256": hashlib.sha256(marker.encode()).hexdigest()})
        if item.expected_action is not None:
            labelled += 1
            predicted_refusal = result.get("action") == "refuse"
            expected_refusal = item.expected_action == "refuse"
            if predicted_refusal and expected_refusal:
                true_positive += 1
            elif predicted_refusal and not expected_refusal:
                false_positive += 1
            elif not predicted_refusal and expected_refusal:
                false_negative += 1
        case_results.append(
            {
                "case_id": item.case_id,
                "action": result.get("action"),
                "reason": result.get("reason"),
                "label_source": item.label_source,
                "expected_action": item.expected_action,
                "candidate_rationale": item.rationale,
                "latency_ms": latency,
                "estimated_scan_work": execution.get("estimated_scan_work"),
            }
        )
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    refusal_precision = (
        true_positive / precision_denominator if precision_denominator else None
    )
    refusal_recall = true_positive / recall_denominator if recall_denominator else None
    return {
        "evaluation_version": evaluation_version,
        "labels": {
            "labelled_candidate_cases": labelled,
            "unlabelled_cases": len(cases) - labelled,
            "supplied_question_labels_invented": False,
        },
        "execution": {
            "attempts": execution_attempts,
            "successes": execution_successes,
            "success_rate": (
                execution_successes / execution_attempts if execution_attempts else None
            ),
        },
        "refusal": {
            "evaluated_only_on_versioned_candidate_labels": True,
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": refusal_precision,
            "recall": refusal_recall,
            "estimable": labelled > 0,
        },
        "cost": {
            "queries_with_estimates": len(estimated_work),
            "total_estimated_scan_work": sum(estimated_work),
            "max_estimated_scan_work": max(estimated_work, default=None),
        },
        "latency": {
            "count": len(latencies),
            "mean_ms": sum(latencies) / len(latencies) if latencies else None,
            "p99_ms": _nearest_rank_percentile(latencies, 0.99),
        },
        "authorization": {
            "violations": len(violations),
            "acceptable_violations": 0,
            "passed": not violations,
            "details": violations,
        },
        "cases": case_results,
    }


def load_unlabelled_supplied_questions(path: Path, principal_id: str) -> list[EvaluationCase]:
    """Load questions without manufacturing answers or refusal labels."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "question" not in reader.fieldnames:
            raise AnalyticsServiceError("supplied question set has no question column")
        identifier = "question_id" if "question_id" in reader.fieldnames else None
        cases = []
        for index, row in enumerate(reader, start=1):
            cases.append(
                EvaluationCase(
                    case_id=str(row.get(identifier) or index) if identifier else str(index),
                    principal_id=principal_id,
                    question=str(row.get("question") or ""),
                    expected_action=None,
                    label_source=None,
                    rationale="supplied question; no reference label supplied",
                )
            )
    return cases


def load_candidate_evaluation_cases(
    policy_path: Path, principal_id: str
) -> list[EvaluationCase]:
    """Load explicitly candidate-created classifications and rationales."""
    parsed = _as_mapping(
        yaml.safe_load(policy_path.read_bytes()), "analytics policy"
    )
    section = _as_mapping(parsed.get("candidate_evaluation"), "candidate_evaluation")
    version = section.get("version")
    if not isinstance(version, str) or not version.startswith("candidate-"):
        raise PolicyError("candidate evaluation version must start with candidate-")
    if section.get("supplied_question_labels_available") is not False:
        raise PolicyError("candidate evaluation must not claim supplied labels")
    raw_cases = section.get("cases")
    if not isinstance(raw_cases, list):
        raise PolicyError("candidate evaluation cases must be a list")
    cases: list[EvaluationCase] = []
    for item in raw_cases:
        mapping = _as_mapping(item, "candidate evaluation case")
        rationale = mapping.get("rationale")
        if not isinstance(rationale, str) or not rationale:
            raise PolicyError("every candidate classification requires a rationale")
        cases.append(
            EvaluationCase(
                case_id=str(mapping.get("case_id", "")),
                principal_id=principal_id,
                question=str(mapping.get("question", "")),
                expected_action=str(mapping.get("expected_action", "")),
                label_source=version,
                rationale=rationale,
            )
        )
    return cases


def _handler_class(
    assistant: AnalyticsAssistant, identity_resolver: IdentityResolver
) -> type[BaseHTTPRequestHandler]:
    query_slots = threading.BoundedSemaphore(assistant.policy.limits.max_concurrent_queries)

    class Handler(BaseHTTPRequestHandler):
        server_version = "BarnabusAnalytics/1"

        def log_message(self, format: str, *args: Any) -> None:
            # Access logging is structured and deliberately excludes request bodies.
            assistant.audit.emit(
                "http_access",
                client=self.client_address[0],
                method=self.command,
                path=self.path,
                status=args[1] if len(args) > 1 else None,
            )

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(HTTPStatus.OK, assistant.health())
            elif self.path == "/readyz":
                health = assistant.health()
                self._send(
                    HTTPStatus.OK if health["ready"] else HTTPStatus.SERVICE_UNAVAILABLE,
                    health,
                )
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/v1/query":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not query_slots.acquire(blocking=False):
                self._send(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    {"error": "concurrency_budget_exhausted", "retryable": True},
                )
                return
            try:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
                    return
                if length <= 0 or length > 65536:
                    self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid_body_size"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                    return
                if not isinstance(payload, dict) or set(payload) != {"question"}:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "body_must_contain_only_question"},
                    )
                    return
                principal_id = identity_resolver.resolve(
                    self.headers.get("Authorization")
                )
                if principal_id is None:
                    assistant.audit.emit(
                        "authentication_refused",
                        request_id=self.headers.get("X-Request-Id") or uuid.uuid4().hex,
                        reason="unverified_identity",
                    )
                    self._send(HTTPStatus.UNAUTHORIZED, {"error": "unverified_identity"})
                    return
                request_id = self.headers.get("X-Request-Id") or uuid.uuid4().hex
                result = assistant.query(principal_id, payload["question"], request_id=request_id)
                if result["action"] == "refuse" and result.get("reason") in {
                    "unknown_principal",
                    "site_outside_caller_scope",
                    "service_outside_caller_scope",
                    "column_outside_caller_scope",
                    "unauthorized_or_adversarial_request",
                }:
                    status = HTTPStatus.FORBIDDEN
                elif result["action"] in {"refuse", "clarify"}:
                    status = HTTPStatus.UNPROCESSABLE_ENTITY
                else:
                    status = HTTPStatus.OK
                self._send(status, result)
            finally:
                query_slots.release()

    return Handler


def serve(
    assistant: AnalyticsAssistant,
    identity_resolver: IdentityResolver,
    host: str,
    port: int,
) -> None:
    server = create_server(assistant, identity_resolver, host, port)
    assistant.audit.emit("service_started", host=host, port=port, **assistant.health())
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        assistant.audit.emit("service_stopped")


def create_server(
    assistant: AnalyticsAssistant,
    identity_resolver: IdentityResolver,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """Construct a bound server for production startup or HTTP tests."""
    return ThreadingHTTPServer(
        (host, port), _handler_class(assistant, identity_resolver)
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=Path("config/analytics-policy-v1.yaml"))
    parser.add_argument("--state-dir", type=Path, default=Path("/state/analytics"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--provider", default=os.environ.get("BARNABUS_MODEL_PROVIDER"))
    parser.add_argument("--commit")
    parser.add_argument(
        "--deployment-mode",
        choices=("development", "production"),
        default=os.environ.get("BARNABUS_DEPLOYMENT_MODE", "development"),
    )
    parser.add_argument(
        "--identity-mode",
        choices=("fixed-development", "bearer-sha256"),
        default=os.environ.get("BARNABUS_IDENTITY_MODE", "fixed-development"),
    )
    parser.add_argument(
        "--fixed-principal",
        default=os.environ.get("BARNABUS_DEV_PRINCIPAL", "site-a-card-reader"),
    )
    parser.add_argument(
        "--credential-hashes",
        type=Path,
        default=(
            Path(os.environ["BARNABUS_CREDENTIAL_HASH_FILE"])
            if os.environ.get("BARNABUS_CREDENTIAL_HASH_FILE")
            else None
        ),
    )
    parser.add_argument(
        "--data-artifact-version",
        default=os.environ.get("BARNABUS_DATA_ARTIFACT_VERSION", "unavailable"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = load_policy(args.policy.resolve())
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    source = args.source or (
        Path(os.environ["BARNABUS_ANALYTICS_SOURCE"])
        if os.environ.get("BARNABUS_ANALYTICS_SOURCE")
        else state_dir / "empty-analytics-source.duckdb"
    )
    if args.source is None and not os.environ.get("BARNABUS_ANALYTICS_SOURCE"):
        create_empty_source(source)
    provider = load_provider(args.provider)
    commit_sha = resolve_service_commit(args.commit)
    identity_resolver = IdentityResolver(
        policy,
        mode=args.identity_mode,
        fixed_principal=args.fixed_principal,
        credential_hash_path=args.credential_hashes,
    )
    assistant = AnalyticsAssistant(
        policy,
        source,
        state_dir,
        provider=provider,
        commit_sha=commit_sha,
        deployment_mode=args.deployment_mode,
        identity_mode=identity_resolver.mode,
        data_artifact_version=args.data_artifact_version,
    )
    if args.deployment_mode == "production":
        if args.source is None and not os.environ.get("BARNABUS_ANALYTICS_SOURCE"):
            raise AnalyticsServiceError(
                "production requires an explicit canonical analytics source"
            )
        if not identity_resolver.production_capable:
            raise AnalyticsServiceError(
                "production requires bearer-sha256 identity verification"
            )
        if not assistant.health()["ready"]:
            raise AnalyticsServiceError(
                "production readiness failed: "
                + ",".join(assistant.health()["readiness_reasons"])
            )
    serve(assistant, identity_resolver, args.host, args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by container startup
    raise SystemExit(main())
