"""MQTT Resilience Framework with Circuit Breaker, Health Monitoring, and Metrics.

Provides comprehensive resilience patterns for MQTT operations including:
- Circuit breaker pattern with state machine
- Health monitoring for subscriber and publisher
- Heartbeat task with stale detection
- Prometheus-style metrics collection
- JSON health endpoint
- Graceful shutdown with pending batch flushing

Architecture:
- Pure async, no blocking I/O
- Thread-safe state transitions using asyncio.Lock
- Type hints throughout for static analysis
- Comprehensive logging with structured context
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Protocol

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# State Machine & Enums
# ─────────────────────────────────────────────────────────────────────────────


class CircuitBreakerState(Enum):
    """Circuit breaker states per state machine pattern."""

    CLOSED = "CLOSED"  # Normal operation
    OPEN = "OPEN"  # Failing, reject requests
    HALF_OPEN = "HALF_OPEN"  # Testing recovery


# ─────────────────────────────────────────────────────────────────────────────
# Type Definitions
# ─────────────────────────────────────────────────────────────────────────────


class MetricCollector(Protocol):
    """Protocol for metric collection implementations."""

    def increment(self, metric_name: str, delta: float = 1.0, tags: Optional[Dict[str, str]] = None) -> None:
        """Increment a metric."""
        ...

    def gauge(self, metric_name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        """Set a gauge value."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SubscriberHealth:
    """Health status of MQTT subscriber."""

    connection_ok: bool = True
    queue_depth: int = 0
    last_message_ts: Optional[float] = None
    total_messages_received: int = 0
    consecutive_failures: int = 0
    last_failure_ts: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return asdict(self)


@dataclass
class PublisherHealth:
    """Health status of MQTT publisher."""

    connection_ok: bool = True
    batch_count: int = 0
    retry_count: int = 0
    failed_sends: int = 0
    total_sends: int = 0
    last_send_ts: Optional[float] = None
    consecutive_failures: int = 0
    last_failure_ts: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return asdict(self)


@dataclass
class MetricsSnapshot:
    """Snapshot of all MQTT metrics."""

    mqtt_subscriber_messages_total: int = 0
    mqtt_publisher_batches_total: int = 0
    mqtt_publisher_retries_total: int = 0
    mqtt_publisher_failed_sends_total: int = 0
    mqtt_circuit_breaker_state: str = "CLOSED"
    mqtt_circuit_breaker_failures: int = 0
    mqtt_heartbeat_count: int = 0
    mqtt_queue_depth: int = 0
    timestamp_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Circuit Breaker Implementation
# ─────────────────────────────────────────────────────────────────────────────


