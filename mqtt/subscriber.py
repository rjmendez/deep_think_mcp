"""MQTT integration for DAMAColonySubscriber → deep_think_mcp engine.

Wires the DAMAColonySubscriber into the async deep_think_mcp event loop,
batching claims from MQTT telemetry and passing them through the deep_think
reasoning engine with local-only Ollama models.

Architecture:
    1. DAMAColonySubscriber (in ground_truth.py) — connects to MQTT broker,
       deserializes sensor telemetry into Claim objects, queues them.
    
    2. MQTTClaimsProcessor (this module) — batches claims from the subscriber,
       processes them through deep_think_passes(), handles backoff/errors,
       publishes findings back to MQTT.
    
    3. Engine lifecycle hooks — init/shutdown/signal handling to start/stop
       the MQTT pipeline cleanly.

Environment Configuration:
    MQTT_ENABLE=true|false             Toggle MQTT integration (default: false)
    MQTT_HOST=botnet.floppydicks.net   Broker hostname
    MQTT_PORT=1883                     Broker port (1883=plain, 8883=TLS)
    MQTT_USERNAME=dama                 Authentication username
    MQTT_PASSWORD=...                  Authentication password (secret!)
    MQTT_USE_TLS=false                 TLS/SSL for secure connection
    
    MQTT_SUBSCRIBER_QUEUE_SIZE=1000    Claims queue max size
    MQTT_BATCH_SIZE=10                 Claims per deep_think batch
    MQTT_BATCH_TIMEOUT_MS=5000         Timeout to flush partial batch (ms)
    
    MQTT_FINDINGS_TOPIC=dama/colony/findings/{device_id}
                                       Topic for publishing deep_think results
"""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Any, Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration Loading
# ─────────────────────────────────────────────────────────────────────────────


class MQTTConfig:
    """MQTT subscriber and processor configuration loaded from environment."""
    
    def __init__(self) -> None:
        """Load MQTT configuration from environment variables."""
        self.enabled: bool = os.getenv("MQTT_ENABLE", "false").lower() in ("true", "1", "yes")
        self.broker_host: str = os.getenv("MQTT_HOST", "botnet.floppydicks.net")
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


# ─────────────────────────────────────────────────────────────────────────────
# MQTT Claims Processor
# ─────────────────────────────────────────────────────────────────────────────


