"""Example integration of MQTT Resilience Framework with subscriber and publisher.

Shows how to:
1. Initialize resilience components
2. Integrate with DAMAColonySubscriber
3. Setup HTTP health/metrics endpoints
4. Graceful shutdown

This is a reference implementation. Adapt to your actual subscriber/publisher classes.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiomqtt

from mqtt.resilience import (
    CircuitBreaker,
    HealthCheckHandler,
    HeartbeatPublisher,
    MQTTHealthMonitor,
    load_mqtt_config,
    log_publisher_summary,
    log_subscriber_summary,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# MQTT Publisher with Resilience
# ─────────────────────────────────────────────────────────────────────────────


class ResilientMQTTPublisher:
    """Publisher with circuit breaker and health monitoring integration.

    Example showing how to integrate resilience with publish operations.
    """

    def __init__(
        self,
        broker_host: str = "botnet.floppydicks.net",
        broker_port: int = 1883,
        broker_user: Optional[str] = None,
        broker_password: Optional[str] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        health_monitor: Optional[MQTTHealthMonitor] = None,
    ):
        """Initialize resilient publisher.

        Args:
            broker_host: MQTT broker hostname
            broker_port: MQTT broker port
            broker_user: MQTT username
            broker_password: MQTT password
            circuit_breaker: Circuit breaker instance
            health_monitor: Health monitor instance
        """
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.broker_user = broker_user or os.getenv("MQTT_USERNAME", "dama")
        self.broker_password = broker_password or os.getenv("MQTT_PASSWORD", "")

        self.circuit_breaker = circuit_breaker
        self.health_monitor = health_monitor

        self._mqtt_client: Optional[aiomqtt.Client] = None
        self._connected = False

        log.info(
            f"[MQTT] [publisher] Initialized resilient publisher: "
            f"{broker_host}:{broker_port}"
        )

    async def connect(self) -> None:
        """Connect to MQTT broker."""
        if self._connected:
            log.warning("[MQTT] [publisher] Already connected")
            return

        try:
            self._mqtt_client = aiomqtt.Client(
                hostname=self.broker_host,
                port=self.broker_port,
                username=self.broker_user,
                password=self.broker_password,
            )
            await self._mqtt_client.__aenter__()
            self._connected = True
            if self.health_monitor:
                await self.health_monitor.record_publisher_send(success=True)
            log.info("[MQTT] [publisher] Connected")
        except Exception as e:
            self._connected = False
            if self.health_monitor:
                await self.health_monitor.record_publisher_send(success=False)
            log.error(f"[MQTT] [publisher] Connection failed: {e}")
            raise

    async def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        if not self._connected or not self._mqtt_client:
            return

        try:
            await self._mqtt_client.__aexit__(None, None, None)
            self._connected = False
            log.info("[MQTT] [publisher] Disconnected")
        except Exception as e:
            log.error(f"[MQTT] [publisher] Disconnect error: {e}")

    async def publish(
        self,
        topic: str,
        message: str,
        qos: int = 1,
        retain: bool = False,
        max_retries: int = 3,
    ) -> bool:
        """Publish message with circuit breaker protection and retries.

        Args:
            topic: MQTT topic
            message: Message payload
            qos: Quality of service (0, 1, or 2)
            retain: Retain message on broker
            max_retries: Maximum retry attempts

        Returns:
            True if successful, False otherwise
        """
        if not self._connected:
            if self.health_monitor:
                await self.health_monitor.record_publisher_send(success=False)
            log.error("[MQTT] [publisher] Not connected")
            return False

        async def _publish():
            """Async publish wrapped for circuit breaker."""
            if not self._mqtt_client:
                raise RuntimeError("MQTT client not initialized")

            await self._mqtt_client.publish(
                topic=topic,
                payload=message,
                qos=qos,
                retain=retain,
            )

        # Try with retries
        for attempt in range(max_retries):
            try:
                # Use circuit breaker if available
                if self.circuit_breaker:
                    await self.circuit_breaker.call(_publish)
                else:
                    await _publish()

                # Record success
                if self.health_monitor:
                    await self.health_monitor.record_publisher_send(
                        success=True,
                        retried=(attempt > 0),
                    )
                return True

            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    log.warning(
                        f"[MQTT] [publisher] Publish failed (attempt {attempt + 1}), "
                        f"retry in {wait_time}s: {e}"
                    )
                    if self.health_monitor:
                        await self.health_monitor.record_publisher_send(success=False, retried=True)
                    await asyncio.sleep(wait_time)
                else:
                    log.error(
                        f"[MQTT] [publisher] Publish failed after {max_retries} attempts: {e}"
                    )
                    if self.health_monitor:
                        await self.health_monitor.record_publisher_send(success=False)
                    return False

        return False

    async def publish_batch(self, messages: list[tuple[str, str]]) -> int:
        """Publish batch of messages.

        Args:
            messages: List of (topic, message) tuples

        Returns:
            Number of successful publishes
        """
        if self.health_monitor:
            await self.health_monitor.record_publisher_batch(batch_size=len(messages))

        successful = 0
        for topic, message in messages:
            if await self.publish(topic, message):
                successful += 1

        return successful


# ─────────────────────────────────────────────────────────────────────────────
# Resilience Integration Manager
# ─────────────────────────────────────────────────────────────────────────────


class MQTTResilienceManager:
    """Manages all resilience components and provides unified interface.

    Initializes, starts, and coordinates:
    - Circuit breaker
    - Health monitoring
    - Heartbeat publishing
    - Health check endpoints
    - Graceful shutdown
    """

    def __init__(self):
        """Initialize resilience manager."""
        self.config = load_mqtt_config()

        # Initialize components
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=self.config["circuit_breaker_failure_threshold"],
            cooldown_secs=self.config["circuit_breaker_cooldown_secs"],
            name="mqtt",
        )

        self.health_monitor = MQTTHealthMonitor(
            heartbeat_interval_secs=self.config["heartbeat_interval_secs"],
            heartbeat_timeout_secs=self.config["heartbeat_timeout_secs"],
        )

        self.publisher: Optional[ResilientMQTTPublisher] = None
        self.heartbeat: Optional[HeartbeatPublisher] = None
        self.health_check_handler: Optional[HealthCheckHandler] = None

        self._start_time = time.time()
        self._monitoring_task: Optional[asyncio.Task] = None
        self._running = False

        log.info("[MQTT] [resilience] Initialized manager")

    async def initialize(
        self,
        broker_host: str = "botnet.floppydicks.net",
        broker_port: int = 1883,
        broker_user: Optional[str] = None,
        broker_password: Optional[str] = None,
    ) -> None:
        """Initialize all components.

        Args:
            broker_host: MQTT broker hostname
            broker_port: MQTT broker port
            broker_user: MQTT username
            broker_password: MQTT password
        """
        # Initialize publisher
        self.publisher = ResilientMQTTPublisher(
            broker_host=broker_host,
            broker_port=broker_port,
            broker_user=broker_user,
            broker_password=broker_password,
            circuit_breaker=self.circuit_breaker,
            health_monitor=self.health_monitor,
        )

        # Initialize heartbeat
        self.heartbeat = HeartbeatPublisher(
            publish_fn=self._publish_heartbeat,
            interval_secs=self.config["heartbeat_interval_secs"],
            topic="dama/colony/heartbeat",
        )

        # Initialize health check handler
        self.health_check_handler = HealthCheckHandler(
            health_monitor=self.health_monitor,
            circuit_breaker=self.circuit_breaker,
            start_time=self._start_time,
        )

        log.info("[MQTT] [resilience] Manager initialized")

    async def start(self) -> None:
        """Start all resilience components.

        Raises:
            RuntimeError: If not initialized
        """
        if not self.publisher or not self.heartbeat:
            raise RuntimeError("Manager not initialized")

        if self._running:
            log.warning("[MQTT] [resilience] Already running")
            return

        self._running = True

        # Connect publisher
        await self.publisher.connect()

        # Start heartbeat
        await self.heartbeat.start(self.health_monitor)

        # Start monitoring task
        self._monitoring_task = asyncio.create_task(self._run_monitoring())

        log.info("[MQTT] [resilience] Started all components")

    async def stop(self) -> None:
        """Stop all resilience components gracefully."""
        if not self._running:
            return

        self._running = False

        # Stop monitoring
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass

        # Stop heartbeat
        if self.heartbeat:
            await self.heartbeat.stop()

        # Disconnect publisher
        if self.publisher:
            await self.publisher.disconnect()

        # Log final metrics
        if self.health_monitor:
            pub_health = await self.health_monitor.get_publisher_health()
            sub_health = await self.health_monitor.get_subscriber_health()
            log_publisher_summary(pub_health)
            log_subscriber_summary(sub_health)

        log.info("[MQTT] [resilience] Stopped all components")

    async def _publish_heartbeat(self, topic: str, message: str) -> None:
        """Internal heartbeat publisher."""
        if self.publisher:
            await self.publisher.publish(topic, message, max_retries=2)

    async def _run_monitoring(self) -> None:
        """Background monitoring task."""
        while self._running:
            try:
                await asyncio.sleep(30)

                if not self._running:
                    break

                # Check stale heartbeat
                stale_msg = await self.health_monitor.check_stale_heartbeat()
                if stale_msg:
                    log.warning(stale_msg)

                # Log summary
                pub_health = await self.health_monitor.get_publisher_health()
                sub_health = await self.health_monitor.get_subscriber_health()

                if pub_health.total_sends > 0:
                    log_publisher_summary(pub_health)
                if sub_health.total_messages_received > 0:
                    log_subscriber_summary(sub_health)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[MQTT] [resilience] Monitoring error: {e}")

    async def get_health(self) -> dict:
        """Get health check response.

        Returns:
            Health check JSON dict

        Raises:
            RuntimeError: If not initialized
        """
        if not self.health_check_handler:
            raise RuntimeError("Manager not initialized")

        return await self.health_check_handler.handle_health_check()

    async def get_metrics(self) -> str:
        """Get metrics response.

        Returns:
            Prometheus text format metrics

        Raises:
            RuntimeError: If not initialized
        """
        if not self.health_check_handler:
            raise RuntimeError("Manager not initialized")

        return await self.health_check_handler.handle_metrics()


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Integration Example
# ─────────────────────────────────────────────────────────────────────────────


async def setup_fastapi_endpoints(app, manager: MQTTResilienceManager) -> None:
    """Setup FastAPI endpoints for resilience monitoring.

    Args:
        app: FastAPI app instance
        manager: MQTTResilienceManager instance
    """
    try:
        from fastapi.responses import PlainTextResponse
    except ImportError:
        log.warning(
            "[MQTT] [resilience] FastAPI not available, "
            "skipping endpoint setup"
        )
        return

    @app.get("/mqtt/health")
    async def health():
        """Health check endpoint."""
        return await manager.get_health()

    @app.get("/mqtt/metrics")
    async def metrics():
        """Prometheus metrics endpoint."""
        text = await manager.get_metrics()
        return PlainTextResponse(text)

    log.info("[MQTT] [resilience] FastAPI endpoints registered")


# ─────────────────────────────────────────────────────────────────────────────
# Example Main
# ─────────────────────────────────────────────────────────────────────────────


async def main():
    """Example usage of MQTT resilience framework."""
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Create manager
    manager = MQTTResilienceManager()

    # Initialize with broker details from environment
    await manager.initialize(
        broker_host=os.getenv("MQTT_HOST", "botnet.floppydicks.net"),
        broker_port=int(os.getenv("MQTT_PORT", "1883")),
        broker_user=os.getenv("MQTT_USERNAME"),
        broker_password=os.getenv("MQTT_PASSWORD"),
    )

    # Start resilience components
    await manager.start()

    try:
        # Example: Publish some messages
        if manager.publisher:
            await manager.publisher.publish(
                "dama/test/message",
                json.dumps({"hello": "world"}),
            )

        # Run for a bit
        await asyncio.sleep(60)

    finally:
        # Graceful shutdown
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
