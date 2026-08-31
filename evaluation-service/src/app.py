import os
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field, asdict
from enum import Enum

import yaml
import pandas as pd
import numpy as np
from scipy import stats
from flask import Flask, request, jsonify
from pythonjsonlogger import jsonlogger

# Structured logging setup
class CustomJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record['timestamp'] = datetime.utcnow().isoformat()
        log_record['service'] = 'evaluation-service'

logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
handler.setFormatter(CustomJsonFormatter())
logger.addHandler(handler)
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

app = Flask(__name__)

# Configuration from environment
CONFIG = {
    'alert_budget': int(os.environ.get('ALERT_BUDGET', '10')),
    'alert_window_hours': int(os.environ.get('ALERT_WINDOW_HOURS', '24')),
    'drift_threshold': float(os.environ.get('DRIFT_THRESHOLD', '0.1')),
    'calibration_threshold': float(os.environ.get('CALIBRATION_THRESHOLD', '0.05')),
    'outcome_threshold': float(os.environ.get('OUTCOME_THRESHOLD', '0.1')),
    'baseline_window_days': int(os.environ.get('BASELINE_WINDOW_DAYS', '30')),
    'p99_latency_budget_ms': int(os.environ.get('P99_LATENCY_BUDGET_MS', '500')),
    'statement_timeout_ms': int(os.environ.get('STATEMENT_TIMEOUT_MS', '15000')),
}

# Git commit hash (set at build time or use placeholder)
GIT_COMMIT = os.environ.get('GIT_COMMIT', 'dev-uncommitted')

class AlertPriority(Enum):
    CRITICAL = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4

@dataclass
class Case:
    case_id: str
    score: float
    outcome: Optional[str] = None
    feature_vector: Optional[Dict[str, float]] = None
    scored_ts: Optional[str] = None
    data_version: str = "unknown"
    model_version: str = "unknown"
    threshold_used: float = 0.62

@dataclass
class Provenance:
    data_version: str
    data_row_count: int
    data_time_range: str
    data_filters: Dict[str, Any]
    model_version: str
    model_threshold: float
    prompt_template: str
    policy_alert_rules: Dict[str, Any]
    policy_budget: int
    commit_hash: str

@dataclass
class Alert:
    alert_id: str
    metric_type: str
    metric_name: str
    value: float
    threshold: float
    priority: AlertPriority
    fired_ts: str
    case_ids: List[str]
    provenance: Provenance
    retracted: bool = False
    retraction_id: Optional[str] = None
    retracted_reason: Optional[str] = None

@dataclass
class DriftMetric:
    metric_id: str
    metric_type: str = "drift"
    feature_name: str
    drift_score: float
    baseline_window: str
    current_window: str
    method: str = "kl_divergence"
    provenance: Provenance

@dataclass
class CalibrationMetric:
    metric_id: str
    metric_type: str = "calibration"
    ece: float
    n_bins: int = 10
    calibration_curve: List[Dict[str, float]]
    window: str
    provenance: Provenance

@dataclass
class OutcomeMetric:
    metric_id: str
    metric_type: str = "outcome"
    outcome: str
    count: int
    rate: float
    segment_by: str
    window: str
    provenance: Provenance

# In-memory storage (would be database in production)
_cases: Dict[str, Case] = {}
_alerts: Dict[str, Alert] = {}
_alert_counts: Dict[str, int] = {}  # keyed by time window
_drift_metrics: List[DriftMetric] = []
_calibration_metrics: List[CalibrationMetric] = []
_outcome_metrics: List[OutcomeMetric] = []
_retractions: Dict[str, Dict] = {}
_corrections: Dict[str, Dict] = {}

def get_git_commit() -> str:
    """Get git commit hash - returns environment variable or placeholder."""
    return os.environ.get('GIT_COMMIT', 'local-dev')

