"""Tests for MQTT resilience framework.

Tests cover:
- Circuit breaker state machine transitions
- Health monitoring for subscriber and publisher
- Metrics collection and formatting
- Heartbeat publishing
- Health check endpoints
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from mqtt.resilience import (
    CircuitBreaker,
    CircuitBreakerState,
    HealthCheckHandler,
    HeartbeatPublisher,
    MQTTHealthMonitor,
    MetricsSnapshot,
    PrometheusMetricsFormatter,
    PublisherHealth,
    SubscriberHealth,
    load_mqtt_config,
    log_publisher_summary,
    log_subscriber_summary,
)


# ─────────────────────────────────────────────────────────────────────────────
# CircuitBreaker Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_initial_state():
    """Circuit breaker starts in CLOSED state."""
    cb = CircuitBreaker(failure_threshold=50, cooldown_secs=300)
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.consecutive_failures == 0


@pytest.mark.asyncio
async def test_circuit_breaker_successful_call():
    """Successful call keeps breaker in CLOSED state."""
    cb = CircuitBreaker(failure_threshold=50, cooldown_secs=300)

    async def success_fn():
        return "ok"

    result = await cb.call(success_fn)
    assert result == "ok"
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.consecutive_failures == 0


@pytest.mark.asyncio
async def test_circuit_breaker_trip_on_threshold():
    """Circuit breaker opens when consecutive failures reach threshold."""
    cb = CircuitBreaker(failure_threshold=3, cooldown_secs=300)

    async def fail_fn():
        raise ValueError("test failure")

    # First 3 failures should trip the breaker
    for i in range(3):
        with pytest.raises(ValueError):
            await cb.call(fail_fn)

    assert cb.state == CircuitBreakerState.OPEN
    assert cb.consecutive_failures == 3


@pytest.mark.asyncio
async def test_circuit_breaker_open_rejects_calls():
    """When OPEN, circuit breaker rejects new calls."""
    cb = CircuitBreaker(failure_threshold=1, cooldown_secs=300)

    async def fail_fn():
        raise ValueError("test failure")

    # Trip the breaker
    with pytest.raises(ValueError):
        await cb.call(fail_fn)

    assert cb.state == CircuitBreakerState.OPEN

    # Subsequent calls should be rejected immediately
    async def success_fn():
        return "ok"

    with pytest.raises(RuntimeError, match="Circuit breaker is OPEN"):
        await cb.call(success_fn)


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_after_cooldown():
    """Circuit breaker transitions to HALF_OPEN after cooldown."""
    cb = CircuitBreaker(failure_threshold=1, cooldown_secs=1)

    async def fail_fn():
        raise ValueError("test failure")

    # Trip the breaker
    with pytest.raises(ValueError):
        await cb.call(fail_fn)

    assert cb.state == CircuitBreakerState.OPEN

    # Wait for cooldown
    await asyncio.sleep(1.1)

    # Next call should attempt (will fail, but transitions to HALF_OPEN first)
    with pytest.raises(ValueError):
        await cb.call(fail_fn)

    assert cb.state == CircuitBreakerState.OPEN  # Failed in half-open


@pytest.mark.asyncio
async def test_circuit_breaker_recovery_from_half_open():
    """Successful call in HALF_OPEN state closes breaker."""
    cb = CircuitBreaker(failure_threshold=1, cooldown_secs=1)

    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            raise ValueError("first call fails")
        return "ok"

    # Trip the breaker
    with pytest.raises(ValueError):
        await cb.call(fn)
    assert cb.state == CircuitBreakerState.OPEN

    # Wait for cooldown
    await asyncio.sleep(1.1)

    # Successful call should close breaker
    result = await cb.call(fn)
    assert result == "ok"
    assert cb.state == CircuitBreakerState.CLOSED
    assert cb.consecutive_failures == 0


@pytest.mark.asyncio
async def test_circuit_breaker_state_transitions():
    """State transitions are tracked."""
    cb = CircuitBreaker(failure_threshold=1, cooldown_secs=300)

    async def fail_fn():
        raise ValueError("test failure")

    with pytest.raises(ValueError):
        await cb.call(fail_fn)

    transitions = cb.get_transitions()
    assert len(transitions) > 0
    assert transitions[-1]["to"] == "OPEN"
    assert transitions[-1]["consecutive_failures"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Health Monitor Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_monitor_initial_state():
    """Health monitor initializes with healthy state."""
    monitor = MQTTHealthMonitor()

    sub_health = await monitor.get_subscriber_health()
    pub_health = await monitor.get_publisher_health()

    assert sub_health.connection_ok is True
    assert pub_health.connection_ok is True
    assert sub_health.queue_depth == 0


@pytest.mark.asyncio
async def test_subscriber_message_recording():
    """Subscriber message recording updates metrics."""
    monitor = MQTTHealthMonitor()

    await monitor.record_subscriber_message(queue_depth=5)
    health = await monitor.get_subscriber_health()

    assert health.total_messages_received == 1
    assert health.queue_depth == 5
    assert health.last_message_ts is not None
    assert health.consecutive_failures == 0


@pytest.mark.asyncio
async def test_subscriber_failure_detection():
    """Subscriber failures update connection status."""
    monitor = MQTTHealthMonitor()

    for _ in range(4):
        await monitor.record_subscriber_failure()

    health = await monitor.get_subscriber_health()
    assert health.consecutive_failures == 4
    assert health.connection_ok is False


@pytest.mark.asyncio
async def test_publisher_send_recording():
    """Publisher send attempts are recorded."""
    monitor = MQTTHealthMonitor()

    await monitor.record_publisher_send(success=True, retried=False)
    await monitor.record_publisher_send(success=True, retried=True)
    await monitor.record_publisher_send(success=False, retried=False)

    health = await monitor.get_publisher_health()
    assert health.total_sends == 3
    assert health.retry_count == 1
    assert health.failed_sends == 1


@pytest.mark.asyncio
async def test_heartbeat_recording():
    """Heartbeat messages are tracked."""
    monitor = MQTTHealthMonitor()

    await monitor.record_heartbeat()
    await monitor.record_heartbeat()

    health = await monitor.get_subscriber_health()
    assert health.connection_ok is True

    # Check stale detection (should not warn immediately)
    stale = await monitor.check_stale_heartbeat()
    assert stale is None


@pytest.mark.asyncio
async def test_stale_heartbeat_detection():
    """Stale heartbeat is detected."""
    monitor = MQTTHealthMonitor(heartbeat_interval_secs=1, heartbeat_timeout_secs=1)

    await monitor.record_heartbeat()
    await asyncio.sleep(1.1)

    stale = await monitor.check_stale_heartbeat()
    assert stale is not None
    assert "no heartbeat" in stale


# ─────────────────────────────────────────────────────────────────────────────
# Metrics Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_snapshot():
    """Metrics snapshot is properly created."""
    monitor = MQTTHealthMonitor()
    cb = CircuitBreaker()

    await monitor.record_subscriber_message(queue_depth=5)
    await monitor.record_publisher_batch(batch_size=10)

    metrics = await monitor.get_metrics(cb)

    assert metrics.mqtt_subscriber_messages_total == 1
    assert metrics.mqtt_publisher_batches_total == 10
    assert metrics.mqtt_circuit_breaker_state == "CLOSED"
    assert metrics.mqtt_queue_depth == 5


def test_prometheus_metrics_formatting():
    """Prometheus metrics are formatted correctly."""
    metrics = MetricsSnapshot(
        mqtt_subscriber_messages_total=100,
        mqtt_publisher_batches_total=50,
        mqtt_publisher_retries_total=10,
        mqtt_publisher_failed_sends_total=5,
        mqtt_circuit_breaker_state="CLOSED",
        mqtt_circuit_breaker_failures=0,
        mqtt_heartbeat_count=30,
        mqtt_queue_depth=5,
    )

    text = PrometheusMetricsFormatter.format_metrics(metrics)

    assert "mqtt_subscriber_messages_total 100" in text
    assert "mqtt_publisher_batches_total 50" in text
    assert "mqtt_circuit_breaker_state 0" in text
    assert "mqtt_heartbeat_count 30" in text
    assert "TYPE mqtt_subscriber_messages_total counter" in text


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat Publisher Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_publisher_start_stop():
    """Heartbeat publisher starts and stops cleanly."""
    published: list[tuple[str, str]] = []

    async def mock_publish(topic: str, message: str) -> None:
        published.append((topic, message))

    monitor = MQTTHealthMonitor()
    hb = HeartbeatPublisher(mock_publish, interval_secs=1, topic="test/heartbeat")

    await hb.start(monitor)
    assert hb._running is True

    await asyncio.sleep(1.1)
    await hb.stop()
    assert hb._running is False


@pytest.mark.asyncio
async def test_heartbeat_publisher_publishes_correctly():
    """Heartbeat publisher publishes valid JSON messages."""
    published: list[tuple[str, str]] = []

    async def mock_publish(topic: str, message: str) -> None:
        published.append((topic, message))

    monitor = MQTTHealthMonitor()
    hb = HeartbeatPublisher(mock_publish, interval_secs=1, topic="dama/colony/heartbeat")

    await hb.start(monitor)
    await asyncio.sleep(1.1)
    await hb.stop()

    assert len(published) > 0
    topic, message = published[0]
    assert topic == "dama/colony/heartbeat"

    payload = json.loads(message)
    assert "timestamp" in payload
    assert payload["status"] == "alive"


# ─────────────────────────────────────────────────────────────────────────────
# Health Check Handler Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_endpoint():
    """Health check endpoint returns correct JSON."""
    monitor = MQTTHealthMonitor()
    cb = CircuitBreaker()
    start_time = time.time()
    handler = HealthCheckHandler(monitor, cb, start_time)

    await monitor.record_subscriber_message(queue_depth=5)
    await monitor.record_publisher_send(success=True)

    health = await handler.handle_health_check()

    assert health["status"] in ["healthy", "degraded"]
    assert "uptime_seconds" in health
    assert health["subscriber"]["queue_depth"] == 5
    assert health["subscriber"]["total_messages"] == 1
    assert health["publisher"]["total_sends"] == 1
    assert health["circuit_breaker"]["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_metrics_endpoint():
    """Metrics endpoint returns Prometheus text format."""
    monitor = MQTTHealthMonitor()
    cb = CircuitBreaker()
    start_time = time.time()
    handler = HealthCheckHandler(monitor, cb, start_time)

    await monitor.record_subscriber_message(queue_depth=5)
    await monitor.record_publisher_batch(batch_size=10)

    metrics_text = await handler.handle_metrics()

    assert "mqtt_subscriber_messages_total" in metrics_text
    assert "mqtt_publisher_batches_total" in metrics_text
    assert "TYPE" in metrics_text
    assert "HELP" in metrics_text


# ─────────────────────────────────────────────────────────────────────────────
# Configuration Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_load_mqtt_config():
    """MQTT configuration is loaded from environment."""
    config = load_mqtt_config()

    assert "circuit_breaker_failure_threshold" in config
    assert "circuit_breaker_cooldown_secs" in config
    assert "heartbeat_interval_secs" in config
    assert "heartbeat_timeout_secs" in config

    # Check defaults are reasonable
    assert config["circuit_breaker_failure_threshold"] > 0
    assert config["circuit_breaker_cooldown_secs"] > 0
    assert config["heartbeat_interval_secs"] > 0
    assert config["heartbeat_timeout_secs"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# Integration Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_resilience_workflow():
    """End-to-end test of resilience framework."""
    # Setup
    monitor = MQTTHealthMonitor(heartbeat_interval_secs=1, heartbeat_timeout_secs=2)
    cb = CircuitBreaker(failure_threshold=3, cooldown_secs=2)

    published = []

    async def mock_publish(topic: str, message: str) -> None:
        published.append((topic, message))

    hb = HeartbeatPublisher(mock_publish, interval_secs=1)
    handler = HealthCheckHandler(monitor, cb, time.time())

    # Start heartbeat
    await hb.start(monitor)

    # Simulate subscriber receiving messages
    await monitor.record_subscriber_message(queue_depth=3)
    await monitor.record_subscriber_message(queue_depth=2)

    # Simulate publisher sending
    await monitor.record_publisher_batch(batch_size=5)
    await monitor.record_publisher_send(success=True)
    await monitor.record_publisher_send(success=True)

    # Check health
    health = await handler.handle_health_check()
    assert health["subscriber"]["total_messages"] == 2
    assert health["publisher"]["total_sends"] == 2

    # Simulate failures (need > 3 to set connection_ok to False)
    for _ in range(4):
        await monitor.record_subscriber_failure()

    health = await handler.handle_health_check()
    assert health["subscriber"]["connected"] is False

    # Simulate circuit breaker trip
    async def fail_fn():
        raise ValueError("test")

    for _ in range(3):
        with pytest.raises(ValueError):
            await cb.call(fail_fn)

    assert cb.state == CircuitBreakerState.OPEN

    # Check metrics
    metrics_text = await handler.handle_metrics()
    assert "mqtt_circuit_breaker_state 1" in metrics_text  # 1 = OPEN

    # Cleanup
    await hb.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Data Class Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_subscriber_health_serialization():
    """SubscriberHealth serializes correctly."""
    health = SubscriberHealth(
        connection_ok=True,
        queue_depth=5,
        last_message_ts=time.time(),
        total_messages_received=10,
    )

    d = health.to_dict()
    assert isinstance(d, dict)
    assert d["connection_ok"] is True
    assert d["queue_depth"] == 5


def test_publisher_health_serialization():
    """PublisherHealth serializes correctly."""
    health = PublisherHealth(
        connection_ok=True,
        batch_count=10,
        retry_count=2,
        failed_sends=1,
        total_sends=50,
    )

    d = health.to_dict()
    assert isinstance(d, dict)
    assert d["batch_count"] == 10
    assert d["retry_count"] == 2


def test_metrics_snapshot_serialization():
    """MetricsSnapshot serializes correctly."""
    metrics = MetricsSnapshot(
        mqtt_subscriber_messages_total=100,
        mqtt_publisher_batches_total=50,
    )

    d = metrics.to_dict()
    assert isinstance(d, dict)
    assert d["mqtt_subscriber_messages_total"] == 100
    assert "timestamp_utc" in d
