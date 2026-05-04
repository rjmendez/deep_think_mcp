"""MQTT integration module for deep_think_mcp.

Provides MQTT subscriber, publisher, and resilience components for
integrating MQTT-based telemetry with the deep_think reasoning engine.

Components:
    - subscriber: DAMAColonySubscriber and claims processing
    - publisher: Findings publisher with batching and persistence
    - resilience: Circuit breaker, health monitoring, and metrics
    - config: MQTT configuration management
    - models: Data structures (Finding, etc.)
    - utils: Helper functions and retry logic
"""

from .config import MQTTConfig
from .models import Finding, Confirmation
from .subscriber import (
    MQTTClaimsProcessor,
    mqtt_startup,
    mqtt_shutdown,
    setup_signal_handlers,
    get_mqtt_processor,
    get_mqtt_subscriber,
    is_mqtt_enabled,
)
from .publisher import MQTTFindingsPublisher, FindingsPersistenceStore
from .feedback_store import FeedbackStore
from .resilience import (
    CircuitBreakerState,
    CircuitBreaker,
    MQTTHealthMonitor,
    MetricsSnapshot,
    PrometheusMetricsFormatter,
    PublisherHealth,
    SubscriberHealth,
    HeartbeatPublisher,
    HealthCheckHandler,
)

__all__ = [
    "MQTTConfig",
    "Finding",
    "Confirmation",
    "MQTTClaimsProcessor",
    "mqtt_startup",
    "mqtt_shutdown",
    "setup_signal_handlers",
    "get_mqtt_processor",
    "get_mqtt_subscriber",
    "is_mqtt_enabled",
    "MQTTFindingsPublisher",
    "FindingsPersistenceStore",
    "FeedbackStore",
    "CircuitBreakerState",
    "CircuitBreaker",
    "MQTTHealthMonitor",
    "MetricsSnapshot",
    "PrometheusMetricsFormatter",
    "PublisherHealth",
    "SubscriberHealth",
    "HeartbeatPublisher",
    "HealthCheckHandler",
]