class CircuitBreaker:
    """State machine for MQTT operation resilience.

    Transitions:
    - CLOSED → OPEN: When consecutive_failures >= failure_threshold
    - OPEN → HALF_OPEN: After cooldown_secs
    - HALF_OPEN → CLOSED: After successful call
    - HALF_OPEN → OPEN: After failed call in half-open state

    Thread-safe via asyncio.Lock for state transitions.
    """

    def __init__(
        self,
        failure_threshold: int = 50,
        cooldown_secs: int = 300,
        name: str = "mqtt",
    ):
        """Initialize circuit breaker.

        Args:
            failure_threshold: Percentage of consecutive failures to trip (0-100)
            cooldown_secs: Seconds to wait before trying half-open state
            name: Name for logging
        """
        self.failure_threshold = failure_threshold
        self.cooldown_secs = cooldown_secs
        self.name = name

        self._state = CircuitBreakerState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_ts: Optional[float] = None
        self._state_changed_ts = time.time()
        self._lock = asyncio.Lock()
        self._state_transitions: List[Dict[str, Any]] = []

        log.info(
            f"[MQTT] [circuit_breaker] Initialized {name} "
            f"(threshold={failure_threshold}%, cooldown={cooldown_secs}s)"
        )

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute function with circuit breaker protection.

        Args:
            func: Async function to call
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result if successful

        Raises:
            RuntimeError: If circuit is OPEN
            Exception: If function raises exception
        """
        async with self._lock:
            state = self._state

        if state == CircuitBreakerState.OPEN:
            elapsed = time.time() - self._state_changed_ts
            if elapsed >= self.cooldown_secs:
                await self._transition_to(CircuitBreakerState.HALF_OPEN)
            else:
                raise RuntimeError(f"Circuit breaker is {state.value} (retry in {self.cooldown_secs - elapsed:.1f}s)")

        try:
            result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            await self._on_failure()
            raise

    async def _on_success(self) -> None:
        """Handle successful call."""
        async with self._lock:
            if self._state == CircuitBreakerState.HALF_OPEN:
                self._consecutive_failures = 0
                await self._transition_to(CircuitBreakerState.CLOSED)
            elif self._state == CircuitBreakerState.CLOSED:
                self._consecutive_failures = 0

    async def _on_failure(self) -> None:
        """Handle failed call."""
        async with self._lock:
            self._consecutive_failures += 1
            self._last_failure_ts = time.time()

            if self._state == CircuitBreakerState.HALF_OPEN:
                await self._transition_to(CircuitBreakerState.OPEN)
            elif self._state == CircuitBreakerState.CLOSED:
                if self._consecutive_failures >= self.failure_threshold:
                    await self._transition_to(CircuitBreakerState.OPEN)

    async def _transition_to(self, new_state: CircuitBreakerState) -> None:
        """Transition to new state (must be called with lock held).

        Args:
            new_state: Target state
        """
        old_state = self._state
        self._state = new_state
        self._state_changed_ts = time.time()

        transition = {
            "from": old_state.value,
            "to": new_state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "consecutive_failures": self._consecutive_failures,
        }
        self._state_transitions.append(transition)

        elapsed = time.time() - self._state_changed_ts if len(self._state_transitions) > 1 else 0
        log.warning(
            f"[MQTT] [circuit_breaker] State change: {old_state.value} → {new_state.value} "
            f"({self._consecutive_failures} consecutive failures)"
        )

    @property
    def state(self) -> CircuitBreakerState:
        """Get current state (non-blocking read)."""
        return self._state

    @property
    def consecutive_failures(self) -> int:
        """Get consecutive failure count."""
        return self._consecutive_failures

    @property
    def last_failure_ts(self) -> Optional[float]:
        """Get timestamp of last failure."""
        return self._last_failure_ts

    def get_transitions(self) -> List[Dict[str, Any]]:
        """Get all state transitions for debugging."""
        return list(self._state_transitions)


# ─────────────────────────────────────────────────────────────────────────────
# Health Monitor
# ─────────────────────────────────────────────────────────────────────────────


class MQTTHealthMonitor:
    """Monitor health of MQTT subscriber and publisher.

    Tracks connection status, message flow, queue depth, and detects stale
    subscribers (no heartbeat for 2x interval).
    """

    def __init__(
        self,
        heartbeat_interval_secs: int = 30,
        heartbeat_timeout_secs: int = 60,
    ):
        """Initialize health monitor.

        Args:
            heartbeat_interval_secs: Expected heartbeat interval
            heartbeat_timeout_secs: Alert if no heartbeat for this long
        """
        self.heartbeat_interval_secs = heartbeat_interval_secs
        self.heartbeat_timeout_secs = heartbeat_timeout_secs

        self.subscriber = SubscriberHealth()
        self.publisher = PublisherHealth()

        self._lock = asyncio.Lock()
        self._last_heartbeat_ts: Optional[float] = None
        self._heartbeat_count = 0

        log.info(
            f"[MQTT] [health] Initialized monitor "
            f"(heartbeat_interval={heartbeat_interval_secs}s, timeout={heartbeat_timeout_secs}s)"
        )

    async def record_subscriber_message(self, queue_depth: int = 0) -> None:
        """Record a message received by subscriber.

        Args:
            queue_depth: Current queue depth
        """
        async with self._lock:
            self.subscriber.last_message_ts = time.time()
            self.subscriber.total_messages_received += 1
            self.subscriber.queue_depth = queue_depth
            self.subscriber.consecutive_failures = 0

    async def record_subscriber_failure(self) -> None:
        """Record a subscriber failure."""
        async with self._lock:
            self.subscriber.consecutive_failures += 1
            self.subscriber.last_failure_ts = time.time()
            if self.subscriber.consecutive_failures > 3:
                self.subscriber.connection_ok = False

    async def record_publisher_send(self, success: bool, retried: bool = False) -> None:
        """Record a publisher send attempt.

        Args:
            success: Whether send succeeded
            retried: Whether this was a retry
        """
        async with self._lock:
            self.publisher.total_sends += 1
            self.publisher.last_send_ts = time.time()

            if retried:
                self.publisher.retry_count += 1

            if success:
                self.publisher.consecutive_failures = 0
                self.publisher.connection_ok = True
            else:
                self.publisher.failed_sends += 1
                self.publisher.consecutive_failures += 1
                self.publisher.last_failure_ts = time.time()

                if self.publisher.consecutive_failures > 3:
                    self.publisher.connection_ok = False

    async def record_publisher_batch(self, batch_size: int = 1) -> None:
        """Record a publisher batch.

        Args:
            batch_size: Number of messages in batch
        """
        async with self._lock:
            self.publisher.batch_count += batch_size

    async def record_heartbeat(self) -> None:
        """Record a heartbeat message."""
        async with self._lock:
            self._last_heartbeat_ts = time.time()
            self._heartbeat_count += 1

    async def check_stale_heartbeat(self) -> Optional[str]:
        """Check if heartbeat is stale.

        Returns:
            Warning message if stale, None if healthy
        """
        async with self._lock:
            if self._last_heartbeat_ts is None:
                return None

            elapsed = time.time() - self._last_heartbeat_ts
            if elapsed > self.heartbeat_timeout_secs:
                msg = (
                    f"[MQTT] [health] Subscriber unhealthy: no heartbeat for {elapsed:.0f}s "
                    f"(threshold {self.heartbeat_timeout_secs}s)"
                )
                return msg

        return None

    async def get_subscriber_health(self) -> SubscriberHealth:
        """Get subscriber health snapshot."""
        async with self._lock:
            return SubscriberHealth(
                connection_ok=self.subscriber.connection_ok,
                queue_depth=self.subscriber.queue_depth,
                last_message_ts=self.subscriber.last_message_ts,
                total_messages_received=self.subscriber.total_messages_received,
                consecutive_failures=self.subscriber.consecutive_failures,
                last_failure_ts=self.subscriber.last_failure_ts,
            )

    async def get_publisher_health(self) -> PublisherHealth:
        """Get publisher health snapshot."""
        async with self._lock:
            return PublisherHealth(
                connection_ok=self.publisher.connection_ok,
                batch_count=self.publisher.batch_count,
                retry_count=self.publisher.retry_count,
                failed_sends=self.publisher.failed_sends,
                total_sends=self.publisher.total_sends,
                last_send_ts=self.publisher.last_send_ts,
                consecutive_failures=self.publisher.consecutive_failures,
                last_failure_ts=self.publisher.last_failure_ts,
            )

    async def get_metrics(self, circuit_breaker: CircuitBreaker) -> MetricsSnapshot:
        """Get all metrics as snapshot.

        Args:
            circuit_breaker: Circuit breaker instance

        Returns:
            Metrics snapshot
        """
        async with self._lock:
            return MetricsSnapshot(
                mqtt_subscriber_messages_total=self.subscriber.total_messages_received,
                mqtt_publisher_batches_total=self.publisher.batch_count,
                mqtt_publisher_retries_total=self.publisher.retry_count,
                mqtt_publisher_failed_sends_total=self.publisher.failed_sends,
                mqtt_circuit_breaker_state=circuit_breaker.state.value,
                mqtt_circuit_breaker_failures=circuit_breaker.consecutive_failures,
                mqtt_heartbeat_count=self._heartbeat_count,
                mqtt_queue_depth=self.subscriber.queue_depth,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Metrics Formatter
# ─────────────────────────────────────────────────────────────────────────────


class PrometheusMetricsFormatter:
    """Format metrics in Prometheus text exposition format."""

    @staticmethod
    def format_metrics(metrics: MetricsSnapshot) -> str:
        """Format metrics as Prometheus text.

        Args:
            metrics: Metrics snapshot

        Returns:
            Prometheus text format metrics
        """
        lines = [
            "# HELP mqtt_subscriber_messages_total Total messages received from subscriber",
            "# TYPE mqtt_subscriber_messages_total counter",
            f"mqtt_subscriber_messages_total {metrics.mqtt_subscriber_messages_total}",
            "",
            "# HELP mqtt_publisher_batches_total Total batches published",
            "# TYPE mqtt_publisher_batches_total counter",
            f"mqtt_publisher_batches_total {metrics.mqtt_publisher_batches_total}",
            "",
            "# HELP mqtt_publisher_retries_total Total publish retries",
            "# TYPE mqtt_publisher_retries_total counter",
            f"mqtt_publisher_retries_total {metrics.mqtt_publisher_retries_total}",
            "",
            "# HELP mqtt_publisher_failed_sends_total Total failed publish attempts",
            "# TYPE mqtt_publisher_failed_sends_total counter",
            f"mqtt_publisher_failed_sends_total {metrics.mqtt_publisher_failed_sends_total}",
            "",
            "# HELP mqtt_circuit_breaker_state Circuit breaker state (0=CLOSED, 1=OPEN, 2=HALF_OPEN)",
            "# TYPE mqtt_circuit_breaker_state gauge",
            f"mqtt_circuit_breaker_state {_state_to_gauge(metrics.mqtt_circuit_breaker_state)}",
            "",
            "# HELP mqtt_circuit_breaker_failures Current consecutive failures",
            "# TYPE mqtt_circuit_breaker_failures gauge",
            f"mqtt_circuit_breaker_failures {metrics.mqtt_circuit_breaker_failures}",
            "",
            "# HELP mqtt_heartbeat_count Total heartbeats sent",
            "# TYPE mqtt_heartbeat_count counter",
            f"mqtt_heartbeat_count {metrics.mqtt_heartbeat_count}",
            "",
            "# HELP mqtt_queue_depth Current message queue depth",
            "# TYPE mqtt_queue_depth gauge",
            f"mqtt_queue_depth {metrics.mqtt_queue_depth}",
            "",
        ]
        return "\n".join(lines)


def _state_to_gauge(state: str) -> int:
    """Convert circuit breaker state to gauge value for Prometheus."""
    state_map = {
        "CLOSED": 0,
        "OPEN": 1,
        "HALF_OPEN": 2,
    }
    return state_map.get(state, -1)


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat Publisher
# ─────────────────────────────────────────────────────────────────────────────


class HeartbeatPublisher:
    """Publish periodic heartbeat messages for liveness detection.

    Publishes to dama/colony/heartbeat every N seconds.
    """

    def __init__(
        self,
        publish_fn: Callable[[str, str], Any],
        interval_secs: int = 30,
        topic: str = "dama/colony/heartbeat",
    ):
        """Initialize heartbeat publisher.

        Args:
            publish_fn: Async function to publish message (topic, message)
            interval_secs: Publish interval in seconds
            topic: MQTT topic to publish to
        """
        self.publish_fn = publish_fn
        self.interval_secs = interval_secs
        self.topic = topic

        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

    async def start(self, health_monitor: MQTTHealthMonitor) -> None:
        """Start heartbeat task.

        Args:
            health_monitor: Health monitor to record heartbeats
        """
        if self._running:
            log.warning("[MQTT] [heartbeat] Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_heartbeat_loop(health_monitor))
        log.info(f"[MQTT] [heartbeat] Started (interval={self.interval_secs}s)")

    async def stop(self) -> None:
        """Stop heartbeat task gracefully."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("[MQTT] [heartbeat] Stopped")

    async def _run_heartbeat_loop(self, health_monitor: MQTTHealthMonitor) -> None:
        """Run heartbeat publishing loop.

        Args:
            health_monitor: Health monitor to record heartbeats
        """
        while self._running:
            try:
                await asyncio.sleep(self.interval_secs)

                if not self._running:
                    break

                payload = json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "alive",
                    }
                )

                await self.publish_fn(self.topic, payload)
                await health_monitor.record_heartbeat()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[MQTT] [heartbeat] Error publishing: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Health Check Endpoint Handler
