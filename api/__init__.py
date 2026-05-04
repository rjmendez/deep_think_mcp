"""API module initialization and route registration.

This module provides the function to register all API routes with the FastMCP app.
Each submodule (health, reasoning, verify, etc.) registers its routes independently.
"""

import logging

log = logging.getLogger(__name__)


def register_routes(mcp):
    """Register all API routes with the FastMCP app.
    
    Args:
        mcp: FastMCP application instance
    """
    # Import route modules (deferred to avoid circular imports)
    from . import health, reasoning, verify, self_improvement, mcp_routes, mqtt
    
    log.info("Registering health routes...")
    health.register(mcp)
    
    log.info("Registering reasoning routes...")
    reasoning.register(mcp)
    
    log.info("Registering verify routes...")
    verify.register(mcp)
    
    log.info("Registering self-improvement routes...")
    self_improvement.register(mcp)
    
    log.info("Registering MCP routes...")
    mcp_routes.register(mcp)
    
    log.info("Registering MQTT routes...")
    mqtt.register(mcp)
    
    log.info("All routes registered successfully")
