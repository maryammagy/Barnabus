"""
Load test for Evaluation Service - p99 latency verification.

Run with:
    locust -f locustfile.py --host=http://localhost:5000 --headless -u 100 -r 10 -t 60s
"""

import random
import json
from locust import HttpUser, task, between, events

class EvaluationServiceUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        """Ingest test data on start."""
        self.ingest_test_data()

    def ingest_test_data(self):
        """Ingest a batch of test cases."""
        cases = []
        for i in range(200):
            cases.append({
                'case_id': f'LOAD_C{i:07d}',
                'score': random.uniform(0.1, 0.9),
                'outcome': random.choice(['PROCEED', 'DEFER', 'CANCEL']),
                'model_version': 'load_test_v1',
                'threshold_used': 0.62,
                'scored_ts': f'2026-{(i % 12) + 1:02d}-15T00:00:00'
            })

        self.client.post('/cases/batch', json={'cases': cases})

    @task(3)
    def health_check(self):
        """Check health endpoint."""
        self.client.get('/health')

    @task(10)
    def compute_drift(self):
        """Compute drift metrics."""
        self.client.get('/metrics/drift')

    @task(10)
    def compute_calibration(self):
        """Compute calibration metrics."""
        self.client.get('/metrics/calibration')

    @task(5)
    def compute_outcome(self):
        """Compute outcome metrics."""
        self.client.get('/metrics/outcome')

    @task(5)
    def get_alerts(self):
        """Get alerts."""
        self.client.get('/alerts')

    @task(2)
    def ingest_single_case(self):
        """Ingest a single case."""
        self.client.post('/cases', json={
            'case_id': f'SINGLE_{random.randint(100000, 999999)}',
            'score': random.uniform(0.1, 0.9),
            'outcome': random.choice(['PROCEED', 'DEFER', 'CANCEL'])
        })

    @task(1)
    def get_config(self):
        """Get config."""
        self.client.get('/config')

@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Print p99 latency stats."""
    stats = environment.stats

    print("\n" + "="*50)
    print("LOAD TEST RESULTS")
    print("="*50)

    total_requests = stats.total.num_requests
    total_failures = stats.total.num_failures

    if total_requests > 0:
        # Calculate p99 from aggregated data
        # Locust provides response times percentiles
        response_times = stats.total.get_response_time_percentiles()

        print(f"Total Requests: {total_requests}")
        print(f"Failures: {total_failures}")
        print(f"Failure Rate: {total_failures/total_requests*100:.2f}%")

        if response_times:
            print(f"\nResponse Time Percentiles (ms):")
            print(f"  50th (median): {response_times[0]:.2f}")
            print(f"  66th: {response_times[1]:.2f}")
            print(f"  75th: {response_times[2]:.2f}")
            print(f"  80th: {response_times[3]:.2f}")
            print(f"  90th: {response_times[4]:.2f}")
            print(f"  95th: {response_times[5]:.2f}")
            print(f"  98th: {response_times[6]:.2f}")
            print(f"  99th: {response_times[7]:.2f}")
            print(f"  99.9th: {response_times[8]:.2f}")

        # Check p99 budget
        if response_times and len(response_times) > 7:
            p99 = response_times[7]
            budget = 500  # Default p99 budget in ms
            print(f"\nP99 Latency: {p99:.2f}ms")
            print(f"P99 Budget: {budget}ms")
            if p99 <= budget:
                print("✓ P99 LATENCY WITHIN BUDGET")
            else:
                print("✗ P99 LATENCY EXCEEDS BUDGET")

    print("="*50)