def compute_kl_divergence(p: np.ndarray, q: np.ndarray) -> float:
    """Compute KL divergence between two distributions."""
    # Add small epsilon to avoid log(0)
    epsilon = 1e-10
    p = np.clip(p, epsilon, 1)
    q = np.clip(q, epsilon, 1)
    # Normalize
    p = p / p.sum()
    q = q / q.sum()
    return stats.entropy(p, q)

def compute_population_drift(baseline_scores: np.ndarray, current_scores: np.ndarray) -> float:
    """Compute population drift using histogram-based KL divergence."""
    # Create bins
    bins = np.linspace(0, 1, 21)
    baseline_hist, _ = np.histogram(baseline_scores, bins=bins)
    current_hist, _ = np.histogram(current_scores, bins=bins)
    return compute_kl_divergence(baseline_hist, current_hist)

def compute_calibration(outcomes: np.ndarray, scores: np.ndarray, n_bins: int = 10) -> tuple:
    """Compute calibration curve and Expected Calibration Error."""
    bins = np.linspace(0, 1, n_bins + 1)
    calibration_curve = []
    ece = 0.0
    total = len(outcomes)

    for i in range(n_bins):
        bin_mask = (scores >= bins[i]) & (scores < bins[i + 1])
        bin_count = bin_mask.sum()

        if bin_count > 0:
            bin_outcomes = outcomes[bin_mask]
            bin_scores = scores[bin_mask]

            avg_predicted = bin_scores.mean()
            avg_observed = bin_outcomes.mean()

            calibration_curve.append({
                'bin_start': float(bins[i]),
                'bin_end': float(bins[i + 1]),
                'avg_predicted': float(avg_predicted),
                'avg_observed': float(avg_observed),
                'count': int(bin_count)
            })

            ece += (bin_count / total) * abs(avg_predicted - avg_observed)

    return calibration_curve, ece

def check_alert_budget() -> bool:
    """Check if we're within alert budget."""
    window_start = datetime.utcnow() - timedelta(hours=CONFIG['alert_window_hours'])
    recent_alerts = [
        a for a in _alerts.values()
        if not a.retracted and datetime.fromisoformat(a.fired_ts) >= window_start
    ]
    return len(recent_alerts) < CONFIG['alert_budget']

def select_alerts_by_priority(alerts: List[Alert], budget: int) -> List[Alert]:
    """Select which alerts to fire based on priority when budget exceeded."""
    sorted_alerts = sorted(alerts, key=lambda a: (a.priority.value, a.value), reverse=True)
    return sorted_alerts[:budget]

def create_provenance(
    data_version: str,
    data_row_count: int,
    data_time_range: str,
    data_filters: Dict[str, Any],
    model_version: str,
    model_threshold: float,
    prompt_template: str = "N/A",
    policy_alert_rules: Dict[str, Any] = None
) -> Provenance:
    """Create provenance object with full tracking."""
    return Provenance(
        data_version=data_version,
        data_row_count=data_row_count,
        data_time_range=data_time_range,
        data_filters=data_filters,
        model_version=model_version,
        model_threshold=model_threshold,
        prompt_template=prompt_template,
        policy_alert_rules=policy_alert_rules or {
            'drift_threshold': CONFIG['drift_threshold'],
            'calibration_threshold': CONFIG['calibration_threshold'],
            'outcome_threshold': CONFIG['outcome_threshold'],
        },
        policy_budget=CONFIG['alert_budget'],
        commit_hash=get_git_commit()
    )

# API Routes

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'service': 'evaluation-service',
        'version': GIT_COMMIT,
        'cases_ingested': len(_cases),
        'alerts_fired': len([a for a in _alerts.values() if not a.retracted]),
        'config': CONFIG
    })