# ─────────────────────────────────────────────────────────────────────────────


class HealthCheckHandler:
    """HTTP endpoint handler for health checks and metrics.

    Provides:
    - GET /mqtt/health → JSON health status
    - GET /mqtt/metrics → Prometheus metrics
    """

    def __init__(
        self,
        health_monitor: MQTTHealthMonitor,
        circuit_breaker: CircuitBreaker,
        start_time: float,
    ):
        """Initialize health check handler.

        Args:
            health_monitor: Health monitor instance
            circuit_breaker: Circuit breaker instance
            start_time: Process start timestamp (for uptime calculation)
        """
        self.health_monitor = health_monitor
        self.circuit_breaker = circuit_breaker
        self.start_time = start_time

    async def handle_health_check(self) -> Dict[str, Any]:
        """Handle /mqtt/health endpoint.

        Returns:
            JSON response with health status
        """
        subscriber_health = await self.health_monitor.get_subscriber_health()
        publisher_health = await self.health_monitor.get_publisher_health()
        metrics = await self.health_monitor.get_metrics(self.circuit_breaker)

        uptime_seconds = time.time() - self.start_time

        return {
            "status": "healthy" if subscriber_health.connection_ok and publisher_health.connection_ok else "degraded",
            "uptime_seconds": int(uptime_seconds),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "subscriber": {
                "connected": subscriber_health.connection_ok,
                "queue_depth": subscriber_health.queue_depth,
                "total_messages": subscriber_health.total_messages_received,
                "last_message_age_seconds": int(time.time() - subscriber_health.last_message_ts)
                if subscriber_health.last_message_ts
                else None,
                "consecutive_failures": subscriber_health.consecutive_failures,
            },
            "publisher": {
                "connected": publisher_health.connection_ok,
                "total_batches": publisher_health.batch_count,
                "total_sends": publisher_health.total_sends,
                "successful_sends": publisher_health.total_sends - publisher_health.failed_sends,
                "failed_sends": publisher_health.failed_sends,
                "retries": publisher_health.retry_count,
                "last_send_age_seconds": int(time.time() - publisher_health.last_send_ts)
                if publisher_health.last_send_ts
                else None,
                "consecutive_failures": publisher_health.consecutive_failures,
            },
            "circuit_breaker": {
                "state": self.circuit_breaker.state.value,
                "consecutive_failures": self.circuit_breaker.consecutive_failures,
                "transitions": self.circuit_breaker.get_transitions()[-5:],  # Last 5 transitions
            },
        }

    async def handle_metrics(self) -> str:
        """Handle /mqtt/metrics endpoint.

        Returns:
            Prometheus text format metrics
        """
        metrics = await self.health_monitor.get_metrics(self.circuit_breaker)
        return PrometheusMetricsFormatter.format_metrics(metrics)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


