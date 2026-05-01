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

from mqtt.config import MQTTConfig
from mqtt.models import Finding
from mqtt.subscriber import (
    MQTTClaimsProcessor,
    mqtt_startup,
    mqtt_shutdown,
    setup_signal_handlers,
    get_mqtt_processor,
    get_mqtt_subscriber,
    is_mqtt_enabled,
)
from mqtt.publisher import MQTTFindingsPublisher, FindingsPersistenceStore
from mqtt.resilience import (
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
    "MQTTClaimsProcessor",
    "mqtt_startup",
    "mqtt_shutdown",
    "setup_signal_handlers",
    "get_mqtt_processor",
    "get_mqtt_subscriber",
    "is_mqtt_enabled",
    "MQTTFindingsPublisher",
    "FindingsPersistenceStore",
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