@app.route('/cases', methods=['POST'])
def ingest_case():
    """Ingest a single scored case."""
    data = request.json
    logger.info('Ingesting case', extra={'case_id': data.get('case_id')})

    case_id = data.get('case_id')
    if not case_id:
        return jsonify({'error': 'case_id required'}), 400

    correction_id = data.get('correction_id')
    if correction_id:
        _corrections[case_id] = {'correction_id': correction_id, 'data': data}

    case = Case(
        case_id=case_id,
        score=data.get('score', 0.5),
        outcome=data.get('outcome'),
        feature_vector=data.get('feature_vector'),
        scored_ts=data.get('scored_ts', datetime.utcnow().isoformat()),
        data_version=data.get('data_version', 'unknown'),
        model_version=data.get('model_version', 'unknown'),
        threshold_used=data.get('threshold_used', 0.62)
    )
    _cases[case_id] = case

    logger.info('Case ingested', extra={'case_id': case_id, 'score': case.score})
    return jsonify({'status': 'ingested', 'case_id': case_id})

@app.route('/cases/batch', methods=['POST'])
def ingest_cases_batch():
    """Batch ingest scored cases."""
    data = request.json
    cases = data.get('cases', [])
    logger.info('Batch ingesting cases', extra={'count': len(cases)})

    ingested = []
    for case_data in cases:
        case_id = case_data.get('case_id')
        if not case_id:
            continue

        correction_id = case_data.get('correction_id')
        if correction_id:
            _corrections[case_id] = {'correction_id': correction_id, 'data': case_data}

        case = Case(
            case_id=case_id,
            score=case_data.get('score', 0.5),
            outcome=case_data.get('outcome'),
            feature_vector=case_data.get('feature_vector'),
            scored_ts=case_data.get('scored_ts', datetime.utcnow().isoformat()),
            data_version=case_data.get('data_version', 'unknown'),
            model_version=case_data.get('model_version', 'unknown'),
            threshold_used=case_data.get('threshold_used', 0.62)
        )
        _cases[case_id] = case
        ingested.append(case_id)

    logger.info('Batch complete', extra={'ingested': len(ingested)})
    return jsonify({'status': 'ingested', 'count': len(ingested), 'case_ids': ingested})

@app.route('/metrics/drift', methods=['GET'])
def get_drift_metrics():
    """Compute and return drift metrics."""
    logger.info('Computing drift metrics')

    baseline_days = CONFIG['baseline_window_days']
    baseline_cutoff = datetime.utcnow() - timedelta(days=baseline_days * 2)
    current_cutoff = datetime.utcnow() - timedelta(days=baseline_days)

    baseline_cases = [c for c in _cases.values()
                     if c.scored_ts and datetime.fromisoformat(c.scored_ts.replace('Z', '+00:00')) >= baseline_cutoff
                     and datetime.fromisoformat(c.scored_ts.replace('Z', '+00:00')) < current_cutoff]
    current_cases = [c for c in _cases.values()
                    if c.scored_ts and datetime.fromisoformat(c.scored_ts.replace('Z', '+00:00')) >= current_cutoff]

    if len(baseline_cases) < 10 or len(current_cases) < 10:
        return jsonify({'error': 'Insufficient data for drift computation'}), 400

    baseline_scores = np.array([c.score for c in baseline_cases])
    current_scores = np.array([c.score for c in current_cases])

    # Population drift
    drift_score = compute_population_drift(baseline_scores, current_scores)

    provenance = create_provenance(
        data_version='current',
        data_row_count=len(_cases),
        data_time_range=f"{baseline_cutoff.isoformat()} to {datetime.utcnow().isoformat()}",
        data_filters={'baseline_days': baseline_days},
        model_version=baseline_cases[0].model_version if baseline_cases else 'unknown',
        model_threshold=baseline_cases[0].threshold_used if baseline_cases else 0.62
    )

    metric = DriftMetric(
        metric_id=str(uuid.uuid4()),
        feature_name='population',
        drift_score=float(drift_score),
        baseline_window=f"{baseline_cutoff.isoformat()} to {current_cutoff.isoformat()}",
        current_window=f"{current_cutoff.isoformat()} to {datetime.utcnow().isoformat()}",
        provenance=provenance
    )
    _drift_metrics.append(metric)

    # Check for alert
    if drift_score > CONFIG['drift_threshold'] and check_alert_budget():
        alert = Alert(
            alert_id=str(uuid.uuid4()),
            metric_type='drift',
            metric_name='population_drift',
            value=drift_score,
            threshold=CONFIG['drift_threshold'],
            priority=AlertPriority.HIGH if drift_score > CONFIG['drift_threshold'] * 2 else AlertPriority.MEDIUM,
            fired_ts=datetime.utcnow().isoformat(),
            case_ids=[c.case_id for c in current_cases[:100]],
            provenance=provenance
        )
        _alerts[alert.alert_id] = alert

    return jsonify({
        'metric_id': metric.metric_id,
        'drift_score': metric.drift_score,
        'threshold': CONFIG['drift_threshold'],
        'alert_fired': alert.alert_id if drift_score > CONFIG['drift_threshold'] and check_alert_budget() else None,
        'provenance': asdict(metric.provenance)
    })

