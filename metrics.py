"""Metrics collection and reporting for ground_truth.py.

Provides counters, histograms, and gauges for monitoring validation performance,
Nova/MQTT availability, and cache efficiency in production.
"""

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class HistogramBucket:
    """Histogram bucket with configurable boundaries."""
    boundaries_ms: List[int] = field(default_factory=lambda: [10, 50, 100, 500, 1000, 5000])
    buckets: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sum_ms: int = 0
    count: int = 0

    def observe(self, value_ms: float) -> None:
        """Record an observation in the histogram."""
        self.sum_ms += int(value_ms)
        self.count += 1
        for boundary in self.boundaries_ms:
            if value_ms <= boundary:
                self.buckets[f"le_{boundary}"] += 1

    def mean_ms(self) -> float:
        """Calculate mean latency."""
        return self.sum_ms / self.count if self.count > 0 else 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize histogram to dict."""
        return {
            "count": self.count,
            "sum_ms": self.sum_ms,
            "mean_ms": self.mean_ms(),
            "buckets": dict(self.buckets),
        }


@dataclass
class Metrics:
    """Thread-safe metrics collector."""
    
    # Validation metrics
    validation_count: int = 0
    validation_latency_ms: HistogramBucket = field(default_factory=HistogramBucket)
    validation_errors: int = 0
    
    # Nova metrics
    nova_latency_ms: HistogramBucket = field(default_factory=HistogramBucket)
    nova_errors: int = 0
    nova_timeouts: int = 0
    
    # MQTT metrics
    mqtt_message_count: int = 0
    mqtt_connection_failures: int = 0
    mqtt_reconnects: int = 0
    
    # Cache metrics
    cache_hit_rate: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_evictions: int = 0
    
    # Contradiction detection
    contradiction_count: int = 0
    contradiction_types: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Timeout metrics
    timeout_count: int = 0
    timeout_by_component: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Orphaned job metrics
    orphaned_jobs_detected: int = 0
    orphaned_jobs_requeued: int = 0
    
    # Timestamps
    start_time_utc: datetime = field(default_factory=lambda: datetime.utcnow())
    last_reset_utc: datetime = field(default_factory=lambda: datetime.utcnow())
    
    # Thread safety
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def increment_validation(self, latency_ms: float = 0.0) -> None:
        """Record a validation attempt."""
        with self._lock:
            self.validation_count += 1
            if latency_ms > 0:
                self.validation_latency_ms.observe(latency_ms)

    def increment_validation_error(self) -> None:
        """Record a validation error."""
        with self._lock:
            self.validation_errors += 1

    def record_nova_latency(self, latency_ms: float) -> None:
        """Record Nova API call latency."""
        with self._lock:
            self.nova_latency_ms.observe(latency_ms)

    def increment_nova_error(self) -> None:
        """Record Nova error."""
        with self._lock:
            self.nova_errors += 1

    def increment_nova_timeout(self) -> None:
        """Record Nova timeout."""
        with self._lock:
            self.nova_timeouts += 1

    def increment_mqtt_message(self) -> None:
        """Record MQTT message received/sent."""
        with self._lock:
            self.mqtt_message_count += 1

    def increment_mqtt_connection_failure(self) -> None:
        """Record MQTT connection failure."""
        with self._lock:
            self.mqtt_connection_failures += 1

    def increment_mqtt_reconnect(self) -> None:
        """Record MQTT reconnection."""
        with self._lock:
            self.mqtt_reconnects += 1

    def record_cache_hit(self) -> None:
        """Record cache hit."""
        with self._lock:
            self.cache_hits += 1
            self._update_cache_hit_rate()

    def record_cache_miss(self) -> None:
        """Record cache miss."""
        with self._lock:
            self.cache_misses += 1
            self._update_cache_hit_rate()

    def increment_cache_eviction(self) -> None:
        """Record cache eviction."""
        with self._lock:
            self.cache_evictions += 1

    def record_contradiction(self, contradiction_type: str = "unknown") -> None:
        """Record contradiction detection."""
        with self._lock:
            self.contradiction_count += 1
            self.contradiction_types[contradiction_type] += 1

    def record_timeout(self, component: str = "unknown") -> None:
        """Record timeout by component."""
        with self._lock:
            self.timeout_count += 1
            self.timeout_by_component[component] += 1

    def increment_orphaned_jobs_detected(self) -> None:
        """Record detection of an orphaned job."""
        with self._lock:
            self.orphaned_jobs_detected += 1

    def increment_orphaned_jobs_requeued(self) -> None:
        """Record successful requeue of an orphaned job."""
        with self._lock:
            self.orphaned_jobs_requeued += 1

    def _update_cache_hit_rate(self) -> None:
        """Update cache hit rate (call with lock held)."""
        total = self.cache_hits + self.cache_misses
        if total > 0:
            self.cache_hit_rate = self.cache_hits / total
        else:
            self.cache_hit_rate = 0.0

    def get_uptime_seconds(self) -> float:
        """Get uptime since start."""
        return (datetime.utcnow() - self.start_time_utc).total_seconds()

    def reset(self) -> None:
        """Reset all metrics to zero."""
        with self._lock:
            self.validation_count = 0
            self.validation_latency_ms = HistogramBucket()
            self.validation_errors = 0
            self.nova_latency_ms = HistogramBucket()
            self.nova_errors = 0
            self.nova_timeouts = 0
            self.mqtt_message_count = 0
            self.mqtt_connection_failures = 0
            self.mqtt_reconnects = 0
            self.cache_hit_rate = 0.0
            self.cache_hits = 0
            self.cache_misses = 0
            self.cache_evictions = 0
            self.contradiction_count = 0
            self.contradiction_types = defaultdict(int)
            self.timeout_count = 0
            self.timeout_by_component = defaultdict(int)
            self.last_reset_utc = datetime.utcnow()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize metrics to dict for export."""
        with self._lock:
            return {
                "validation": {
                    "total": self.validation_count,
                    "errors": self.validation_errors,
                    "latency_ms": self.validation_latency_ms.to_dict(),
                },
                "nova": {
                    "latency_ms": self.nova_latency_ms.to_dict(),
                    "errors": self.nova_errors,
                    "timeouts": self.nova_timeouts,
                },
                "mqtt": {
                    "message_count": self.mqtt_message_count,
                    "connection_failures": self.mqtt_connection_failures,
                    "reconnects": self.mqtt_reconnects,
                },
                "cache": {
                    "hit_rate": self.cache_hit_rate,
                    "hits": self.cache_hits,
                    "misses": self.cache_misses,
                    "evictions": self.cache_evictions,
                },
                "contradictions": {
                    "total": self.contradiction_count,
                    "by_type": dict(self.contradiction_types),
                },
                "timeouts": {
                    "total": self.timeout_count,
                    "by_component": dict(self.timeout_by_component),
                },
                "uptime_seconds": self.get_uptime_seconds(),
                "start_time_utc": self.start_time_utc.isoformat(),
                "last_reset_utc": self.last_reset_utc.isoformat(),
            }

    def to_prometheus_format(self) -> str:
        """Export metrics in Prometheus text format."""
        lines = []
        
        with self._lock:
            lines.append(f"# HELP ground_truth_validations_total Total validation attempts")
            lines.append(f"# TYPE ground_truth_validations_total counter")
            lines.append(f"ground_truth_validations_total {self.validation_count}")
            
            lines.append(f"# HELP ground_truth_validation_errors_total Validation errors")
            lines.append(f"# TYPE ground_truth_validation_errors_total counter")
            lines.append(f"ground_truth_validation_errors_total {self.validation_errors}")
            
            lines.append(f"# HELP ground_truth_nova_latency_seconds Nova API latency")
            lines.append(f"# TYPE ground_truth_nova_latency_seconds summary")
            lines.append(f"ground_truth_nova_latency_seconds_sum {self.nova_latency_ms.sum_ms / 1000.0}")
            lines.append(f"ground_truth_nova_latency_seconds_count {self.nova_latency_ms.count}")
            
            lines.append(f"# HELP ground_truth_mqtt_messages_total MQTT messages processed")
            lines.append(f"# TYPE ground_truth_mqtt_messages_total counter")
            lines.append(f"ground_truth_mqtt_messages_total {self.mqtt_message_count}")
            
            lines.append(f"# HELP ground_truth_cache_hit_rate Cache hit rate (0-1)")
            lines.append(f"# TYPE ground_truth_cache_hit_rate gauge")
            lines.append(f"ground_truth_cache_hit_rate {self.cache_hit_rate}")
            
            lines.append(f"# HELP ground_truth_contradictions_total Contradictions detected")
            lines.append(f"# TYPE ground_truth_contradictions_total counter")
            lines.append(f"ground_truth_contradictions_total {self.contradiction_count}")
            
            lines.append(f"# HELP ground_truth_timeouts_total Timeouts by component")
            lines.append(f"# TYPE ground_truth_timeouts_total counter")
            lines.append(f"ground_truth_timeouts_total {self.timeout_count}")
            
            lines.append(f"# HELP ground_truth_orphaned_jobs_detected_total Orphaned jobs detected")
            lines.append(f"# TYPE ground_truth_orphaned_jobs_detected_total counter")
            lines.append(f"ground_truth_orphaned_jobs_detected_total {self.orphaned_jobs_detected}")
            
            lines.append(f"# HELP ground_truth_orphaned_jobs_requeued_total Orphaned jobs requeued")
            lines.append(f"# TYPE ground_truth_orphaned_jobs_requeued_total counter")
            lines.append(f"ground_truth_orphaned_jobs_requeued_total {self.orphaned_jobs_requeued}")
            
            lines.append(f"# HELP ground_truth_uptime_seconds Process uptime")
            lines.append(f"# TYPE ground_truth_uptime_seconds gauge")
            lines.append(f"ground_truth_uptime_seconds {self.get_uptime_seconds()}")

        return "\n".join(lines)


# Global metrics instance
_metrics = Metrics()


def get_metrics() -> Metrics:
    """Get the global metrics instance."""
    return _metrics


def reset_metrics() -> None:
    """Reset all metrics (useful for testing or periodic resets)."""
    _metrics.reset()
