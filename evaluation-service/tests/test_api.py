import pytest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from app import (
    app, Case, compute_kl_divergence, compute_population_drift,
    compute_calibration, check_alert_budget, CONFIG
)
import numpy as np

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

@pytest.fixture
def sample_cases():
    """Generate sample cases for testing."""
    cases = []
    for i in range(100):
        cases.append({
            'case_id': f'C{i:07d}',
            'score': 0.3 + (i % 70) / 100.0,
            'outcome': 'PROCEED' if i % 5 != 0 else 'CANCEL',
            'scored_ts': f'2026-{(i % 12) + 1:02d}-01T00:00:00'
        })
    return cases

class TestHealthEndpoint:
    def test_health_check(self, client):
        response = client.get('/health')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['status'] == 'healthy'
        assert data['service'] == 'evaluation-service'

class TestCaseIngestion:
    def test_ingest_single_case(self, client):
        response = client.post('/cases', json={
            'case_id': 'C000000001',
            'score': 0.75,
            'outcome': 'PROCEED',
            'model_version': 'v1.0',
            'threshold_used': 0.62
        })
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['status'] == 'ingested'

    def test_ingest_batch(self, client, sample_cases):
        response = client.post('/cases/batch', json={
            'cases': sample_cases
        })
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['count'] == 100

    def test_ingest_case_with_correction(self, client):
        response = client.post('/cases', json={
            'case_id': 'C000000001',
            'score': 0.75,
            'outcome': 'PROCEED',
            'correction_id': 'CORR001'
        })
        assert response.status_code == 200

class TestDriftMetrics:
    def test_compute_drift_insufficient_data(self, client):
        response = client.get('/metrics/drift')
        assert response.status_code == 400

    def test_compute_drift_with_data(self, client, sample_cases):
        # Ingest enough cases
        client.post('/cases/batch', json={'cases': sample_cases})

        # Now compute drift
        response = client.get('/metrics/drift')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'drift_score' in data
        assert 'provenance' in data

class TestCalibrationMetrics:
    def test_compute_calibration_insufficient_data(self, client):
        response = client.get('/metrics/calibration')
        assert response.status_code == 400

    def test_compute_calibration_with_data(self, client, sample_cases):
        client.post('/cases/batch', json={'cases': sample_cases})
        response = client.get('/metrics/calibration')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'ece' in data

class TestOutcomeMetrics:
    def test_compute_outcome_with_data(self, client, sample_cases):
        client.post('/cases/batch', json={'cases': sample_cases})
        response = client.get('/metrics/outcome')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'metrics' in data

class TestAlertRetraction:
    def test_retract_nonexistent_alert(self, client):
        response = client.post('/alerts/retract', json={
            'alert_id': 'nonexistent',
            'reason': 'Test retraction'
        })
        assert response.status_code == 404

    def test_retract_alert_after_firing(self, client, sample_cases):
        # Ingest cases
        client.post('/cases/batch', json={'cases': sample_cases})

        # Fire an alert by computing drift
        client.get('/metrics/drift')

        # Get alerts
        response = client.get('/alerts')
        data = json.loads(response.data)

        if data['alerts']:
            alert_id = data['alerts'][0]['alert_id']
            response = client.post('/alerts/retract', json={
                'alert_id': alert_id,
                'reason': 'Incorrectly fired'
            })
            assert response.status_code == 200

class TestProvenance:
    def test_provenance_tracking(self, client, sample_cases):
        client.post('/cases/batch', json={'cases': sample_cases})

        # Get drift metric
        response = client.get('/metrics/drift')
        data = json.loads(response.data)
        metric_id = data.get('metric_id')

        if metric_id:
            response = client.get(f'/metrics/{metric_id}/provenance')
            assert response.status_code == 200
            prov_data = json.loads(response.data)
            assert 'provenance' in prov_data
            prov = prov_data['provenance']
            assert 'commit_hash' in prov
            assert 'data_version' in prov

class TestAlertBudget:
    def test_alert_budget_enforcement(self, client):
        # Set very low budget for testing
        original_budget = CONFIG['alert_budget']

        # Create many alerts
        cases = []
        for i in range(50):
            cases.append({
                'case_id': f'C{i:07d}',
                'score': 0.1 + (i % 10) / 100.0,  # Low scores to trigger drift
                'outcome': 'PROCEED',
                'scored_ts': f'2025-01-{(i % 28) + 1:02d}T00:00:00'
            })
            # Also need cases from older period for baseline
            cases.append({
                'case_id': f'COLD{i:07d}',
                'score': 0.8,
                'outcome': 'PROCEED',
                'scored_ts': f'2024-12-{(i % 28) + 1:02d}T00:00:00'
            })

        client.post('/cases/batch', json={'cases': cases})

        # Compute drift multiple times
        for _ in range(5):
            response = client.get('/metrics/drift')

        # Check alerts count
        response = client.get('/alerts')
        data = json.loads(response.data)
        active_alerts = [a for a in data['alerts'] if not a['retracted']]

        assert len(active_alerts) <= original_budget

class TestConfiguration:
    def test_config_endpoint(self, client):
        response = client.get('/config')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'alert_budget' in data
        assert 'drift_threshold' in data

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
