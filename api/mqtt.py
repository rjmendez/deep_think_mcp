"""MQTT adapter health and metrics endpoints."""

import logging
from datetime import datetime

log = logging.getLogger(__name__)


def register(mcp):
    """Register MQTT health and metrics routes."""
    
    @mcp.tool()
    async def mqtt_health() -> dict:
        """Get MQTT engine health status and metrics.
        
        Returns:
            Health status, circuit breaker state, message counts, error logs, and connection status.
        """
        if not hasattr(mcp, "mqtt_adapter"):
            return {
                "status": "not_initialized",
                "message": "MQTT adapter not initialized (MQTT_ENABLE=false?)"
            }
        
        adapter = mcp.mqtt_adapter
        return adapter.get_health()
    
    @mcp.tool()
    async def mqtt_metrics() -> dict:
        """Get detailed MQTT metrics for monitoring and observability.
        
        Returns:
            Messages received/published, deep_think runs, failures, circuit breaker trips, etc.
        """
        if not hasattr(mcp, "mqtt_adapter"):
            return {
                "status": "not_initialized",
                "metrics": {}
            }
        
        adapter = mcp.mqtt_adapter
        health = adapter.get_health()
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "circuit_breaker_state": health["circuit_breaker"],
            "metrics": health["metrics"],
            "connections": health["connections"],
        }