class MQTTClaimsProcessor:
    """Batch claims from DAMAColonySubscriber, process through deep_think, publish results.
    
    Responsibilities:
        1. Accept Claim objects from subscriber queue
        2. Batch them (batch_size or batch_timeout)
        3. Pass to deep_think_passes() with local-only models
        4. Publish findings back to MQTT dama/colony/findings/{device_id}
        5. Handle errors gracefully (log, continue processing)
    """
    
    def __init__(
        self,
        config: MQTTConfig,
        subscriber: Any = None,
    ) -> None:
        """Initialize MQTT claims processor.
        
        Args:
            config: MQTTConfig instance
            subscriber: DAMAColonySubscriber instance (from ground_truth.py)
        """
        self.config = config
        self.subscriber = subscriber
        
        self._processor_task: Optional[asyncio.Task] = None
        self._running = False
        self._processed_count = 0
        self._error_count = 0
        self._batch_buffer: list[Any] = []
        
        log.info(f"[MQTT] Initialized processor: {config}")
    
    async def start(self) -> None:
        """Start the batch processor task."""
        if self._processor_task and not self._processor_task.done():
            log.warning("[MQTT] Processor already running")
            return
        
        if not self.subscriber:
            log.error("[MQTT] No subscriber configured, cannot start processor")
            return
        
        self._running = True
        self._processor_task = asyncio.create_task(self._run_processor_loop())
        log.info("[MQTT] Processor task started")
    
    async def stop(self) -> None:
        """Stop the processor task gracefully."""
        self._running = False
        
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                log.info("[MQTT] Processor task cancelled")
        
        # Flush any remaining claims in buffer
        if self._batch_buffer:
            log.info(f"[MQTT] Flushing {len(self._batch_buffer)} claims on shutdown")
            await self._process_batch(self._batch_buffer)
            self._batch_buffer = []
        
        log.info(
            f"[MQTT] Processor stopped. "
            f"Processed: {self._processed_count}, Errors: {self._error_count}"
        )
    
    async def _run_processor_loop(self) -> None:
        """Main processor loop: batch claims, call deep_think, publish findings."""
        try:
            while self._running:
                try:
                    # Collect claims into a batch
                    batch = await self._collect_batch()
                    
                    if batch:
                        await self._process_batch(batch)
                        self._batch_buffer = []
                    
                except asyncio.TimeoutError:
                    # Timeout waiting for claims—flush partial batch
                    if self._batch_buffer:
                        log.debug(
                            f"[MQTT] Batch timeout, flushing {len(self._batch_buffer)} claims"
                        )
                        await self._process_batch(self._batch_buffer)
                        self._batch_buffer = []
                
                except asyncio.CancelledError:
                    log.info("[MQTT] Processor loop cancelled")
                    break
                
                except Exception as e:
                    self._error_count += 1
                    log.error(f"[MQTT] Processor error: {e}", exc_info=True)
                    await asyncio.sleep(0.5)  # Brief backoff before retry
        
        except Exception as e:
            log.error(f"[MQTT] Fatal processor error: {e}", exc_info=True)
    
    async def _collect_batch(self) -> list[Any]:
        """Collect claims into a batch of size batch_size or until timeout.
        
        Returns:
            List of Claim objects, or empty list if timed out with no claims
        """
        batch = []
        deadline = asyncio.get_event_loop().time() + self.config.batch_timeout_sec
        
        while len(batch) < self.config.batch_size:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            
            try:
                claim = await asyncio.wait_for(
                    self.subscriber.get_claim(timeout=remaining),
                    timeout=remaining + 0.1,
                )
                if claim:
                    batch.append(claim)
                    log.debug(f"[MQTT] Collected claim: {claim.id}")
            
            except asyncio.TimeoutError:
                # Expected when deadline expires
                break
            except Exception as e:
                log.warning(f"[MQTT] Failed to get claim: {e}")
                break
        
        return batch
    
    async def _process_batch(self, batch: list[Any]) -> None:
        """Process a batch of claims through deep_think and publish findings.
        
        Args:
            batch: List of Claim objects from subscriber
        """
        if not batch:
            return
        
        try:
            # Import here to avoid circular dependency
            from deep_think_mcp.engine import deep_think_passes, build_provider_config
            
            # Extract device_id from first claim for logging and findings topic
            device_id = getattr(batch[0], "device_id", "unknown")
            
            # Format claims as a question for deep_think
            claims_text = "\n".join([
                f"  - {c.statement} (confidence: {c.confidence_model:.2f})"
                for c in batch
            ])
            
            question = (
                f"Analyze these {len(batch)} sensor claims from DAMA device {device_id}:\n"
                f"{claims_text}\n\n"
                f"Provide confidence scores, evidence gaps, and any anomalies detected."
            )
            
            log.info(f"[MQTT] Processing batch of {len(batch)} claims from {device_id}")
            
            # Call deep_think with local-only models (force_local_models=True)
            result = await deep_think_passes(
                question=question,
                passes=2,  # Quick 2-pass for live telemetry
                task_class="general",
                data_policy="local",  # Local-only Ollama
                force_local_models=True,  # Enforce security policy
                device_id=device_id,
            )
            
            # Parse result and extract key findings
            findings = self._extract_findings(result, device_id)
            
            self._processed_count += 1
            log.info(
                f"[MQTT] Batch processed successfully. "
                f"Findings: {len(findings)} items, Total processed: {self._processed_count}"
            )
            
            # Publish findings back to MQTT
            await self._publish_findings(findings, device_id)
        
        except Exception as e:
            self._error_count += 1
            log.error(
                f"[MQTT] Failed to process batch from {device_id}: {e}",
                exc_info=True
            )
            # Return confidence 0.0 and continue (graceful degradation)
    
    def _extract_findings(self, result: str, device_id: str) -> dict[str, Any]:
        """Extract key findings from deep_think result.
        
        Args:
            result: JSON string from deep_think_passes
            device_id: Device ID for context
        
        Returns:
            Dictionary of findings (or empty dict on parse error)
        """
        try:
            result_obj = json.loads(result)
            
            # Extract final_answer and key metadata
            findings = {
                "device_id": device_id,
                "timestamp": os.popen("date -u +%Y-%m-%dT%H:%M:%SZ").read().strip(),
                "final_answer": result_obj.get("final_answer", ""),
                "confidence": result_obj.get("confidence", 0.0),
                "passes": result_obj.get("passes", 0),
            }
            
            # Extract claims if available
            if "claims" in result_obj:
                findings["claims"] = result_obj["claims"]
            
            return findings
        
        except json.JSONDecodeError as e:
            log.warning(f"[MQTT] Failed to parse deep_think result: {e}")
            return {
                "device_id": device_id,
                "error": "Failed to parse reasoning output",
                "confidence": 0.0,
            }
    
    async def _publish_findings(self, findings: dict[str, Any], device_id: str) -> None:
        """Publish findings back to MQTT dama/colony/findings/{device_id}.
        
        Args:
            findings: Dictionary of findings from deep_think
            device_id: Device ID for topic routing
        """
        if not self.subscriber or not hasattr(self.subscriber, "_mqtt_client"):
            log.warning("[MQTT] MQTT client not available, skipping publish")
            return
        
        try:
            topic = self.config.findings_topic_template.format(device_id=device_id)
            payload = json.dumps(findings, indent=2)
            
            # Use subscriber's MQTT client if available
            client = self.subscriber._mqtt_client
            if client:
                await client.publish(topic, payload, qos=1)
                log.debug(f"[MQTT] Published findings to {topic}")
            else:
                log.warning("[MQTT] MQTT client not connected, cannot publish")
        
        except Exception as e:
            log.error(f"[MQTT] Failed to publish findings: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MQTT Confirmation Subscriber
# ─────────────────────────────────────────────────────────────────────────────


class ConfirmationSubscriber:
    """Subscribe to anomaly confirmation feedback from devices.
    
    Responsibilities:
        1. Subscribe to dama/+/anomaly_confirmation topic
        2. Parse confirmation payloads (finding_id, device_id, confirmed, evidence)
        3. Route confirmations to FeedbackStore for immediate processing
        4. Handle deserialization errors gracefully
        5. Normalize UUIDs from payload to hex format
    """
    
    def __init__(
        self,
        mqtt_host: str = "botnet.floppydicks.net",
        mqtt_port: int = 1883,
        mqtt_username: str = "dama",
        mqtt_password: str = "",
    ) -> None:
        """Initialize confirmation subscriber.
        
        Args:
            mqtt_host: MQTT broker hostname
            mqtt_port: MQTT broker port
            mqtt_username: MQTT username
            mqtt_password: MQTT password
        """
        try:
            import aiomqtt
            self._aiomqtt = aiomqtt
        except ImportError:
            self._aiomqtt = None
            log.warning("[MQTT] aiomqtt not installed, confirmation subscriber disabled")
        
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        
        self._client: Optional[Any] = None
        self._connected = False
        self._running = False
        self._subscription_task: Optional[asyncio.Task] = None
        
        # Import feedback store
        try:
            from mqtt.feedback_store import FeedbackStore
            self.feedback_store = FeedbackStore()
        except ImportError:
            self.feedback_store = None
            log.error("[MQTT] Failed to import FeedbackStore")
        
        log.debug(
            f"ConfirmationSubscriber initialized: {mqtt_host}:{mqtt_port}"
        )
    
    async def start(self) -> None:
        """Start the confirmation subscriber."""
        if not self._aiomqtt or not self.feedback_store:
            log.warning("[MQTT] Confirmation subscriber disabled (missing dependencies)")
            return
        
        self._running = True
        try:
            self._client = self._aiomqtt.Client(
                self.mqtt_host,
                self.mqtt_port,
                username=self.mqtt_username,
                password=self.mqtt_password,
                clean_session=True,
            )
            await self._client.connect()
            self._connected = True
            log.info(f"[MQTT] Confirmation subscriber connected to {self.mqtt_host}:{self.mqtt_port}")
            
            # Start subscription listener
            self._subscription_task = asyncio.create_task(self._subscription_loop())
        except Exception as e:
            log.error(f"[MQTT] Failed to connect confirmation subscriber: {e}")
            self._connected = False
    
    async def stop(self) -> None:
        """Stop the confirmation subscriber."""
        self._running = False
        
        if self._subscription_task:
            self._subscription_task.cancel()
            try:
                await self._subscription_task
            except asyncio.CancelledError:
                log.debug("[MQTT] Confirmation subscription cancelled")
        
        if self._client and self._connected:
            try:
                await self._client.disconnect()
                self._connected = False
                log.info("[MQTT] Confirmation subscriber disconnected")
            except Exception as e:
                log.error(f"[MQTT] Error disconnecting confirmation subscriber: {e}")
    
    async def subscribe_to_confirmations(self) -> None:
        """Subscribe to confirmation topic and process messages.
        
        Subscribes to dama/+/anomaly_confirmation and processes confirmations
        by calling feedback_store.record_confirmation() immediately (no batching).
        """
        if not self.feedback_store:
            log.error("[MQTT] No feedback store available")
            return
        
        await self.subscribe_to_confirmations_with_store(self.feedback_store)
    
    async def subscribe_to_confirmations_with_store(self, feedback_store: Any) -> None:
        """Subscribe to confirmations with a specific feedback store.
        
        Args:
            feedback_store: FeedbackStore instance to record confirmations
        """
        from mqtt.models import Confirmation, normalize_uuid
        
        if not self._client or not self._running:
            log.warning("[MQTT] Confirmation subscriber not ready")
            return
        
        try:
            # Subscribe to wildcard for all devices
            await self._client.subscribe("dama/+/anomaly_confirmation")
            log.info("[MQTT] Subscribed to anomaly confirmation topics")
            
            async with self._client.messages() as messages:
                async for message in messages:
                    try:
                        topic = message.topic
                        payload_str = message.payload.decode()
                        
                        # Parse topic: dama/{device_id}/anomaly_confirmation
                        parts = topic.split("/")
                        if len(parts) < 2:
                            log.warning(f"[MQTT] Invalid confirmation topic: {topic}")
                            continue
                        
                        device_id = parts[1]
                        
                        try:
                            payload = json.loads(payload_str)
                        except json.JSONDecodeError as e:
                            log.error(f"[MQTT] Failed to deserialize confirmation: {e}")
                            continue
                        
                        # Extract fields
                        try:
                            finding_id = payload.get("finding_id", "")
                            confirmed = payload.get("confirmed", False)
                            evidence = payload.get("evidence", "")
                            timestamp = payload.get("timestamp", "")
                            
                            # Validate required fields
                            if not finding_id or not device_id:
                                log.error("[MQTT] Missing required field in confirmation payload")
                                continue
                            
                            # Normalize finding_id from payload
                            try:
                                normalized_finding_id = normalize_uuid(finding_id)
                            except Exception as e:
                                log.error(f"[MQTT] Failed to normalize UUID: {e}")
                                continue
                            
                            # Create confirmation object
                            confirmation = Confirmation(
                                finding_id=normalized_finding_id,
                                device_id=device_id,
                                confirmed=confirmed,
                                evidence=evidence,
                                timestamp=timestamp,
                            )
                            
                            # Record confirmation immediately (no batching)
                            result = feedback_store.record_confirmation(confirmation)
                            
                            log.debug(
                                f"Confirmation received: finding_id={normalized_finding_id} "
                                f"device={device_id} confirmed={confirmed}"
                            )
                            
                        except KeyError as e:
                            log.error(f"[MQTT] Missing required field in confirmation: {e}")
                            continue
                        except Exception as e:
                            log.error(f"[MQTT] Error processing confirmation: {e}")
                            continue
                    
                    except asyncio.CancelledError:
                        log.debug("[MQTT] Confirmation subscription cancelled")
                        break
                    except Exception as e:
                        log.error(f"[MQTT] Subscription loop error: {e}")
        
        except asyncio.CancelledError:
            log.debug("[MQTT] Confirmation subscription cancelled")
        except Exception as e:
            log.error(f"[MQTT] Confirmation subscription error: {e}")
    
    async def _subscription_loop(self) -> None:
        """Internal subscription loop."""
        try:
            await self.subscribe_to_confirmations()
        except Exception as e:
            log.error(f"[MQTT] Confirmation subscription loop error: {e}")



# Global state (used by engine integration hooks)
_mqtt_config: Optional[MQTTConfig] = None
_mqtt_subscriber: Optional[Any] = None
_mqtt_processor: Optional[MQTTClaimsProcessor] = None


async def mqtt_startup() -> None:
    """Initialize MQTT subscriber and processor on engine startup."""
    global _mqtt_config, _mqtt_subscriber, _mqtt_processor
    
    try:
        _mqtt_config = MQTTConfig()
        
        # Validate configuration
        error = _mqtt_config.validate()
        if error:
            log.error(f"[MQTT] Configuration error: {error}")
            return
        
        if not _mqtt_config.enabled:
            log.info("[MQTT] Integration disabled (MQTT_ENABLE=false)")
            return
        
        log.info(f"[MQTT] Starting up with config: {_mqtt_config}")
        
        # Import and initialize DAMAColonySubscriber
        from deep_think_mcp.ground_truth import DAMAColonySubscriber
        
        _mqtt_subscriber = DAMAColonySubscriber(
            broker_host=_mqtt_config.broker_host,
            broker_port=_mqtt_config.broker_port,
            broker_user=_mqtt_config.broker_user,
            broker_password=_mqtt_config.broker_password,
        )
        
        # Start subscriber
        await _mqtt_subscriber.start()
        log.info("[MQTT] Subscriber started")
        
        # Initialize and start processor
        _mqtt_processor = MQTTClaimsProcessor(_mqtt_config, _mqtt_subscriber)
        await _mqtt_processor.start()
        log.info("[MQTT] Processor started")
    
    except ImportError as e:
        log.error(f"[MQTT] Failed to import DAMAColonySubscriber: {e}")
        log.info("[MQTT] MQTT integration disabled (import error)")
    
    except Exception as e:
        log.error(f"[MQTT] Startup error: {e}", exc_info=True)
        log.info("[MQTT] MQTT integration disabled (startup error)")


async def mqtt_shutdown() -> None:
    """Gracefully stop MQTT subscriber and processor on engine shutdown."""
    global _mqtt_subscriber, _mqtt_processor
    
    try:
        if _mqtt_processor:
            log.info("[MQTT] Stopping processor...")
            await _mqtt_processor.stop()
        
        if _mqtt_subscriber:
            log.info("[MQTT] Stopping subscriber...")
            await _mqtt_subscriber.stop()
        
        log.info("[MQTT] Shutdown complete")
    
    except Exception as e:
        log.error(f"[MQTT] Shutdown error: {e}", exc_info=True)


def setup_signal_handlers() -> None:
    """Register SIGTERM/SIGINT handlers for graceful shutdown."""
    def handle_signal(signum: int, frame: Any) -> None:
        log.info(f"[MQTT] Received signal {signum}, shutting down gracefully...")
        try:
            asyncio.run(mqtt_shutdown())
        except Exception as e:
            log.error(f"[MQTT] Error during signal handler shutdown: {e}")
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    log.info("[MQTT] Signal handlers registered")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def get_mqtt_processor() -> Optional[MQTTClaimsProcessor]:
    """Return the current MQTT processor instance (for testing/monitoring)."""
    return _mqtt_processor


def get_mqtt_subscriber() -> Optional[Any]:
    """Return the current MQTT subscriber instance (for testing/monitoring)."""
    return _mqtt_subscriber


def is_mqtt_enabled() -> bool:
    """Check if MQTT integration is enabled."""
    return _mqtt_config is not None and _mqtt_config.enabled