@app.route('/metrics/calibration', methods=['GET'])
def get_calibration_metrics():
    """Compute and return calibration metrics."""
    logger.info('Computing calibration metrics')

    cases_with_outcome = [c for c in _cases.values() if c.outcome is not None]

    if len(cases_with_outcome) < 10:
        return jsonify({'error': 'Insufficient data with outcomes for calibration'}), 400

    outcomes = np.array([1.0 if c.outcome == 'PROCEED' else 0.0 for c in cases_with_outcome])
    scores = np.array([c.score for c in cases_with_outcome])

    calibration_curve, ece = compute_calibration(outcomes, scores)

    provenance = create_provenance(
        data_version='current',
        data_row_count=len(cases_with_outcome),
        data_time_range='all',
        data_filters={},
        model_version=cases_with_outcome[0].model_version,
        model_threshold=cases_with_outcome[0].threshold_used
    )

    metric = CalibrationMetric(
        metric_id=str(uuid.uuid4()),
        ece=float(ece),
        calibration_curve=calibration_curve,
        window=datetime.utcnow().isoformat(),
        provenance=provenance
    )
    _calibration_metrics.append(metric)

    # Check for alert
    if ece > CONFIG['calibration_threshold'] and check_alert_budget():
        alert = Alert(
            alert_id=str(uuid.uuid4()),
            metric_type='calibration',
            metric_name='expected_calibration_error',
            value=ece,
            threshold=CONFIG['calibration_threshold'],
            priority=AlertPriority.HIGH if ece > CONFIG['calibration_threshold'] * 2 else AlertPriority.MEDIUM,
            fired_ts=datetime.utcnow().isoformat(),
            case_ids=[c.case_id for c in cases_with_outcome[:100]],
            provenance=provenance
        )
        _alerts[alert.alert_id] = alert

    return jsonify({
        'metric_id': metric.metric_id,
        'ece': metric.ece,
        'threshold': CONFIG['calibration_threshold'],
        'calibration_curve': metric.calibration_curve,
        'alert_fired': alert.alert_id if ece > CONFIG['calibration_threshold'] and check_alert_budget() else None,
        'provenance': asdict(metric.provenance)
    })

