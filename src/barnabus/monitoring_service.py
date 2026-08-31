"""Event-sourced evaluation and monitoring HTTP service.

Ingested JSON is data only: it is validated, parameter-bound into SQLite, and is
never evaluated as code or interpolated into a query.  Every accepted revision is
immutable.  Metrics and alerts are deterministic projections of the accepted
event-time history, so a correction or backfill can retract an alert explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse

import yaml


LOGGER = logging.getLogger("barnabus.monitoring")
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,199}$")
SAFE_SEGMENT = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_FIELDS = {
    "event_id",
    "case_id",
    "revision",
    "correction_of_event_id",
    "scored_at",
    "arrived_at",
    "site",
    "service_code",
    "score",
    "threshold",
    "outcome",
    "outcome_observed_at",
    "numeric_features",
    "data_version",
    "model_version",
    "prompt_version",
    "policy_version",
    "commit_sha",
}


class MonitoringError(ValueError):
    """Base class for safe client-visible monitoring errors."""


class ConflictError(MonitoringError):
    """An immutable identifier or revision was reused inconsistently."""


def _json_log(event: str, **fields: Any) -> None:
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": "INFO",
        "event": event,
        **fields,
    }
    LOGGER.info(json.dumps(record, sort_keys=True, separators=(",", ":")))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise MonitoringError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MonitoringError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MonitoringError(f"{field} must include a UTC offset")
    return parsed.astimezone(UTC)


def _token(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SAFE_TOKEN.fullmatch(value):
        raise MonitoringError(f"{field} has an invalid format")
    return value


def _commit_token(value: Any, field: str) -> str:
    token = _token(value, field).lower()
    if not FULL_COMMIT.fullmatch(token):
        raise MonitoringError(f"{field} must be a full 40-hex commit")
    return token


def _segment(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MonitoringError(f"{field} must be a string")
    normalized = value.strip().upper()
    if not SAFE_SEGMENT.fullmatch(normalized):
        raise MonitoringError(f"{field} has an invalid format")
    return normalized


def _bounded_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise MonitoringError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MonitoringError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise MonitoringError(f"{field} must be in [0, 1]")
    return number


def _week_start(value: datetime) -> str:
    day = value.date() - timedelta(days=value.weekday())
    return day.isoformat()


@dataclass(frozen=True)
class CaseRevision:
    event_id: str
    case_id: str
    revision: int
    correction_of_event_id: str | None
    scored_at: str
    arrived_at: str
    site: str
    service_code: str
    score: float
    threshold: float
    outcome: int | None
    outcome_observed_at: str | None
    numeric_features: dict[str, float]
    data_version: str
    model_version: str
    prompt_version: str
    policy_version: str
    commit_sha: str

    @classmethod
    def parse(
        cls, payload: Any, *, allowed_numeric_features: set[str]
    ) -> "CaseRevision":
        if not isinstance(payload, dict):
            raise MonitoringError("request body must be a JSON object")
        unexpected = sorted(set(payload) - ALLOWED_FIELDS)
        if unexpected:
            raise MonitoringError(f"unexpected fields: {', '.join(unexpected)}")
        required = ALLOWED_FIELDS - {
            "correction_of_event_id",
            "outcome",
            "outcome_observed_at",
            "numeric_features",
        }
        missing = sorted(name for name in required if name not in payload)
        if missing:
            raise MonitoringError(f"missing fields: {', '.join(missing)}")

        event_id = _token(payload["event_id"], "event_id")
        case_id = _token(payload["case_id"], "case_id")
        revision = payload["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise MonitoringError("revision must be a positive integer")
        correction = payload.get("correction_of_event_id")
        if correction is not None:
            correction = _token(correction, "correction_of_event_id")

        scored = _parse_timestamp(payload["scored_at"], "scored_at")
        arrived = _parse_timestamp(payload["arrived_at"], "arrived_at")
        if arrived < scored:
            raise MonitoringError("arrived_at cannot precede scored_at")
        outcome = payload.get("outcome")
        if outcome is not None and (isinstance(outcome, bool) or outcome not in (0, 1)):
            raise MonitoringError("outcome must be 0, 1, or null")
        outcome_at_raw = payload.get("outcome_observed_at")
        outcome_at: datetime | None = None
        if outcome_at_raw is not None:
            outcome_at = _parse_timestamp(outcome_at_raw, "outcome_observed_at")
            if outcome_at < scored:
                raise MonitoringError("outcome_observed_at cannot precede scored_at")
        if outcome is None and outcome_at is not None:
            raise MonitoringError("outcome_observed_at requires a non-null outcome")
        if outcome is not None and outcome_at is None:
            raise MonitoringError("a non-null outcome requires outcome_observed_at")
        if outcome_at is not None and outcome_at > arrived:
            raise MonitoringError("outcome_observed_at cannot follow arrived_at")

        raw_features = payload.get("numeric_features", {})
        if not isinstance(raw_features, dict):
            raise MonitoringError("numeric_features must be an object")
        unknown_features = sorted(set(raw_features) - allowed_numeric_features)
        if unknown_features:
            raise MonitoringError(
                f"numeric_features not allowlisted: {', '.join(unknown_features)}"
            )
        features: dict[str, float] = {}
        for name, value in sorted(raw_features.items()):
            features[name] = _bounded_float(value, f"numeric_features.{name}")

        return cls(
            event_id=event_id,
            case_id=case_id,
            revision=revision,
            correction_of_event_id=correction,
            scored_at=scored.isoformat(),
            arrived_at=arrived.isoformat(),
            site=_segment(payload["site"], "site"),
            service_code=_segment(payload["service_code"], "service_code"),
            score=_bounded_float(payload["score"], "score"),
            threshold=_bounded_float(payload["threshold"], "threshold"),
            outcome=outcome,
            outcome_observed_at=outcome_at.isoformat() if outcome_at else None,
            numeric_features=features,
            data_version=_token(payload["data_version"], "data_version"),
            model_version=_token(payload["model_version"], "model_version"),
            prompt_version=_token(payload["prompt_version"], "prompt_version"),
            policy_version=_token(payload["policy_version"], "policy_version"),
            commit_sha=_commit_token(payload["commit_sha"], "commit_sha"),
        )

    def payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "case_id": self.case_id,
            "revision": self.revision,
            "correction_of_event_id": self.correction_of_event_id,
            "scored_at": self.scored_at,
            "arrived_at": self.arrived_at,
            "site": self.site,
            "service_code": self.service_code,
            "score": self.score,
            "threshold": self.threshold,
            "outcome": self.outcome,
            "outcome_observed_at": self.outcome_observed_at,
            "numeric_features": self.numeric_features,
            "data_version": self.data_version,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "policy_version": self.policy_version,
            "commit_sha": self.commit_sha,
        }

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.payload()
        payload.pop("event_id")
        return payload


class MonitoringStore:
    """Append-only source events plus replaceable deterministic projections."""

    def __init__(
        self,
        database_path: Path | str,
        config: Mapping[str, Any],
        *,
        service_commit: str | None,
        development_mode: bool = False,
    ) -> None:
        if service_commit is not None and not SAFE_TOKEN.fullmatch(service_commit):
            raise MonitoringError("service_commit has an invalid format")
        self.database_path = str(database_path)
        self.config = dict(config)
        self.config_sha256 = _sha256(self.config)
        self.service_commit = service_commit or "UNAVAILABLE"
        self.development_mode = development_mode
        self.provenance_ready = bool(
            service_commit is not None and FULL_COMMIT.fullmatch(service_commit)
        )
        ingestion = self.config.get("ingestion", {})
        self.allowed_numeric_features = set(
            ingestion.get("allowed_numeric_features", [])
        )
        self.maturation_days = int(ingestion.get("maturation_days", 9))
        self._lock = threading.RLock()
        path = Path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.database_path, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    @classmethod
    def from_config_path(
        cls,
        database_path: Path | str,
        config_path: Path | str,
        *,
        service_commit: str | None,
        development_mode: bool = False,
    ) -> "MonitoringStore":
        with Path(config_path).open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
        if not isinstance(config, dict) or config.get("schema_version") != 1:
            raise MonitoringError("monitoring config schema_version must be 1")
        return cls(
            database_path,
            config,
            service_commit=service_commit,
            development_mode=development_mode,
        )

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS scored_case_events (
                event_id TEXT PRIMARY KEY,
                event_hash TEXT NOT NULL,
                semantic_hash TEXT NOT NULL,
                case_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                correction_of_event_id TEXT,
                scored_at TEXT NOT NULL,
                arrived_at TEXT NOT NULL,
                period_start TEXT NOT NULL,
                site TEXT NOT NULL,
                service_code TEXT NOT NULL,
                score REAL NOT NULL,
                threshold_value REAL NOT NULL,
                outcome INTEGER,
                outcome_observed_at TEXT,
                numeric_features_json TEXT NOT NULL,
                data_version TEXT NOT NULL,
                model_version TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                is_late INTEGER NOT NULL,
                is_backfill INTEGER NOT NULL,
                accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
                disposition TEXT NOT NULL,
                FOREIGN KEY(correction_of_event_id) REFERENCES scored_case_events(event_id)
            );
            CREATE INDEX IF NOT EXISTS ix_case_revision
                ON scored_case_events(case_id, revision);
            CREATE INDEX IF NOT EXISTS ix_case_period
                ON scored_case_events(site, service_code, period_start);

            CREATE TABLE IF NOT EXISTS metric_snapshots (
                metric_id TEXT PRIMARY KEY,
                monitor_id TEXT NOT NULL,
                catalog_id TEXT,
                domain TEXT NOT NULL,
                site TEXT NOT NULL,
                service_code TEXT NOT NULL,
                period_start TEXT NOT NULL,
                value REAL,
                sample_size INTEGER NOT NULL,
                baseline_size INTEGER NOT NULL,
                status TEXT NOT NULL,
                threshold_value REAL,
                candidate INTEGER NOT NULL,
                severity REAL NOT NULL,
                alert_status TEXT NOT NULL,
                metric_hash TEXT NOT NULL,
                provenance_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_metric_period
                ON metric_snapshots(period_start, monitor_id, site, service_code);
            CREATE TABLE IF NOT EXISTS metric_inputs (
                metric_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                PRIMARY KEY(metric_id, event_id),
                FOREIGN KEY(metric_id) REFERENCES metric_snapshots(metric_id),
                FOREIGN KEY(event_id) REFERENCES scored_case_events(event_id)
            );

            CREATE TABLE IF NOT EXISTS alerts_current (
                alert_id TEXT PRIMARY KEY,
                metric_id TEXT NOT NULL,
                metric_hash TEXT NOT NULL,
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                last_event_type TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alert_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id TEXT NOT NULL,
                metric_id TEXT NOT NULL,
                metric_hash TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('fired', 'retracted')),
                reason TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            """
        )

    def ingest(self, payload: Any) -> dict[str, Any]:
        if not self.provenance_ready and not self.development_mode:
            raise MonitoringError(
                "service implementation commit unavailable; provenance is not ready"
            )
        revision = CaseRevision.parse(
            payload, allowed_numeric_features=self.allowed_numeric_features
        )
        event_hash = _sha256(revision.payload())
        semantic_hash = _sha256(revision.semantic_payload())
        scored_dt = datetime.fromisoformat(revision.scored_at)
        arrived_dt = datetime.fromisoformat(revision.arrived_at)
        is_late = (arrived_dt - scored_dt) > timedelta(days=self.maturation_days)

        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self.connection.execute(
                    "SELECT event_hash FROM scored_case_events WHERE event_id = ?",
                    (revision.event_id,),
                ).fetchone()
                if existing is not None:
                    if existing["event_hash"] == event_hash:
                        self.connection.execute("COMMIT")
                        _json_log(
                            "scored_case_replayed",
                            event_id=revision.event_id,
                            event_hash=event_hash,
                            disposition="replayed",
                        )
                        return {
                            "status": "replayed",
                            "event_id": revision.event_id,
                            "event_hash": event_hash,
                            "retractions": [],
                        }
                    _json_log(
                        "hard_stop",
                        monitor_id="DQ-02",
                        reason="event_id_reused_with_different_content",
                        event_id=revision.event_id,
                        event_hash=event_hash,
                    )
                    raise ConflictError("event_id was already used with different content")

                same_revision = self.connection.execute(
                    """SELECT event_id, semantic_hash FROM scored_case_events
                       WHERE case_id = ? AND revision = ? AND accepted = 1""",
                    (revision.case_id, revision.revision),
                ).fetchone()
                if same_revision is not None:
                    if same_revision["semantic_hash"] == semantic_hash:
                        _json_log(
                            "hard_stop",
                            monitor_id="DQ-02",
                            reason="case_revision_replayed_under_different_event_id",
                            event_id=revision.event_id,
                            event_hash=event_hash,
                        )
                        raise ConflictError(
                            "case revision was replayed under a different event_id"
                        )
                    _json_log(
                        "hard_stop",
                        monitor_id="DQ-02",
                        reason="case_revision_conflict",
                        event_id=revision.event_id,
                        event_hash=event_hash,
                    )
                    raise ConflictError("case revision conflicts with accepted history")

                active = self.connection.execute(
                    """SELECT event_id, revision FROM scored_case_events
                       WHERE case_id = ? AND accepted = 1
                       ORDER BY revision DESC, event_id DESC LIMIT 1""",
                    (revision.case_id,),
                ).fetchone()
                if active is None:
                    if revision.revision != 1 or revision.correction_of_event_id is not None:
                        raise ConflictError("a new case must begin at revision 1")
                else:
                    if revision.revision != int(active["revision"]) + 1:
                        raise ConflictError("a correction must be the next case revision")
                    if revision.correction_of_event_id != active["event_id"]:
                        raise ConflictError(
                            "a correction must reference the currently active event"
                        )

                maximum_scored = self.connection.execute(
                    "SELECT MAX(scored_at) AS maximum_scored FROM scored_case_events WHERE accepted = 1"
                ).fetchone()["maximum_scored"]
                is_backfill = maximum_scored is not None and revision.scored_at < maximum_scored
                self.connection.execute(
                    """INSERT INTO scored_case_events (
                         event_id, event_hash, semantic_hash, case_id, revision,
                         correction_of_event_id, scored_at, arrived_at, period_start,
                         site, service_code, score, threshold_value, outcome,
                         outcome_observed_at, numeric_features_json, data_version,
                         model_version, prompt_version, policy_version, commit_sha,
                         is_late, is_backfill, accepted, disposition
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 ?, ?, ?, ?, ?, 1, 'accepted')""",
                    (
                        revision.event_id,
                        event_hash,
                        semantic_hash,
                        revision.case_id,
                        revision.revision,
                        revision.correction_of_event_id,
                        revision.scored_at,
                        revision.arrived_at,
                        _week_start(scored_dt),
                        revision.site,
                        revision.service_code,
                        revision.score,
                        revision.threshold,
                        revision.outcome,
                        revision.outcome_observed_at,
                        _canonical_json(revision.numeric_features),
                        revision.data_version,
                        revision.model_version,
                        revision.prompt_version,
                        revision.policy_version,
                        revision.commit_sha,
                        int(is_late),
                        int(is_backfill),
                    ),
                )
                changes = self._recompute()
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

        _json_log(
            "scored_case_ingested",
            event_id=revision.event_id,
            event_hash=event_hash,
            disposition="accepted",
            is_late=is_late,
            is_backfill=is_backfill,
            alerts_fired=len(changes["fired"]),
            alerts_retracted=len(changes["retracted"]),
        )
        return {
            "status": "accepted",
            "event_id": revision.event_id,
            "event_hash": event_hash,
            "is_late": is_late,
            "lateness_days": round((arrived_dt - scored_dt).total_seconds() / 86400, 6),
            "is_backfill": is_backfill,
            "alerts_fired": changes["fired"],
            "retractions": changes["retracted"],
        }

    def _active_rows(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """WITH ranked AS (
                   SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY case_id ORDER BY revision DESC, event_id DESC
                   ) AS row_number
                   FROM scored_case_events WHERE accepted = 1
               )
               SELECT * FROM ranked WHERE row_number = 1
               ORDER BY scored_at, case_id, event_id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def _recompute(self) -> dict[str, list[str]]:
        rows = self._active_rows()
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[(row["site"], row["service_code"], row["period_start"])].append(row)

        metrics: list[dict[str, Any]] = []
        keys_by_segment: dict[tuple[str, str], list[str]] = defaultdict(list)
        for site, service, period in grouped:
            keys_by_segment[(site, service)].append(period)
        for segment in keys_by_segment:
            keys_by_segment[segment] = sorted(set(keys_by_segment[segment]))

        metric_config = self.config.get("metrics", {})
        outcome_maturation_days = int(
            self.config.get("ingestion", {}).get("outcome_maturation_days", 90)
        )
        minimum_cases = int(metric_config.get("minimum_segment_cases", 20))
        minimum_baseline_weeks = int(metric_config.get("minimum_baseline_weeks", 8))
        baseline_weeks = int(metric_config.get("baseline_weeks", 8))
        bins = [float(value) for value in metric_config.get("psi_bins", [])]
        monitor_config = metric_config.get("monitors", {})
        watermark = max(
            (datetime.fromisoformat(row["arrived_at"]) for row in rows),
            default=datetime(1970, 1, 1, tzinfo=UTC),
        )

        for (site, service), periods in sorted(keys_by_segment.items()):
            for period_index, period in enumerate(periods):
                current = grouped[(site, service, period)]
                reference_periods = periods[max(0, period_index - baseline_weeks) : period_index]
                baseline = [
                    row
                    for prior in reference_periods
                    for row in grouped[(site, service, prior)]
                ]
                baseline_ready = len(reference_periods) >= minimum_baseline_weeks
                def mature_outcome(row: Mapping[str, Any]) -> bool:
                    return (
                        row["outcome"] is not None
                        and row["outcome_observed_at"] is not None
                        and datetime.fromisoformat(row["outcome_observed_at"]) <= watermark
                        and datetime.fromisoformat(row["scored_at"])
                        + timedelta(days=outcome_maturation_days)
                        <= watermark
                    )

                current_outcomes = [row for row in current if mature_outcome(row)]
                baseline_outcomes = [row for row in baseline if mature_outcome(row)]

                score_status = (
                    "ok"
                    if len(current) >= minimum_cases and baseline_ready and baseline
                    else "insufficient_data"
                )
                score_psi = (
                    self._psi(
                        [float(row["score"]) for row in current],
                        [float(row["score"]) for row in baseline],
                        bins,
                    )
                    if score_status == "ok"
                    else None
                )
                metrics.append(
                    self._metric(
                        "score_psi",
                        site,
                        service,
                        period,
                        score_psi,
                        len(current),
                        len(baseline),
                        score_status,
                        current + baseline,
                        monitor_config,
                    )
                )

                calibration_minimum = int(
                    monitor_config.get("calibration_gap", {}).get(
                        "minimum_cases", minimum_cases
                    )
                )
                outcome_minimum = int(
                    monitor_config.get("mature_outcome_rate", {}).get(
                        "minimum_cases", minimum_cases
                    )
                )
                outcome_status = (
                    "ok"
                    if len(current_outcomes) >= calibration_minimum
                    else "insufficient_data"
                )
                brier = (
                    sum(
                        (float(row["score"]) - int(row["outcome"])) ** 2
                        for row in current_outcomes
                    )
                    / len(current_outcomes)
                    if outcome_status == "ok"
                    else None
                )
                calibration_gap = (
                    abs(
                        sum(float(row["score"]) for row in current_outcomes)
                        / len(current_outcomes)
                        - sum(int(row["outcome"]) for row in current_outcomes)
                        / len(current_outcomes)
                    )
                    if outcome_status == "ok"
                    else None
                )
                metrics.append(
                    self._metric(
                        "brier_score",
                        site,
                        service,
                        period,
                        brier,
                        len(current_outcomes),
                        0,
                        outcome_status,
                        current_outcomes,
                        monitor_config,
                    )
                )
                metrics.append(
                    self._metric(
                        "calibration_gap",
                        site,
                        service,
                        period,
                        calibration_gap,
                        len(current_outcomes),
                        0,
                        outcome_status,
                        current_outcomes,
                        monitor_config,
                    )
                )
                mature_rate = (
                    sum(int(row["outcome"]) for row in current_outcomes)
                    / len(current_outcomes)
                    if outcome_status == "ok"
                    else None
                )
                metrics.append(
                    self._metric(
                        "mature_outcome_rate",
                        site,
                        service,
                        period,
                        mature_rate,
                        len(current_outcomes),
                        0,
                        (
                            "ok"
                            if len(current_outcomes) >= outcome_minimum
                            else "insufficient_data"
                        ),
                        current_outcomes,
                        monitor_config,
                    )
                )

                delta_status = (
                    "ok"
                    if len(current_outcomes) >= outcome_minimum
                    and len(baseline_outcomes) >= outcome_minimum
                    and baseline_ready
                    else "insufficient_data"
                )
                outcome_delta = (
                    sum(int(row["outcome"]) for row in current_outcomes)
                    / len(current_outcomes)
                    - sum(int(row["outcome"]) for row in baseline_outcomes)
                    / len(baseline_outcomes)
                    if delta_status == "ok"
                    else None
                )
                metrics.append(
                    self._metric(
                        "mature_outcome_rate_delta",
                        site,
                        service,
                        period,
                        outcome_delta,
                        len(current_outcomes),
                        len(baseline_outcomes),
                        delta_status,
                        current_outcomes + baseline_outcomes,
                        monitor_config,
                    )
                )

        self._apply_persistence(metrics)
        self._apply_alert_budget(metrics)
        for metric in metrics:
            metric["metric_hash"] = _sha256(
                {
                    key: value
                    for key, value in metric.items()
                    if key not in {"input_event_ids", "metric_hash"}
                }
            )
        changes = self._replace_projection(metrics, rows)
        return changes

    @staticmethod
    def _psi(current: list[float], reference: list[float], bins: list[float]) -> float:
        if len(bins) < 2 or bins[0] != 0.0 or bins[-1] != 1.0:
            raise MonitoringError("psi_bins must start at 0 and end at 1")

        def counts(values: list[float]) -> list[int]:
            result = [0] * (len(bins) - 1)
            for value in values:
                index = len(result) - 1
                for candidate in range(len(result)):
                    if bins[candidate] <= value < bins[candidate + 1]:
                        index = candidate
                        break
                result[index] += 1
            return result

        current_counts = counts(current)
        reference_counts = counts(reference)
        smoothing = 0.5
        current_denominator = len(current) + smoothing * len(current_counts)
        reference_denominator = len(reference) + smoothing * len(reference_counts)
        total = 0.0
        for current_count, reference_count in zip(current_counts, reference_counts):
            current_fraction = (current_count + smoothing) / current_denominator
            reference_fraction = (reference_count + smoothing) / reference_denominator
            total += (current_fraction - reference_fraction) * math.log(
                current_fraction / reference_fraction
            )
        return total

    def _metric(
        self,
        monitor_id: str,
        site: str,
        service: str,
        period: str,
        value: float | None,
        sample_size: int,
        baseline_size: int,
        status: str,
        input_rows: Sequence[Mapping[str, Any]],
        monitor_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        settings = monitor_config.get(monitor_id, {})
        threshold = float(settings.get("threshold", 0.0))
        alerting = bool(settings.get("alerting", False))
        mode = settings.get("mode", "operational")
        if status == "ok" and mode != "operational":
            status = str(mode)
        candidate = status == "ok" and alerting and value is not None and value >= threshold
        severity = float(value / threshold) if candidate and threshold > 0 else 0.0
        input_rows = sorted(input_rows, key=lambda row: row["event_id"])
        provenance_inputs = [
            {
                "event_id": row["event_id"],
                "event_hash": row["event_hash"],
                "case_revision": int(row["revision"]),
                "data_version": row["data_version"],
                "model_version": row["model_version"],
                "prompt_version": row["prompt_version"],
                "policy_version": row["policy_version"],
                "commit_sha": row["commit_sha"],
            }
            for row in input_rows
        ]
        provenance = {
            "service_commit": self.service_commit,
            "monitor_config_sha256": self.config_sha256,
            "input_version_status": self.config.get("provenance", {}).get(
                "input_version_status", "client_claimed_not_registry_verified"
            ),
            "input_set_sha256": _sha256(provenance_inputs),
            "inputs": provenance_inputs,
        }
        metric_id = _sha256(
            {
                "monitor_id": monitor_id,
                "site": site,
                "service_code": service,
                "period_start": period,
            }
        )
        metric = {
            "metric_id": metric_id,
            "monitor_id": monitor_id,
            "domain": settings.get("domain", "unspecified"),
            "catalog_id": settings.get("catalog_id"),
            "site": site,
            "service_code": service,
            "period_start": period,
            "value": value,
            "sample_size": sample_size,
            "baseline_size": baseline_size,
            "status": status,
            "threshold": threshold,
            "candidate": candidate,
            "severity": severity,
            "alert_status": "pending" if candidate else "no_alert",
            "provenance": provenance,
            "input_event_ids": [row["event_id"] for row in input_rows],
        }
        metric["metric_hash"] = _sha256(
            {key: value for key, value in metric.items() if key != "input_event_ids"}
        )
        return metric

    def _apply_persistence(self, metrics: list[dict[str, Any]]) -> None:
        needed = int(self.config.get("alerts", {}).get("persistence_periods", 2))
        by_series: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for metric in metrics:
            by_series[
                (metric["monitor_id"], metric["site"], metric["service_code"])
            ].append(metric)
        for series in by_series.values():
            series.sort(key=lambda metric: metric["period_start"])
            streak = 0
            previous_date = None
            for metric in series:
                current_date = datetime.fromisoformat(metric["period_start"]).date()
                consecutive = (
                    previous_date is not None
                    and (current_date - previous_date).days == 7
                )
                if metric["candidate"]:
                    streak = streak + 1 if consecutive else 1
                    if streak < needed:
                        metric["candidate"] = False
                        metric["alert_status"] = "awaiting_persistence"
                else:
                    streak = 0
                previous_date = current_date

    def _apply_alert_budget(self, metrics: list[dict[str, Any]]) -> None:
        budget = int(
            self.config.get("alerts", {}).get("statistical_budget_per_period", 3)
        )
        by_period: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for metric in metrics:
            if metric["candidate"]:
                by_period[metric["period_start"]].append(metric)
        for candidates in by_period.values():
            bundles: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for metric in candidates:
                bundles[(metric["site"], metric["service_code"])].append(metric)
            ordered_bundles = sorted(
                bundles.items(),
                key=lambda item: (
                    -max(metric["severity"] for metric in item[1]),
                    item[0][0],
                    item[0][1],
                ),
            )
            for bundle_index, (_, bundle) in enumerate(ordered_bundles):
                bundle.sort(key=lambda metric: (-metric["severity"], metric["monitor_id"]))
                if bundle_index >= budget:
                    for metric in bundle:
                        metric["alert_status"] = "budget_suppressed"
                    continue
                bundle[0]["alert_status"] = "fired"
                for metric in bundle[1:]:
                    metric["alert_status"] = "bundled_into_incident"

    def _replace_projection(
        self, metrics: list[dict[str, Any]], active_rows: list[dict[str, Any]]
    ) -> dict[str, list[str]]:
        previous_active = {
            row["alert_id"]: dict(row)
            for row in self.connection.execute(
                "SELECT * FROM alerts_current WHERE active = 1"
            ).fetchall()
        }
        self.connection.execute("DELETE FROM metric_inputs")
        self.connection.execute("DELETE FROM metric_snapshots")
        desired: dict[str, dict[str, Any]] = {}
        for metric in metrics:
            self.connection.execute(
                    """INSERT INTO metric_snapshots (
                       metric_id, monitor_id, catalog_id, domain, site, service_code, period_start,
                       value, sample_size, baseline_size, status, threshold_value,
                       candidate, severity, alert_status, metric_hash, provenance_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    metric["metric_id"],
                    metric["monitor_id"],
                    metric["catalog_id"],
                    metric["domain"],
                    metric["site"],
                    metric["service_code"],
                    metric["period_start"],
                    metric["value"],
                    metric["sample_size"],
                    metric["baseline_size"],
                    metric["status"],
                    metric["threshold"],
                    int(metric["candidate"]),
                    metric["severity"],
                    metric["alert_status"],
                    metric["metric_hash"],
                    _canonical_json(metric["provenance"]),
                ),
            )
            for event_id in sorted(set(metric["input_event_ids"])):
                self.connection.execute(
                    "INSERT INTO metric_inputs(metric_id, event_id) VALUES (?, ?)",
                    (metric["metric_id"], event_id),
                )
            if metric["alert_status"] == "fired":
                alert_id = _sha256(
                    {
                        "monitor_id": metric["monitor_id"],
                        "site": metric["site"],
                        "service_code": metric["service_code"],
                        "period_start": metric["period_start"],
                    }
                )
                desired[alert_id] = metric

        replay_time = max(
            (row["arrived_at"] for row in active_rows),
            default="1970-01-01T00:00:00+00:00",
        )
        fired: list[str] = []
        retracted: list[str] = []
        for alert_id, previous in sorted(previous_active.items()):
            if alert_id not in desired:
                self.connection.execute(
                    """INSERT INTO alert_events(
                           alert_id, metric_id, metric_hash, event_type, reason, recorded_at
                       ) VALUES (?, ?, ?, 'retracted', ?, ?)""",
                    (
                        alert_id,
                        previous["metric_id"],
                        previous["metric_hash"],
                        self.config.get("alerts", {}).get(
                            "corrected_history_retraction_reason",
                            "corrected_history_or_budget_reallocation",
                        ),
                        replay_time,
                    ),
                )
                self.connection.execute(
                    """UPDATE alerts_current
                       SET active = 0, last_event_type = 'retracted'
                       WHERE alert_id = ?""",
                    (alert_id,),
                )
                retracted.append(alert_id)

        for alert_id, metric in sorted(desired.items()):
            previous = previous_active.get(alert_id)
            if previous is None:
                self.connection.execute(
                    """INSERT INTO alert_events(
                           alert_id, metric_id, metric_hash, event_type, reason, recorded_at
                       ) VALUES (?, ?, ?, 'fired', 'threshold_persistence_and_budget', ?)""",
                    (alert_id, metric["metric_id"], metric["metric_hash"], replay_time),
                )
                self.connection.execute(
                    """INSERT INTO alerts_current(
                           alert_id, metric_id, metric_hash, active, last_event_type
                       ) VALUES (?, ?, ?, 1, 'fired')
                       ON CONFLICT(alert_id) DO UPDATE SET
                           metric_id=excluded.metric_id,
                           metric_hash=excluded.metric_hash,
                           active=1,
                           last_event_type='fired'""",
                    (alert_id, metric["metric_id"], metric["metric_hash"]),
                )
                fired.append(alert_id)
            elif previous["metric_hash"] != metric["metric_hash"]:
                self.connection.execute(
                    """INSERT INTO alert_events(
                           alert_id, metric_id, metric_hash, event_type, reason, recorded_at
                       ) VALUES (?, ?, ?, 'retracted', 'corrected_history_superseded', ?)""",
                    (
                        alert_id,
                        previous["metric_id"],
                        previous["metric_hash"],
                        replay_time,
                    ),
                )
                self.connection.execute(
                    """INSERT INTO alert_events(
                           alert_id, metric_id, metric_hash, event_type, reason, recorded_at
                       ) VALUES (?, ?, ?, 'fired', 'corrected_history_recomputed', ?)""",
                    (alert_id, metric["metric_id"], metric["metric_hash"], replay_time),
                )
                self.connection.execute(
                    """UPDATE alerts_current SET metric_hash = ?, metric_id = ?
                       WHERE alert_id = ?""",
                    (metric["metric_hash"], metric["metric_id"], alert_id),
                )
                retracted.append(alert_id)
                fired.append(alert_id)
        return {"fired": fired, "retracted": retracted}

    def metrics(self, filters: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        filters = filters or {}
        allowed = {"monitor_id", "site", "service_code", "period_start"}
        if set(filters) - allowed:
            raise MonitoringError("unsupported metric filter")
        clauses: list[str] = []
        values: list[str] = []
        for name in sorted(filters):
            value = filters[name]
            if name in {"site", "service_code"}:
                value = _segment(value, name)
            clauses.append(f"{name} = ?")
            values.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM metric_snapshots"
                + where
                + " ORDER BY period_start, monitor_id, site, service_code",
                values,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["candidate"] = bool(item["candidate"])
            item["threshold"] = item.pop("threshold_value")
            item["provenance"] = json.loads(item.pop("provenance_json"))
            result.append(item)
        return result

    def alerts(self, *, history: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if history:
                rows = self.connection.execute(
                    "SELECT * FROM alert_events ORDER BY sequence"
                ).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM alerts_current WHERE active = 1 ORDER BY alert_id"
                ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self, filters: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Return the complete programmatic current projection for verification."""
        return {
            "health": self.health(),
            "metrics": self.metrics(filters),
            "active_alerts": self.alerts(),
            "alert_history": self.alerts(history=True),
            "monitor_catalog_coverage": self.catalog(),
            "locked_statistical_policy_coverage": dict(
                self.config.get("locked_statistical_policy_coverage", {})
            ),
        }

    def catalog(self) -> dict[str, str]:
        return dict(self.config.get("monitor_catalog_coverage", {}))

    def health(self) -> dict[str, Any]:
        with self._lock:
            self.connection.execute("SELECT 1").fetchone()
            event_count = self.connection.execute(
                "SELECT COUNT(*) FROM scored_case_events WHERE accepted = 1"
            ).fetchone()[0]
        return {
            "status": (
                "ok"
                if self.provenance_ready
                else "development_only"
                if self.development_mode
                else "not_ready"
            ),
            "service": "evaluation-monitoring",
            "schema_version": 1,
            "accepted_events": event_count,
            "service_commit": self.service_commit,
            "monitor_config_sha256": self.config_sha256,
            "commit_verified": self.provenance_ready,
            "development_mode": self.development_mode,
        }


class MonitoringHandler(BaseHTTPRequestHandler):
    """Small stdlib JSON API; handler never logs request bodies."""

    server: "MonitoringHTTPServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send(self, status: int, body: Mapping[str, Any] | list[Any]) -> None:
        encoded = _canonical_json(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _request_id(self) -> str:
        supplied = self.headers.get("X-Request-ID")
        if supplied and SAFE_TOKEN.fullmatch(supplied):
            return supplied
        return hashlib.sha256(os.urandom(32)).hexdigest()[:24]

    def do_GET(self) -> None:  # noqa: N802
        request_id = self._request_id()
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                response = self.server.store.health()
                status_code = (
                    HTTPStatus.OK
                    if response["status"] in {"ok", "development_only"}
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
            elif parsed.path == "/v1/metrics":
                query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
                response = {"metrics": self.server.store.metrics(query)}
            elif parsed.path == "/v1/alerts":
                response = {"alerts": self.server.store.alerts()}
            elif parsed.path == "/v1/alerts/history":
                response = {"alerts": self.server.store.alerts(history=True)}
            elif parsed.path == "/v1/snapshot":
                response = self.server.store.snapshot()
            elif parsed.path == "/v1/catalog":
                response = {"monitor_catalog_coverage": self.server.store.catalog()}
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found", "request_id": request_id})
                return
            self._send(status_code if parsed.path == "/healthz" else HTTPStatus.OK, response)
            _json_log(
                "http_request",
                request_id=request_id,
                method="GET",
                path=parsed.path,
                status=int(status_code if parsed.path == "/healthz" else HTTPStatus.OK),
            )
        except MonitoringError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc), "request_id": request_id})

    def do_POST(self) -> None:  # noqa: N802
        request_id = self._request_id()
        parsed = urlparse(self.path)
        if parsed.path != "/v1/scored-cases":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not_found", "request_id": request_id})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > self.server.maximum_request_bytes:
                raise MonitoringError("invalid request size")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type != "application/json":
                raise MonitoringError("Content-Type must be application/json")
            raw = self.rfile.read(content_length)
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MonitoringError("request body must be valid JSON") from exc
            response = self.server.store.ingest(payload)
            self._send(HTTPStatus.OK, {**response, "request_id": request_id})
        except ConflictError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc), "request_id": request_id})
        except MonitoringError as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc), "request_id": request_id})
        except Exception:
            _json_log("http_error", request_id=request_id, path=parsed.path, error="internal_error")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "request_id": request_id})


class MonitoringHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: MonitoringStore,
        maximum_request_bytes: int,
    ) -> None:
        super().__init__(address, MonitoringHandler)
        self.store = store
        self.maximum_request_bytes = maximum_request_bytes


def build_server(
    host: str,
    port: int,
    database_path: Path | str,
    config_path: Path | str,
    service_commit: str | None,
    *,
    development_mode: bool = False,
) -> MonitoringHTTPServer:
    store = MonitoringStore.from_config_path(
        database_path,
        config_path,
        service_commit=service_commit,
        development_mode=development_mode,
    )
    maximum = int(store.config.get("ingestion", {}).get("maximum_request_bytes", 65536))
    return MonitoringHTTPServer((host, port), store, maximum)


def resolve_service_commit(
    explicit: str | None, commit_file: Path | str | None
) -> str | None:
    """Resolve provenance without invoking Git or fabricating an identifier."""
    if explicit:
        return explicit.strip()
    environment_value = os.environ.get("BARNABUS_SERVICE_COMMIT")
    if environment_value:
        return environment_value.strip()
    if commit_file is not None:
        path = Path(commit_file)
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Endpoints: GET /healthz, GET /v1/metrics, GET /v1/alerts, "
            "GET /v1/alerts/history, GET /v1/catalog, GET /v1/snapshot, "
            "POST /v1/scored-cases. "
            "Start with: python -m barnabus.monitoring_service serve [options]."
        ),
    )
    parser.add_argument("command", nargs="?", choices=["serve"], default="serve")
    parser.add_argument("--host", default=os.environ.get("BARNABUS_MONITORING_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BARNABUS_MONITORING_PORT", "8081")))
    parser.add_argument("--state", default=os.environ.get("BARNABUS_MONITORING_STATE", "/state/monitoring.sqlite"))
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "BARNABUS_MONITORING_CONFIG", "/app/config/monitoring-service-v1.yaml"
        ),
    )
    parser.add_argument("--service-commit")
    parser.add_argument(
        "--service-commit-file",
        default=os.environ.get(
            "BARNABUS_SERVICE_COMMIT_FILE",
            "/app/config/service-implementation-commit.txt",
        ),
    )
    parser.add_argument(
        "--development-mode",
        action="store_true",
        default=os.environ.get("BARNABUS_SERVICE_DEV_MODE", "false").lower()
        in {"1", "true", "yes"},
        help="allow explicitly labeled startup without a verified 40-hex commit",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    service_commit = resolve_service_commit(
        args.service_commit, args.service_commit_file
    )
    server = build_server(
        args.host,
        args.port,
        args.state,
        args.config,
        service_commit,
        development_mode=args.development_mode,
    )
    _json_log(
        "service_started",
        service="evaluation-monitoring",
        host=args.host,
        port=args.port,
        service_commit=service_commit or "UNAVAILABLE",
        provenance_ready=server.store.provenance_ready,
        development_mode=args.development_mode,
        deterministic_projection=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        server.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
