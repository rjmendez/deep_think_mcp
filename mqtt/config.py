"""MQTT configuration management.

Loads and validates MQTT settings from environment variables.
"""

import os
from typing import Optional


class MQTTConfig:
    """MQTT subscriber and processor configuration loaded from environment."""
    
    def __init__(self) -> None:
        """Load MQTT configuration from environment variables."""
        self.enabled: bool = os.getenv("MQTT_ENABLE", "false").lower() in ("true", "1", "yes")
        self.broker_host: str = os.getenv("MQTT_HOST", "")
        self.broker_port: int = int(os.getenv("MQTT_PORT", "1883"))
        self.broker_user: str = os.getenv("MQTT_USERNAME", "dama")
        self.broker_password: str = os.getenv("MQTT_PASSWORD", "")
        self.use_tls: bool = os.getenv("MQTT_USE_TLS", "false").lower() in ("true", "1", "yes")
        
        self.queue_size: int = int(os.getenv("MQTT_SUBSCRIBER_QUEUE_SIZE", "1000"))
        self.batch_size: int = int(os.getenv("MQTT_BATCH_SIZE", "10"))
        self.batch_timeout_ms: int = int(os.getenv("MQTT_BATCH_TIMEOUT_MS", "5000"))
        self.batch_timeout_sec: float = self.batch_timeout_ms / 1000.0
        
        self.findings_topic_template: str = os.getenv(
            "MQTT_FINDINGS_TOPIC",
            "dama/colony/findings/{device_id}"
        )
    
    def validate(self) -> Optional[str]:
        """Validate configuration. Returns error message if invalid, else None."""
        if not self.enabled:
            return None  # Not enabled is valid
        
        if not self.broker_host:
            return "MQTT_HOST not set"
        if self.broker_port < 1 or self.broker_port > 65535:
            return f"MQTT_PORT out of range: {self.broker_port}"
        if self.batch_size < 1:
            return f"MQTT_BATCH_SIZE must be >= 1: {self.batch_size}"
        if self.batch_timeout_ms < 100:
            return f"MQTT_BATCH_TIMEOUT_MS must be >= 100: {self.batch_timeout_ms}"
        
        return None
    
    def __repr__(self) -> str:
        """Return string representation (hide password)."""
        return (
            f"MQTTConfig(host={self.broker_host}:{self.broker_port}, "
            f"user={self.broker_user}, enabled={self.enabled}, "
            f"batch_size={self.batch_size}, batch_timeout={self.batch_timeout_ms}ms)"
        )
