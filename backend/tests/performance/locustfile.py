"""
Locust performance testing script for Aloft backend.

Run with: locust -f tests/performance/locustfile.py

This tests the performance of key API endpoints under load.
"""

import random

from locust import HttpUser, between, events, task
from locust.runners import MasterRunner

# Test user credentials
TEST_USER = {
    "email": "performance-test@example.com",
    "password": "TestPassword123!",
}


class AloftUser(HttpUser):
    """Simulates a user interacting with the Aloft API."""

    wait_time = between(1, 3)  # Wait 1-3 seconds between tasks
    token = None

    def on_start(self):
        """Called when a user starts. Login and get token."""
        self.signup_and_login()

    def signup_and_login(self):
        """Sign up a test user and get auth token."""
        # Try to sign up (may fail if user exists)
        self.client.post("/v1/auth/signup", json=TEST_USER)

        # Login to get token
        response = self.client.post(
            "/v1/auth/login", json={"email": TEST_USER["email"], "password": TEST_USER["password"]}
        )

        if response.status_code == 200:
            self.token = response.json()["access_token"]
            self.client.headers.update({"Authorization": f"Bearer {self.token}"})

    @task(3)
    def health_check(self):
        """Test health check endpoint."""
        self.client.get("/health")

    @task(2)
    def readiness_check(self):
        """Test readiness check endpoint."""
        self.client.get("/health/ready")

    @task(5)
    def get_user_profile(self):
        """Test getting user profile."""
        if self.token:
            self.client.get("/v1/auth/me")

    @task(1)
    def discover_pois(self):
        """Test POI discovery endpoint."""
        if self.token:
            # Sample route: JFK to LAX
            self.client.post(
                "/v1/routes/pois",
                json={
                    "origin": {"lat": 40.6413, "lng": -73.7781},  # JFK
                    "destination": {"lat": 33.9425, "lng": -118.4081},  # LAX
                    "corridor_width_km": 100.0,
                },
            )

    @task(1)
    def flight_lookup(self):
        """Test flight number lookup."""
        if self.token:
            # Sample flight number
            flight_numbers = ["AA123", "UA456", "DL789", "BA101", "LH202"]
            flight = random.choice(flight_numbers)
            self.client.post(f"/v1/flights/{flight}/pois")

    @task(1)
    def create_session(self):
        """Test session creation."""
        if self.token:
            self.client.post(
                "/v1/sessions",
                json={"flight_iata": "AA123", "scheduled_departure": "2024-12-01T10:00:00Z"},
            )


class ReadOnlyUser(HttpUser):
    """Simulates a read-only user (no authentication required)."""

    wait_time = between(2, 5)

    @task(5)
    def health_check(self):
        """Test health check endpoint."""
        self.client.get("/health")

    @task(3)
    def readiness_check(self):
        """Test readiness check endpoint."""
        self.client.get("/health/ready")

    @task(2)
    def legal_endpoints(self):
        """Test legal/public endpoints."""
        self.client.get("/legal/privacy")
        self.client.get("/legal/terms")


# Performance monitoring
@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """Initialize performance monitoring."""
    if isinstance(environment.runner, MasterRunner):
        print("Running in master mode - performance metrics will be aggregated")


@events.request.add_listener
def on_request(request_type, name, response_time, response_length, exception, **kwargs):
    """Log performance metrics."""
    if exception:
        print(f"Request failed: {name} - {exception}")
    elif response_time > 1000:  # Log slow requests (> 1s)
        print(f"Slow request: {name} - {response_time}ms")


if __name__ == "__main__":
    import locust

    # Run locally with defaults
    locust.main()
