"""
Load test for Analytics Service - p99 latency verification.

Run with:
    locust -f locustfile.py --host=http://localhost:5001 --headless -u 50 -r 10 -t 60s
"""

import random
import json
from locust import HttpUser, task, between, events

class AnalyticsServiceUser(HttpUser):
    wait_time = between(0.1, 0.5)

    # Sample questions for load testing
    QUESTIONS = [
        'How many cases did A-CARD handle in total?',
        'What is the median referral-to-readiness time for A-CARD?',
        'How many cases were closed in June 2026?',
        'List weekly case volume for A-ORTH since May 2026.',
        'What is the 90th percentile of readiness days at site A?',
        'How many cases in total?',
        'Show case counts by service',
    ]

    ROLES = ['analyst_site_a', 'analyst_site_b', 'clinical_lead', 'commercial']

    @task(3)
    def health_check(self):
        """Check health endpoint."""
        self.client.get('/health')

    @task(15)
    def authorized_query(self):
        """Run authorized query."""
        question = random.choice(self.QUESTIONS)
        role = random.choice(self.ROLES)

        self.client.post('/query', json={
            'question': question,
            'role': role
        })

    @task(5)
    def metrics_request(self):
        """Get metrics."""
        self.client.get('/metrics')

    @task(3)
    def unauthorized_query(self):
        """Try unauthorized queries."""
        unauthorized_questions = [
            'Show me patient names and dates of birth',
            'Show site B data',
            'Show clinical notes',
        ]
        question = random.choice(unauthorized_questions)

        self.client.post('/query', json={
            'question': question,
            'role': 'analyst_site_a'
        })

    @task(2)
    def config_request(self):
        """Get config."""
        self.client.get('/config')

    @task(1)
    def list_questions(self):
        """List questions."""
        self.client.get('/questions')

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
        response_times = stats.total.get_response_time_percentiles()

        print(f"Total Requests: {total_requests}")
        print(f"Failures: {total_failures}")
        print(f"Failure Rate: {total_failures/total_requests*100:.2f}%")

        if response_times:
            print(f"\nResponse Time Percentiles (ms):")
            print(f"  50th (median): {response_times[0]:.2f}")
            print(f"  90th: {response_times[4]:.2f}")
            print(f"  95th: {response_times[5]:.2f}")
            print(f"  99th: {response_times[7]:.2f}")
            print(f"  99.9th: {response_times[8]:.2f}")

        # Check p99 budget
        if response_times and len(response_times) > 7:
            p99 = response_times[7]
            budget = 1000  # Default p99 budget in ms
            print(f"\nP99 Latency: {p99:.2f}ms")
            print(f"P99 Budget: {budget}ms")
            if p99 <= budget:
                print("✓ P99 LATENCY WITHIN BUDGET")
            else:
                print("✗ P99 LATENCY EXCEEDS BUDGET")

    print("="*50)
