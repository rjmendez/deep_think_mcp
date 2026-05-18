"""Tests for the health check endpoint.

Covers:
- Unit test: health endpoint response format
- Integration test: health check reflects queue state
- Load test: health endpoint under concurrent requests
- Edge case: DB connection failure (should return 503)
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch, MagicMock

import pytest

from deep_think_mcp import health, store
from deep_think_mcp.api import health as health_api


class TestHealthMetrics:
    """Unit tests for health metrics calculation."""

    def test_health_metrics_response_format(self):
        """Test that health metrics returns correct response format."""
        # Create a mock connection
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            row = Mock()
            row.__getitem__ = Mock(side_effect=lambda x: 5)
            row.get = Mock(return_value=None)
            
            # Mock the database queries
            cursor = Mock()
            cursor.fetchone = Mock(return_value=row)
            conn.execute = Mock(return_value=cursor)
            conn.row_factory = None
            return conn

        metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)

        # Check required fields
        assert "status" in metrics
        assert "http_status" in metrics
        assert "timestamp" in metrics
        assert "pending_count" in metrics
        assert "running_count" in metrics
        assert "failed_count" in metrics
        assert "avg_latency" in metrics
        assert "last_success_timestamp" in metrics
        assert "oldest_queued_age_secs" in metrics
        assert "oldest_running_age_secs" in metrics
        assert "worker_count" in metrics
        assert "db_status" in metrics
        assert "completed_count" in metrics
        assert "timeout_count" in metrics
        assert "timeout_by_component" in metrics
        assert "orphaned_jobs_detected" in metrics
        assert "orphaned_jobs_requeued" in metrics

    def test_health_status_healthy(self):
        """Test healthy status when pending jobs are below threshold."""
        health.reset_cache()
        
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            
            # Mock count query
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=5)
            
            # Mock avg query
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 10.5,
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 100
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            # Return different mocks for different queries
            call_count = [0]
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)

        assert metrics["status"] == "healthy"
        assert metrics["http_status"] == 200
        assert metrics["pending_count"] == 5

    def test_health_status_degraded(self):
        """Test degraded status when pending jobs exceed threshold."""
        health.reset_cache()
        
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            
            # Mock count query with high pending count
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=150)  # Above threshold
            
            # Mock avg query
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 10.5,
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 100
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            call_count = [0]
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)

        assert metrics["status"] == "degraded"
        assert metrics["http_status"] == 503
        assert metrics["pending_count"] == 150
        assert "reason" in metrics

    def test_health_db_unavailable(self):
        """Test that database errors return 503 status."""
        health.reset_cache()
        
        def mock_connect():
            raise sqlite3.DatabaseError("Connection failed")

        metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)

        assert metrics["status"] == "degraded"
        assert metrics["http_status"] == 503
        assert metrics["db_status"] == "unavailable"
        assert "reason" in metrics

    def test_health_metrics_caching(self):
        """Test that metrics are cached to ensure fast response."""
        health.reset_cache()
        
        call_count = [0]
        
        def mock_connect():
            call_count[0] += 1
            conn = Mock(spec=sqlite3.Connection)
            
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=5)
            
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 10.5,
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 100
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            inner_call_count = [0]
            def side_effect(*args, **kwargs):
                inner_call_count[0] += 1
                if inner_call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        # First call should hit the database
        metrics1 = health.get_health_metrics(mock_connect, max_pending_threshold=100)
        first_call_count = call_count[0]

        # Second call (within TTL) should use cache
        metrics2 = health.get_health_metrics(mock_connect, max_pending_threshold=100)
        second_call_count = call_count[0]

        # Compare all fields except timestamp (which is generated on each response)
        assert metrics1["status"] == metrics2["status"]
        assert metrics1["pending_count"] == metrics2["pending_count"]
        assert metrics1["avg_latency"] == metrics2["avg_latency"]
        assert metrics1["worker_count"] == metrics2["worker_count"]
        assert second_call_count == first_call_count  # DB not called again

    def test_health_worker_count_uses_runtime_worker_data(self):
        """Worker count should come from runtime state, not a hardcoded value."""
        health.reset_cache()

        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=0)
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 1.0,
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 1
            }.get(x))
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            call_count = [0]

            def side_effect(*args, **kwargs):
                call_count[0] += 1
                return cursor1 if call_count[0] == 1 else cursor2

            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        with patch("deep_think_mcp.health._get_worker_runtime", return_value={"active_workers": 3}):
            metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)
        assert metrics["worker_count"] == 3

    def test_health_cache_expiration(self):
        """Test that cache expires after TTL."""
        health.reset_cache()
        
        # Set cache TTL to 1 second for testing
        original_ttl = health._CACHE_TTL
        health._CACHE_TTL = 1
        
        try:
            call_count = [0]
            
            def mock_connect():
                call_count[0] += 1
                conn = Mock(spec=sqlite3.Connection)
                
                count_row = Mock()
                count_row.__getitem__ = Mock(return_value=call_count[0])  # Different count each call
                
                avg_row = Mock()
                avg_row.__getitem__ = Mock(side_effect=lambda x: {
                    "avg_secs": 10.5,
                    "last_success": "2024-01-01T00:00:00",
                    "total_completed": 100
                }.get(x))
                
                cursor1 = Mock()
                cursor1.fetchone = Mock(return_value=count_row)
                cursor2 = Mock()
                cursor2.fetchone = Mock(return_value=avg_row)
                
                inner_call_count = [0]
                def side_effect(*args, **kwargs):
                    inner_call_count[0] += 1
                    if inner_call_count[0] == 1:
                        return cursor1
                    else:
                        return cursor2
                
                conn.execute = Mock(side_effect=side_effect)
                conn.row_factory = None
                conn.close = Mock()
                return conn

            # First call
            metrics1 = health.get_health_metrics(mock_connect, max_pending_threshold=100)

            # Wait for cache to expire
            time.sleep(1.1)

            # Second call should hit DB again
            metrics2 = health.get_health_metrics(mock_connect, max_pending_threshold=100)

            # Pending count should be different since we called the mock twice
            assert metrics1["pending_count"] == 1
            assert metrics2["pending_count"] == 2
        finally:
            health._CACHE_TTL = original_ttl


class TestHealthEndpoint:
    """Integration tests for the health endpoint."""

    @pytest.mark.asyncio
    async def test_health_endpoint_response_format(self):
        """Test that health endpoint returns properly formatted JSON."""
        # This would require importing the actual server and making HTTP requests
        # For now, we test the underlying metrics function
        health.reset_cache()
        
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=0)
            
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 5.0,
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 50
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            call_count = [0]
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        result = health.get_health_metrics(mock_connect)

        # Validate response is JSON-serializable
        json_str = json.dumps(result)
        parsed = json.loads(json_str)

        assert parsed["status"] in ["healthy", "degraded"]
        assert isinstance(parsed["pending_count"], int)
        assert isinstance(parsed["avg_latency"], (int, float))
        assert isinstance(parsed["worker_count"], int)

    def test_health_reflects_queue_state(self):
        """Test that health check reflects actual queue state."""
        health.reset_cache()
        
        pending_jobs = [5, 10, 15]
        call_index = [0]
        
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            
            # Return different pending counts
            count_row = Mock()
            pending = pending_jobs[min(call_index[0], len(pending_jobs) - 1)]
            count_row.__getitem__ = Mock(return_value=pending)
            
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 10.0,
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 100
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            inner_call_count = [0]
            def side_effect(*args, **kwargs):
                inner_call_count[0] += 1
                if inner_call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            
            call_index[0] += 1
            return conn

        # Test with different pending counts
        for expected_pending in pending_jobs:
            health.reset_cache()  # Clear cache to force DB query
            metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)
            assert metrics["pending_count"] == expected_pending


class TestHealthLoadTest:
    """Load tests for the health endpoint."""

    def test_health_concurrent_requests(self):
        """Test health endpoint under concurrent requests."""
        health.reset_cache()
        
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=5)
            
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 10.0,
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 100
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            call_count = [0]
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        # Simulate concurrent requests
        import threading
        results = []
        errors = []

        def request_health():
            try:
                metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)
                results.append(metrics)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=request_health) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert len(results) == 10
        
        # All results should have the same metrics values (except timestamp)
        first_result = results[0]
        for r in results[1:]:
            assert r["status"] == first_result["status"]
            assert r["pending_count"] == first_result["pending_count"]
            assert r["avg_latency"] == first_result["avg_latency"]
            assert r["worker_count"] == first_result["worker_count"]

    def test_health_response_time(self):
        """Test that health check completes in <100ms (with cache)."""
        health.reset_cache()
        
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=5)
            
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 10.0,
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 100
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            call_count = [0]
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        # Warm up cache
        health.get_health_metrics(mock_connect, max_pending_threshold=100)

        # Time the response
        start = time.time()
        for _ in range(100):
            health.get_health_metrics(mock_connect, max_pending_threshold=100)
        elapsed = (time.time() - start) * 1000 / 100  # Convert to ms per request

        # Average should be well under 100ms
        assert elapsed < 100, f"Health check took {elapsed:.2f}ms (max allowed: 100ms)"


class TestHealthEdgeCases:
    """Edge case tests for health check."""

    def test_db_connection_failure(self):
        """Test that database connection failure returns 503."""
        health.reset_cache()
        
        def mock_connect():
            raise sqlite3.DatabaseError("Connection refused")

        metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)

        assert metrics["status"] == "degraded"
        assert metrics["http_status"] == 503
        assert metrics["db_status"] == "unavailable"

    def test_db_integrity_error(self):
        """Test that database integrity errors return 503."""
        health.reset_cache()
        
        def mock_connect():
            raise sqlite3.DatabaseError("Database disk image is malformed")

        metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)

        assert metrics["status"] == "degraded"
        assert metrics["http_status"] == 503

    def test_empty_database(self):
        """Test health check with empty database (no jobs)."""
        health.reset_cache()
        
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=0)  # No pending jobs
            
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": None,  # No completed jobs
                "last_success": None,
                "total_completed": 0
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            call_count = [0]
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)

        assert metrics["status"] == "healthy"
        assert metrics["http_status"] == 200
        assert metrics["pending_count"] == 0
        assert metrics["avg_latency"] == 0
        assert metrics["last_success_timestamp"] is None
        assert metrics["completed_count"] == 0

    def test_high_latency_jobs(self):
        """Test health check with high average latency."""
        health.reset_cache()
        
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=5)
            
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 3600.0,  # 1 hour average
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 10
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            call_count = [0]
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)

        assert metrics["status"] == "healthy"  # Still healthy if under job threshold
        assert metrics["avg_latency"] == 3600.0

    def test_many_pending_jobs(self):
        """Test health check degradation with many pending jobs."""
        health.reset_cache()
        
        def mock_connect():
            conn = Mock(spec=sqlite3.Connection)
            
            count_row = Mock()
            count_row.__getitem__ = Mock(return_value=500)  # Many pending
            
            avg_row = Mock()
            avg_row.__getitem__ = Mock(side_effect=lambda x: {
                "avg_secs": 10.0,
                "last_success": "2024-01-01T00:00:00",
                "total_completed": 1000
            }.get(x))
            
            cursor1 = Mock()
            cursor1.fetchone = Mock(return_value=count_row)
            cursor2 = Mock()
            cursor2.fetchone = Mock(return_value=avg_row)
            
            call_count = [0]
            def side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return cursor1
                else:
                    return cursor2
            
            conn.execute = Mock(side_effect=side_effect)
            conn.row_factory = None
            conn.close = Mock()
            return conn

        metrics = health.get_health_metrics(mock_connect, max_pending_threshold=100)

        assert metrics["status"] == "degraded"
        assert metrics["http_status"] == 503
        assert "reason" in metrics
        assert "pending" in metrics["reason"].lower()


class TestMetricsEndpoint:
    def test_metrics_route_returns_prometheus_payload(self):
        routes = {}

        class FakeMCP:
            def custom_route(self, path, methods=None):
                def decorator(func):
                    routes[path] = func
                    return func
                return decorator

        health_api.register(FakeMCP())
        assert "/metrics" in routes

        response = asyncio.run(routes["/metrics"](Mock()))
        assert response.status_code == 200
        assert "ground_truth_validations_total" in response.body.decode()