@app.route('/metrics/outcome', methods=['GET'])
def get_outcome_metrics():
    """Compute and return outcome metrics."""
    logger.info('Computing outcome metrics')

    segment_by = request.args.get('segment_by', 'overall')
    cases_with_outcome = [c for c in _cases.values() if c.outcome is not None]

    if len(cases_with_outcome) < 10:
        return jsonify({'error': 'Insufficient data with outcomes'}), 400

    # Calculate outcome rates
    outcome_counts = {}
    for case in cases_with_outcome:
        outcome = case.outcome
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    total = len(cases_with_outcome)
    provenance = create_provenance(
        data_version='current',
        data_row_count=total,
        data_time_range='all',
        data_filters={'segment_by': segment_by},
        model_version='N/A',
        model_threshold=0.0
    )

    metrics = []
    for outcome, count in outcome_counts.items():
        rate = count / total
        metric = OutcomeMetric(
            metric_id=str(uuid.uuid4()),
            outcome=outcome,
            count=count,
            rate=float(rate),
            segment_by=segment_by,
            window=datetime.utcnow().isoformat(),
            provenance=provenance
        )
        _outcome_metrics.append(metric)
        metrics.append({
            'metric_id': metric.metric_id,
            'outcome': outcome,
            'count': count,
            'rate': rate,
            'provenance': asdict(metric.provenance)
        })

        # Check for alert
        baseline_rate = 0.8 if outcome == 'PROCEED' else 0.2
        if abs(rate - baseline_rate) > CONFIG['outcome_threshold'] and check_alert_budget():
            alert = Alert(
                alert_id=str(uuid.uuid4()),
                metric_type='outcome',
                metric_name=f'{outcome}_rate',
                value=rate,
                threshold=baseline_rate,
                priority=AlertPriority.MEDIUM,
                fired_ts=datetime.utcnow().isoformat(),
                case_ids=[c.case_id for c in cases_with_outcome[:100]],
                provenance=provenance
            )
            _alerts[alert.alert_id] = alert

    return jsonify({'metrics': metrics})

@app.route('/alerts/retract', methods=['POST'])
def retract_alert():
    """Retract an alert."""
    data = request.json
    alert_id = data.get('alert_id')
    reason = data.get('reason', 'No reason provided')
    retraction_id = str(uuid.uuid4())

    if alert_id not in _alerts:
        return jsonify({'error': 'Alert not found'}), 404

    alert = _alerts[alert_id]
    alert.retracted = True
    alert.retraction_id = retraction_id
    alert.retracted_reason = reason

    _retractions[retraction_id] = {
        'original_alert_id': alert_id,
        'reason': reason,
        'retracted_at': datetime.utcnow().isoformat()
    }

    logger.info('Alert retracted', extra={'alert_id': alert_id, 'retraction_id': retraction_id})

    return jsonify({
        'status': 'retracted',
        'alert_id': alert_id,
        'retraction_id': retraction_id,
        'reason': reason
    })

@app.route('/metrics/<metric_id>/provenance', methods=['GET'])
def get_provenance(metric_id: str):
    """Get full provenance for a metric."""
    # Check drift metrics
    for m in _drift_metrics:
        if m.metric_id == metric_id:
            return jsonify({
                'metric_id': metric_id,
                'metric_type': 'drift',
                'provenance': asdict(m.provenance)
            })

    # Check calibration metrics
    for m in _calibration_metrics:
        if m.metric_id == metric_id:
            return jsonify({
                'metric_id': metric_id,
                'metric_type': 'calibration',
                'provenance': asdict(m.provenance)
            })

    # Check outcome metrics
    for m in _outcome_metrics:
        if m.metric_id == metric_id:
            return jsonify({
                'metric_id': metric_id,
                'metric_type': 'outcome',
                'provenance': asdict(m.provenance)
            })

    return jsonify({'error': 'Metric not found'}), 404

@app.route('/alerts', methods=['GET'])
def get_alerts():
    """Get all alerts (active and retracted)."""
    include_retracted = request.args.get('include_retracted', 'false').lower() == 'true'

    alerts = []
    for alert in _alerts.values():
        if not include_retracted and alert.retracted:
            continue
        alerts.append({
            'alert_id': alert.alert_id,
            'metric_type': alert.metric_type,
            'metric_name': alert.metric_name,
            'value': alert.value,
            'threshold': alert.threshold,
            'priority': alert.priority.name,
            'fired_ts': alert.fired_ts,
            'retracted': alert.retracted,
            'retraction_id': alert.retraction_id
        })

    return jsonify({'alerts': alerts})

@app.route('/config', methods=['GET'])
def get_config():
    """Get current configuration (safe values only)."""
    return jsonify(CONFIG)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=False)
