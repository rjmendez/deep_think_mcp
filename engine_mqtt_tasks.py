"""MQTT Engine Integration: Claims → Deep Think → Findings.

Bridges MQTT subscriber, deep_think reasoning, and publisher with resilience.

Components:
- MQTTEngineAdapter: Main integration class
- Circuit breaker: Pause processing on >50% consecutive failures
- Batch processing: Collect claims, send to deep_think, publish findings
- Error recovery: Reconnect on broker failures, persist failed publishes to SQLite
- Health monitoring: Track metrics and expose /mqtt/health endpoint
"""

import asyncio
import json
import logging
import os
import signal
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class CircuitBreakerState(Enum):
    """Circuit breaker state machine."""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Fail fast (threshold exceeded)
    HALF_OPEN = "half_open" # Testing recovery


@dataclass
class MQTTMetrics:
    """Track MQTT health metrics."""
    messages_received: int = 0
    messages_published: int = 0
    deep_think_runs: int = 0
    deep_think_failures: int = 0
    publish_failures: int = 0
    publish_retries: int = 0
    circuit_breaker_trips: int = 0
    last_subscriber_error: Optional[str] = None
    last_publisher_error: Optional[str] = None
    last_circuit_trip: Optional[datetime] = None
    subscriber_connected: bool = False
    publisher_connected: bool = False


@dataclass
class MQTTConfig:
    """MQTT configuration from environment."""
    enable: bool
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    subscriber_batch_size: int
    publisher_batch_size: int
    publisher_batch_timeout_ms: int
    circuit_breaker_failure_threshold: int
    heartbeat_interval_secs: int

    @classmethod
    def from_env(cls) -> "MQTTConfig":
        """Load configuration from environment variables."""
        return cls(
            enable=os.getenv("MQTT_ENABLE", "false").lower() == "true",
            host=os.getenv("MQTT_HOST", "botnet.floppydicks.net"),
            port=int(os.getenv("MQTT_PORT", "1883")),
            username=os.getenv("MQTT_USERNAME", "dama"),
            password=os.getenv("MQTT_PASSWORD", ""),
            use_tls=os.getenv("MQTT_USE_TLS", "false").lower() == "true",
            subscriber_batch_size=int(os.getenv("SUBSCRIBER_BATCH_SIZE", "10")),
            publisher_batch_size=int(os.getenv("PUBLISHER_BATCH_SIZE", "10")),
            publisher_batch_timeout_ms=int(os.getenv("PUBLISHER_BATCH_TIMEOUT_MS", "5000")),
            circuit_breaker_failure_threshold=int(os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "50")),
            heartbeat_interval_secs=int(os.getenv("HEARTBEAT_INTERVAL_SECS", "30")),
        )