def load_mqtt_config() -> Dict[str, Any]:
    """Load MQTT resilience configuration from environment.

    Returns:
        Configuration dict
    """
    return {
        "circuit_breaker_failure_threshold": int(os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "50")),
        "circuit_breaker_cooldown_secs": int(os.getenv("CIRCUIT_BREAKER_COOLDOWN_SECS", "300")),
        "heartbeat_interval_secs": int(os.getenv("HEARTBEAT_INTERVAL_SECS", "30")),
        "heartbeat_timeout_secs": int(os.getenv("HEARTBEAT_TIMEOUT_SECS", "60")),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Summary & Logging Helpers
# ─────────────────────────────────────────────────────────────────────────────


def log_publisher_summary(health: PublisherHealth) -> None:
    """Log publisher health summary.

    Args:
        health: Publisher health snapshot
    """
    successful = health.total_sends - health.failed_sends
    log.info(
        f"[MQTT] [metrics] Publisher: {health.batch_count} batches "
        f"({successful} successful, {health.retry_count} retried, {health.failed_sends} failed)"
    )


def log_subscriber_summary(health: SubscriberHealth) -> None:
    """Log subscriber health summary.

    Args:
        health: Subscriber health snapshot
    """
    age_str = (
        f"{time.time() - health.last_message_ts:.0f}s ago"
        if health.last_message_ts
        else "never"
    )
    log.info(
        f"[MQTT] [metrics] Subscriber: {health.total_messages_received} messages "
        f"(queue_depth={health.queue_depth}, last_message={age_str})"
    )