class MQTTEngineAdapter:
    """Integrate MQTT subscriber, deep_think reasoning, and publisher.
    
    Lifecycle:
    - start_mqtt(): Initialize connections, start background tasks
    - process_batch(): Main loop - get claims, run deep_think, publish findings
    - stop_mqtt(): Graceful shutdown (flush batches, close connections)
    
    Error handling:
    - Subscriber failures: Log and retry with backoff
    - Publisher failures: Persist to SQLite, retry on next batch
    - Deep think timeouts: Skip batch, move to next
    - Circuit breaker: Pause processing if >50% consecutive failures
    """
    
    def __init__(
        self,
        config: Optional[MQTTConfig] = None,
        db_path: str = "mqtt_failures.db",
        deep_think_fn: Optional[Callable] = None,
    ):
        """Initialize MQTT engine adapter.
        
        Args:
            config: MQTT configuration (from env if None)
            db_path: Path to SQLite database for failed publishes
            deep_think_fn: Async function to call deep_think_passes
        """
        self.config = config or MQTTConfig.from_env()
        self.db_path = db_path
        self.deep_think_fn = deep_think_fn
        
        self.metrics = MQTTMetrics()
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._claim_queue: deque = deque(maxlen=self.config.subscriber_batch_size * 2)
        self._finding_batch: list[dict] = []
        self._finding_batch_timer: Optional[asyncio.Task] = None
        
        # Circuit breaker
        self.circuit_breaker_state = CircuitBreakerState.CLOSED
        self._consecutive_failures = 0
        self._last_circuit_reset: datetime = datetime.now(timezone.utc)
        
        # MQTT clients (async)
        self._subscriber = None
        self._publisher = None
        
        # Backoff retry state
        self._subscriber_backoff_secs = 1
        self._max_backoff_secs = 60
        
        # Signal handlers
        self._signal_handlers_registered = False
    
    @staticmethod
    def _init_db(db_path: str) -> None:
        """Initialize SQLite database for failed publishes."""
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS failed_publishes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                retry_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()
    
    async def _get_failed_publishes(self, limit: int = 10) -> list[tuple]:
        """Retrieve failed publishes from database."""
        loop = asyncio.get_event_loop()
        def query():
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, topic, payload, retry_count FROM failed_publishes ORDER BY created_at ASC LIMIT ?",
                (limit,)
            )
            rows = cursor.fetchall()
            conn.close()
            return rows
        return await loop.run_in_executor(None, query)
    
    async def _remove_failed_publish(self, publish_id: int) -> None:
        """Remove a failed publish from database."""
        loop = asyncio.get_event_loop()
        def delete():
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM failed_publishes WHERE id = ?", (publish_id,))
            conn.commit()
            conn.close()
        await loop.run_in_executor(None, delete)
    
    async def _save_failed_publish(self, topic: str, payload: dict, retry_count: int = 0) -> None:
        """Save failed publish to database for retry."""
        loop = asyncio.get_event_loop()
        def insert():
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO failed_publishes (topic, payload, retry_count) VALUES (?, ?, ?)",
                (topic, json.dumps(payload), retry_count)
            )
            conn.commit()
            conn.close()
        await loop.run_in_executor(None, insert)
    
    def _register_signal_handlers(self) -> None:
        """Register SIGTERM/SIGINT handlers for graceful shutdown."""
        if self._signal_handlers_registered:
            return
        
        def handle_signal(signum, frame):
            log.info(f"[MQTT] Received signal {signum}, initiating graceful shutdown")
            if self._running:
                asyncio.create_task(self.stop_mqtt())
        
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
        self._signal_handlers_registered = True
    
    async def start_mqtt(self) -> bool:
        """Initialize subscriber, publisher, and health monitor.
        
        Returns:
            True if MQTT enabled and initialized, False otherwise.
        """
        if not self.config.enable:
            log.info("[MQTT] MQTT_ENABLE=false, skipping MQTT initialization")
            return False
        
        try:
            self._init_db(self.db_path)
            self._register_signal_handlers()
            self._running = True
            
            # Start subscriber task
            subscriber_task = asyncio.create_task(self._subscriber_loop())
            self._tasks.append(subscriber_task)
            
            # Start publisher task
            publisher_task = asyncio.create_task(self._publisher_loop())
            self._tasks.append(publisher_task)
            
            # Start main batch processing task
            processor_task = asyncio.create_task(self._process_batch_loop())
            self._tasks.append(processor_task)
            
            # Start health monitor task
            health_task = asyncio.create_task(self._health_monitor_loop())
            self._tasks.append(health_task)
            
            log.info(
                "[MQTT] Engine initialized: "
                f"subscriber={{{self.config.host}:{self.config.port}}}, "
                f"publisher enabled, circuit breaker active at {self.config.circuit_breaker_failure_threshold}%"
            )
            return True
        
        except Exception as e:
            log.error(f"[MQTT] Failed to initialize: {e}", exc_info=True)
            self._running = False
            return False
    
    async def stop_mqtt(self) -> None:
        """Graceful shutdown: flush batches, close connections, cancel tasks."""
        log.info("[MQTT] Starting graceful shutdown")
        self._running = False
        
        # Flush pending findings
        if self._finding_batch:
            await self._flush_finding_batch()
        
        # Cancel all background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        
        # Wait for all tasks to complete
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # Close MQTT clients
        if self._subscriber:
            try:
                await self._subscriber.disconnect()
            except Exception as e:
                log.warning(f"[MQTT] Error closing subscriber: {e}")
        
        if self._publisher:
            try:
                await self._publisher.disconnect()
            except Exception as e:
                log.warning(f"[MQTT] Error closing publisher: {e}")
        
        log.info("[MQTT] Graceful shutdown complete")
    
    async def _subscriber_loop(self) -> None:
        """Continuously connect to MQTT broker and subscribe to claim topics.
        
        Topics:
        - dama/+/claims: Device telemetry claims to analyze
        
        On connection: Set connected=True, reset backoff
        On disconnect: Log, implement exponential backoff retry
        """
        import aiomqtt
        
        while self._running:
            try:
                log.info(f"[MQTT] Subscriber connecting to {self.config.host}:{self.config.port}")
                
                self._subscriber = aiomqtt.Client(
                    hostname=self.config.host,
                    port=self.config.port,
                    username=self.config.username,
                    password=self.config.password,
                    keepalive=30,
                    tls_insecure=True,  # Allow self-signed certs
                )
                
                await self._subscriber.connect()
                await self._subscriber.subscribe("dama/+/claims")
                
                self.metrics.subscriber_connected = True
                self._subscriber_backoff_secs = 1
                log.info("[MQTT] Subscriber connected and subscribed to dama/+/claims")
                
                # Message loop
                async with self._subscriber.messages() as messages:
                    async for message in messages:
                        if not self._running:
                            break
                        
                        try:
                            payload = json.loads(message.payload.decode())
                            self._claim_queue.append(payload)
                            self.metrics.messages_received += 1
                        except json.JSONDecodeError:
                            log.debug(f"[MQTT] Skipping malformed JSON from {message.topic}")
                        except Exception as e:
                            log.warning(f"[MQTT] Error processing message: {e}")
            
            except asyncio.CancelledError:
                log.debug("[MQTT] Subscriber loop cancelled")
                break
            
            except Exception as e:
                self.metrics.subscriber_connected = False
                self.metrics.last_subscriber_error = str(e)
                log.warning(
                    f"[MQTT] Subscriber error (will retry in {self._subscriber_backoff_secs}s): {e}"
                )
                
                # Exponential backoff
                await asyncio.sleep(self._subscriber_backoff_secs)
                self._subscriber_backoff_secs = min(
                    self._subscriber_backoff_secs * 2,
                    self._max_backoff_secs
                )
    
    async def _process_batch_loop(self) -> None:
        """Main loop: get claims from queue, send to deep_think, batch findings.
        
        Circuit breaker logic:
        - CLOSED (normal): Process batches
        - OPEN (too many failures): Reject new batches for 60 seconds
        - HALF_OPEN (testing): Process one batch, if success → CLOSED, if fail → OPEN
        """
        batch_timeout = self.config.publisher_batch_timeout_ms / 1000.0
        
        while self._running:
            try:
                # Circuit breaker: OPEN state
                if self.circuit_breaker_state == CircuitBreakerState.OPEN:
                    now = datetime.now(timezone.utc)
                    elapsed = (now - self._last_circuit_reset).total_seconds()
                    if elapsed > 60:  # Reset after 60 seconds
                        log.info("[MQTT] Circuit breaker: Attempting recovery (HALF_OPEN)")
                        self.circuit_breaker_state = CircuitBreakerState.HALF_OPEN
                        self._consecutive_failures = 0
                    else:
                        await asyncio.sleep(1)
                        continue
                
                # Collect batch from queue
                batch_size = self.config.subscriber_batch_size
                batch = []
                for _ in range(batch_size):
                    if self._claim_queue:
                        batch.append(self._claim_queue.popleft())
                    else:
                        break
                
                if not batch:
                    await asyncio.sleep(0.1)
                    continue
                
                # Process batch through deep_think
                try:
                    await self._process_batch(batch)
                    
                    # Success: close circuit breaker if in HALF_OPEN
                    if self.circuit_breaker_state == CircuitBreakerState.HALF_OPEN:
                        log.info("[MQTT] Circuit breaker: Recovered (CLOSED)")
                        self.circuit_breaker_state = CircuitBreakerState.CLOSED
                    
                    self._consecutive_failures = 0
                
                except asyncio.TimeoutError:
                    log.warning("[MQTT] Deep think timeout for this batch, moving to next")
                    self._increment_failures()
                
                except Exception as e:
                    log.warning(f"[MQTT] Batch processing error: {e}")
                    self._increment_failures()
            
            except asyncio.CancelledError:
                log.debug("[MQTT] Batch processor loop cancelled")
                break
            
            except Exception as e:
                log.error(f"[MQTT] Unexpected batch processor error: {e}", exc_info=True)
                await asyncio.sleep(1)
    
    def _increment_failures(self) -> None:
        """Increment failure counter and trip circuit breaker if threshold exceeded."""
        self._consecutive_failures += 1
        
        threshold = self.config.circuit_breaker_failure_threshold
        if self._consecutive_failures * 100 // max(1, self._consecutive_failures + 1) >= threshold:
            log.error(
                f"[MQTT] Circuit breaker OPEN: {self._consecutive_failures} consecutive failures "
                f"exceeded {threshold}% threshold"
            )
            self.circuit_breaker_state = CircuitBreakerState.OPEN
            self._last_circuit_reset = datetime.now(timezone.utc)
            self.metrics.circuit_breaker_trips += 1
            self.metrics.last_circuit_trip = self._last_circuit_reset
    
    async def _process_batch(self, claims: list[dict]) -> None:
        """Process a batch of claims through deep_think and queue findings.
        
        Args:
            claims: List of claim dicts from subscriber queue
            
        Raises:
            asyncio.TimeoutError: If deep_think takes too long
            Exception: On unexpected errors
        """
        if not self.deep_think_fn:
            log.warning("[MQTT] deep_think_fn not set, skipping batch processing")
            return
        
        for claim in claims:
            try:
                # Format claim as question for deep_think
                question = self._format_claim_as_question(claim)
                
                # Run deep_think (with local-only enforcement for MQTT)
                result_json = await asyncio.wait_for(
                    self.deep_think_fn(
                        question=question,
                        passes=3,
                        task_class="investigation",
                        data_policy="local",  # MQTT must use local-only (Ollama)
                        force_local_models=True,
                        device_id=claim.get("device_id", "unknown"),
                    ),
                    timeout=30.0,
                )
                
                self.metrics.deep_think_runs += 1
                
                # Parse result and extract findings
                finding = self._extract_finding(claim, result_json)
                self._finding_batch.append(finding)
                
                # Start batch timer if needed
                if len(self._finding_batch) == 1:
                    self._finding_batch_timer = asyncio.create_task(
                        self._finding_batch_timeout()
                    )
                
                # Flush if batch is full
                if len(self._finding_batch) >= self.config.publisher_batch_size:
                    await self._flush_finding_batch()
            
            except asyncio.TimeoutError:
                self.metrics.deep_think_failures += 1
                log.warning(f"[MQTT] Deep think timeout for claim: {claim.get('claim_id', 'unknown')}")
                raise
            
            except Exception as e:
                self.metrics.deep_think_failures += 1
                log.warning(f"[MQTT] Error processing claim: {e}")
    
    def _format_claim_as_question(self, claim: dict) -> str:
        """Convert claim dict to natural language question for deep_think."""
        device_id = claim.get("device_id", "unknown")
        claim_text = claim.get("text", "")
        sensor_data = claim.get("sensor_data", {})
        
        question = f"[Device: {device_id}] {claim_text}"
        if sensor_data:
            question += f"\n\nSensor data: {json.dumps(sensor_data, indent=2)}"
        
        return question
    
    def _extract_finding(self, claim: dict, result_json: str) -> dict:
        """Extract finding from deep_think result."""
        try:
            result = json.loads(result_json)
            final_answer = result.get("final_answer", "")
            confidence = result.get("confidence", 0)
        except Exception as e:
            log.warning(f"[MQTT] Error parsing deep_think result: {e}")
            final_answer = ""
            confidence = 0
        
        return {
            "claim_id": claim.get("claim_id", "unknown"),
            "device_id": claim.get("device_id", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "original_claim": claim.get("text", ""),
            "analysis": final_answer,
            "confidence": confidence,
        }
    
    async def _finding_batch_timeout(self) -> None:
        """Timeout for finding batch (flush if batch is not full within timeout_ms)."""
        try:
            await asyncio.sleep(self.config.publisher_batch_timeout_ms / 1000.0)
            if self._running and self._finding_batch:
                await self._flush_finding_batch()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning(f"[MQTT] Error in batch timeout task: {e}")
    
    async def _flush_finding_batch(self) -> None:
        """Publish all queued findings to MQTT broker."""
        if not self._finding_batch:
            return
        
        batch_to_send = self._finding_batch.copy()
        self._finding_batch.clear()
        
        if self._finding_batch_timer and not self._finding_batch_timer.done():
            self._finding_batch_timer.cancel()
            self._finding_batch_timer = None
        
        for finding in batch_to_send:
            await self._queue_finding_publish(finding)
    
    async def _queue_finding_publish(self, finding: dict) -> None:
        """Queue a finding for publishing (may save to DB on failure)."""
        topic = f"dama/{finding['device_id']}/findings"
        
        try:
            if self._publisher and self.metrics.publisher_connected:
                await self._publisher.publish(topic, json.dumps(finding))
                self.metrics.messages_published += 1
            else:
                # Publisher not ready, save for retry
                await self._save_failed_publish(topic, finding)
                self.metrics.publish_failures += 1
        
        except Exception as e:
            log.warning(f"[MQTT] Publish error for {topic}: {e}")
            await self._save_failed_publish(topic, finding)
            self.metrics.publish_failures += 1
    
    async def _publisher_loop(self) -> None:
        """Continuously connect to MQTT broker and publish findings.
        
        Also retries failed publishes from SQLite database.
        """
        import aiomqtt
        
        while self._running:
            try:
                log.info(f"[MQTT] Publisher connecting to {self.config.host}:{self.config.port}")
                
                self._publisher = aiomqtt.Client(
                    hostname=self.config.host,
                    port=self.config.port,
                    username=self.config.username,
                    password=self.config.password,
                    keepalive=30,
                    tls_insecure=True,
                )
                
                await self._publisher.connect()
                self.metrics.publisher_connected = True
                log.info("[MQTT] Publisher connected")
                
                # Retry loop for failed publishes
                while self._running and self.metrics.publisher_connected:
                    try:
                        # Try to retry failed publishes
                        failed = await self._get_failed_publishes(limit=5)
                        for pub_id, topic, payload_json, retry_count in failed:
                            try:
                                payload = json.loads(payload_json)
                                await self._publisher.publish(topic, json.dumps(payload))
                                await self._remove_failed_publish(pub_id)
                                self.metrics.messages_published += 1
                                self.metrics.publish_retries += 1
                            except Exception as e:
                                log.debug(f"[MQTT] Retry failed for publish {pub_id}: {e}")
                        
                        await asyncio.sleep(5)
                    
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        log.warning(f"[MQTT] Error in retry loop: {e}")
                        await asyncio.sleep(1)
            
            except asyncio.CancelledError:
                log.debug("[MQTT] Publisher loop cancelled")
                break
            
            except Exception as e:
                self.metrics.publisher_connected = False
                self.metrics.last_publisher_error = str(e)
                log.warning(f"[MQTT] Publisher error (will retry in 5s): {e}")
                await asyncio.sleep(5)
    
    async def _health_monitor_loop(self) -> None:
        """Periodically log health metrics."""
        while self._running:
            try:
                await asyncio.sleep(self.config.heartbeat_interval_secs)
                
                log.info(
                    "[MQTT] Heartbeat: "
                    f"received={self.metrics.messages_received}, "
                    f"published={self.metrics.messages_published}, "
                    f"deep_think={self.metrics.deep_think_runs}, "
                    f"failures={self.metrics.deep_think_failures}, "
                    f"circuit_breaker={self.circuit_breaker_state.value}, "
                    f"subscriber_connected={self.metrics.subscriber_connected}, "
                    f"publisher_connected={self.metrics.publisher_connected}"
                )
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"[MQTT] Error in health monitor: {e}")
    
    def get_health(self) -> dict:
        """Get current health status for /mqtt/health endpoint."""
        return {
            "status": "healthy" if self._running else "stopped",
            "circuit_breaker": self.circuit_breaker_state.value,
            "metrics": {
                "messages_received": self.metrics.messages_received,
                "messages_published": self.metrics.messages_published,
                "deep_think_runs": self.metrics.deep_think_runs,
                "deep_think_failures": self.metrics.deep_think_failures,
                "publish_failures": self.metrics.publish_failures,
                "publish_retries": self.metrics.publish_retries,
                "circuit_breaker_trips": self.metrics.circuit_breaker_trips,
            },
            "connections": {
                "subscriber": self.metrics.subscriber_connected,
                "publisher": self.metrics.publisher_connected,
            },
            "last_errors": {
                "subscriber": self.metrics.last_subscriber_error,
                "publisher": self.metrics.last_publisher_error,
            },
        }
