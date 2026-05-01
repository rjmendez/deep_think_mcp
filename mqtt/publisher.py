"""MQTT Findings Publisher for deep_think_mcp engine.

Publishes findings (anomalies, contradictions, etc.) extracted from deep_think
reasoning passes to an MQTT broker with automatic batching, retry, and persistence.

Features:
- Batch findings (configurable size N and timeout T)
- QoS=1 publishing to dama/colony/findings/{device_id}
- Exponential backoff retry with SQLite persistence during outages
- Auto-recovery: load persisted findings on startup, replay on reconnect
- Confirmation subscription handling via dama/{device_id}/anomaly_confirmation
- Type hints and async/await throughout
- Graceful error handling and degradation
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional, Callable, Awaitable, List
from uuid import uuid4

try:
    import aiomqtt
except ImportError:
    aiomqtt = None

# Import Finding from models for feedback loop integration
try:
    from mqtt.models import Finding, Confirmation, AnomalyType
except ImportError:
    Finding = None  # Will be defined locally as fallback

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Finding:
    """A finding extracted from deep_think reasoning results.
    
    Attributes:
        device_id: Device identifier (e.g., 'ant_001')
        claim_ids: List of claim IDs that support this finding
        anomalies: List of anomaly descriptions
        confidence: Confidence score (0.0-1.0)
        severity: Severity level ('low', 'medium', 'high', 'critical')
        timestamp: ISO 8601 timestamp when finding was created
        metadata: Additional context (contradictions, hallucinations, etc.)
    """
    device_id: str
    claim_ids: list[str]
    anomalies: list[str]
    confidence: float
    severity: str
    timestamp: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        """Create Finding from dictionary."""
        return cls(**data)


# ─────────────────────────────────────────────────────────────────────────────
# SQLite Persistence Layer
# ─────────────────────────────────────────────────────────────────────────────


class FindingsPersistenceStore:
    """SQLite persistence for findings during MQTT outages."""

    def __init__(self, db_path: str = "~/.deep_think/findings_queue.db"):
        """Initialize persistence store.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema if it doesn't exist."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS findings_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id TEXT NOT NULL,
                        finding_json TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        retry_count INTEGER DEFAULT 0,
                        last_retry_at TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS confirmations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id TEXT NOT NULL,
                        claim_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
                log.debug(f"Initialized findings persistence store at {self.db_path}")
        except Exception as e:
            log.error(f"Failed to initialize findings DB: {e}")

    def save_finding(self, finding: Finding) -> int:
        """Save finding to persistence store.
        
        Args:
            finding: Finding object to save
            
        Returns:
            Row ID of inserted finding
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO findings_queue (device_id, finding_json)
                    VALUES (?, ?)
                    """,
                    (finding.device_id, json.dumps(finding.to_dict()))
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            log.error(f"Failed to save finding to DB: {e}")
            return -1

    def load_pending_findings(self, limit: int = 100) -> list[tuple[int, Finding]]:
        """Load pending findings from persistence store.
        
        Args:
            limit: Maximum number of findings to load
            
        Returns:
            List of (row_id, Finding) tuples
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cursor = conn.execute(
                    """
                    SELECT id, finding_json FROM findings_queue
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (limit,)
                )
                results = []
                for row_id, finding_json in cursor.fetchall():
                    try:
                        finding_data = json.loads(finding_json)
                        finding = Finding.from_dict(finding_data)
                        results.append((row_id, finding))
                    except Exception as e:
                        log.error(f"Failed to deserialize finding {row_id}: {e}")
                return results
        except Exception as e:
            log.error(f"Failed to load pending findings from DB: {e}")
            return []

    def mark_finding_published(self, row_id: int) -> None:
        """Remove finding from persistence store after successful publish.
        
        Args:
            row_id: Row ID of finding to remove
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "DELETE FROM findings_queue WHERE id = ?",
                    (row_id,)
                )
                conn.commit()
        except Exception as e:
            log.error(f"Failed to mark finding {row_id} as published: {e}")

    def update_retry_count(self, row_id: int, retry_count: int) -> None:
        """Update retry count for a finding.
        
        Args:
            row_id: Row ID of finding
            retry_count: New retry count
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    """
                    UPDATE findings_queue 
                    SET retry_count = ?, last_retry_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (retry_count, row_id)
                )
                conn.commit()
        except Exception as e:
            log.error(f"Failed to update retry count for finding {row_id}: {e}")

    def save_confirmation(self, device_id: str, claim_id: str, status: str) -> None:
        """Save anomaly confirmation from device feedback.
        
        Args:
            device_id: Device that provided feedback
            claim_id: Claim ID being confirmed/rejected
            status: Confirmation status ('confirmed', 'rejected', 'uncertain')
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO confirmations (device_id, claim_id, status)
                    VALUES (?, ?, ?)
                    """,
                    (device_id, claim_id, status)
                )
                conn.commit()
        except Exception as e:
            log.error(f"Failed to save confirmation: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MQTT Publisher with Batching and Retry
# ─────────────────────────────────────────────────────────────────────────────


class MQTTFindingsPublisher:
    """Publishes findings to MQTT broker with batching, retry, and persistence.
    
    Batching:
    - Collects findings up to batch_size or batch_timeout_ms (whichever comes first)
    - Publishes as a single JSON array to dama/colony/findings/{device_id}
    
    Retry:
    - Uses exponential backoff: 1s, 2s, 4s, 8s, then persists for manual review
    - Max retries configurable via max_retries parameter
    
    Persistence:
    - Findings persisted to SQLite during MQTT outages
    - Auto-recovered and replayed on reconnection
    
    Subscriptions:
    - Listens to dama/{device_id}/anomaly_confirmation for feedback
    - Saves confirmations to persistence store
    """

    def __init__(
        self,
        mqtt_host: str = "[REDACTED_MQTT_HOST]",
        mqtt_port: int = 1883,
        mqtt_username: str = "dama",
        mqtt_password: str = "",
        batch_size: int = 10,
        batch_timeout_ms: int = 5000,
        max_retries: int = 8,
        db_path: str = "~/.deep_think/findings_queue.db",
        enabled: bool = True,
    ):
        """Initialize MQTTFindingsPublisher.
        
        Args:
            mqtt_host: MQTT broker hostname
            mqtt_port: MQTT broker port
            mqtt_username: MQTT username
            mqtt_password: MQTT password
            batch_size: Maximum findings per batch before publishing
            batch_timeout_ms: Milliseconds to wait before publishing partial batch
            max_retries: Maximum retry attempts before persisting
            db_path: Path to SQLite persistence database
            enabled: Whether publishing is enabled
        """
        if aiomqtt is None:
            log.warning("aiomqtt not installed, publisher disabled")
            self.enabled = False
            return

        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        self.batch_size = batch_size
        self.batch_timeout_ms = batch_timeout_ms
        self.max_retries = max_retries
        self.enabled = enabled
        self.store = FindingsPersistenceStore(db_path)

        # Batching state per device
        self._batches: dict[str, list[Finding]] = {}
        self._batch_timers: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._client: Optional[aiomqtt.Client] = None
        self._connected = False
        self._running = False
        self._confirmation_callback: Optional[Callable[[str, str, str], Awaitable[None]]] = None

        log.debug(
            f"MQTTFindingsPublisher initialized: {mqtt_host}:{mqtt_port}, "
            f"batch_size={batch_size}, timeout={batch_timeout_ms}ms, "
            f"enabled={self.enabled}"
        )

    async def start(self) -> None:
        """Start MQTT publisher (connect to broker, start subscription loop).
        
        Handles connection failures gracefully — continues without MQTT if unavailable.
        """
        if not self.enabled or aiomqtt is None:
            log.info("MQTT publisher disabled or aiomqtt not available")
            return

        self._running = True
        try:
            self._client = aiomqtt.Client(
                self.mqtt_host,
                self.mqtt_port,
                username=self.mqtt_username,
                password=self.mqtt_password,
                clean_session=True,
            )
            await self._client.connect()
            self._connected = True
            log.info(f"Connected to MQTT broker {self.mqtt_host}:{self.mqtt_port}")

            # Load and replay persisted findings
            await self._replay_persisted_findings()

            # Start subscription listener in background
            asyncio.create_task(self._subscription_loop())
        except Exception as e:
            log.error(f"Failed to connect to MQTT broker: {e}")
            self._connected = False
            log.warning("Continuing without MQTT; findings will be persisted locally")

    async def stop(self) -> None:
        """Stop MQTT publisher (flush batches, disconnect).
        
        Ensures all pending findings are published before shutdown.
        """
        self._running = False

        # Cancel all pending batch timers
        for timer_task in self._batch_timers.values():
            if timer_task and not timer_task.done():
                timer_task.cancel()
        self._batch_timers.clear()

        # Publish any remaining batched findings
        async with self._lock:
            for device_id, findings in list(self._batches.items()):
                if findings:
                    await self._publish_batch(device_id, findings)
            self._batches.clear()

        # Disconnect from MQTT
        if self._client and self._connected:
            try:
                await self._client.disconnect()
                self._connected = False
                log.info("Disconnected from MQTT broker")
            except Exception as e:
                log.error(f"Error disconnecting from MQTT: {e}")

    def set_confirmation_callback(
        self, callback: Callable[[str, str, str], Awaitable[None]]
    ) -> None:
        """Set callback for when confirmations are received.
        
        Args:
            callback: Async function(device_id, claim_id, status) called on confirmation
        """
        self._confirmation_callback = callback

    async def publish_finding(self, finding: Finding) -> bool:
        """Queue finding for batch publishing.
        
        Args:
            finding: Finding object to publish
            
        Returns:
            True if queued successfully, False on error
        """
        if not self.enabled:
            log.debug(f"Publishing disabled, skipping finding for {finding.device_id}")
            return False

        try:
            async with self._lock:
                device_id = finding.device_id

                # Initialize batch for this device if needed
                if device_id not in self._batches:
                    self._batches[device_id] = []

                # Add finding to batch
                self._batches[device_id].append(finding)
                batch_len = len(self._batches[device_id])

                log.debug(
                    f"Queued finding for {device_id}: batch now {batch_len}/{self.batch_size}"
                )

                # Check if batch should publish immediately (size threshold reached)
                if batch_len >= self.batch_size:
                    await self._publish_batch(device_id, self._batches[device_id])
                    self._batches[device_id] = []

                    # Cancel any pending timer for this device
                    if device_id in self._batch_timers:
                        self._batch_timers[device_id].cancel()
                        del self._batch_timers[device_id]
                elif batch_len == 1:
                    # First item in batch, start timer
                    self._schedule_batch_timeout(device_id)

            return True
        except Exception as e:
            log.error(f"Error queueing finding: {e}")
            # Persist finding for recovery
            self.store.save_finding(finding)
            return False

    async def publish_findings(self, findings: List[Any]) -> List[str]:
        """Publish multiple findings with UUID normalization and TTL.
        
        Process findings from ground_truth anomalies:
        - Set finding.id = uuid4().hex (normalized to hex format)
        - Set finding.expires_at = now() + timedelta(days=7) for DefCon TTL
        - Validate confidence (0.0 to 1.0)
        - Serialize to JSON with all field types handled correctly
        - Publish to dama/colony/findings/{device_id} with QoS=1
        
        Args:
            findings: List of Finding objects to publish
            
        Returns:
            List of finding IDs published
        """
        if not self.enabled:
            log.debug(f"Publishing disabled, skipping {len(findings)} findings")
            return []

        published_ids: List[str] = []
        
        try:
            # Process each finding
            for finding in findings:
                try:
                    # Ensure finding has an ID (normalize to hex format)
                    if not finding.id or finding.id == "":
                        finding.id = uuid4().hex
                    
                    # Set TTL to 7 days if not already set
                    if not finding.expires_at:
                        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
                        finding.expires_at = expires_at.isoformat().replace("+00:00", "Z")
                    
                    # Validate confidence
                    if not 0.0 <= finding.confidence <= 1.0:
                        log.error(
                            f"Invalid confidence {finding.confidence} for finding {finding.id}, "
                            f"must be 0.0 to 1.0"
                        )
                        continue
                    
                    # Queue finding for publishing
                    success = await self.publish_finding(finding)
                    if success:
                        published_ids.append(finding.id)
                        log.debug(
                            f"Finding published: id={finding.id} "
                            f"device={finding.device_id}"
                        )
                    else:
                        log.warning(
                            f"Failed to queue finding {finding.id} for {finding.device_id}"
                        )
                
                except Exception as e:
                    log.error(f"Error processing finding: {e}", exc_info=True)
                    continue
            
            return published_ids
        
        except Exception as e:
            log.error(f"Error publishing findings batch: {e}", exc_info=True)
            return published_ids
        """Schedule automatic batch publish after timeout.
        
        Args:
            device_id: Device ID for this batch
        """
        if device_id in self._batch_timers:
            self._batch_timers[device_id].cancel()

        async def timeout_handler() -> None:
            await asyncio.sleep(self.batch_timeout_ms / 1000.0)
            async with self._lock:
                if device_id in self._batches and self._batches[device_id]:
                    findings = self._batches[device_id]
                    await self._publish_batch(device_id, findings)
                    self._batches[device_id] = []
                if device_id in self._batch_timers:
                    del self._batch_timers[device_id]

        self._batch_timers[device_id] = asyncio.create_task(timeout_handler())

    async def _publish_batch(
        self, device_id: str, findings: list[Finding], retry_count: int = 0
    ) -> bool:
        """Publish batch of findings to MQTT with exponential backoff retry.
        
        Args:
            device_id: Device ID for this batch
            findings: List of findings to publish
            retry_count: Current retry attempt (for exponential backoff)
            
        Returns:
            True if published successfully, False if persisted for later
        """
        if not findings:
            return True

        if not self.enabled or not self._connected or not self._client:
            log.debug(f"MQTT not connected, persisting {len(findings)} findings")
            for finding in findings:
                self.store.save_finding(finding)
            return False

        topic = f"dama/colony/findings/{device_id}"
        payload = json.dumps([f.to_dict() for f in findings])

        try:
            await self._client.publish(topic, payload=payload, qos=1)
            log.info(f"Published {len(findings)} findings to {topic}")
            return True
        except Exception as e:
            # Exponential backoff retry
            if retry_count < self.max_retries:
                backoff_seconds = min(2 ** retry_count, 8)  # 1s, 2s, 4s, 8s, ...
                log.warning(
                    f"Publish failed (attempt {retry_count + 1}/{self.max_retries}): {e}. "
                    f"Retrying in {backoff_seconds}s..."
                )

                # Schedule retry
                await asyncio.sleep(backoff_seconds)
                return await self._publish_batch(device_id, findings, retry_count + 1)
            else:
                # Max retries reached, persist for manual review
                log.error(
                    f"Publish failed after {self.max_retries} retries. "
                    f"Persisting {len(findings)} findings for recovery."
                )
                for finding in findings:
                    row_id = self.store.save_finding(finding)
                    if row_id > 0:
                        self.store.update_retry_count(row_id, retry_count)
                return False

    async def _replay_persisted_findings(self) -> None:
        """Load and replay persisted findings from DB on startup/reconnect.
        
        Publishes findings with a small delay between batches to avoid overwhelming
        the broker.
        """
        pending = self.store.load_pending_findings(limit=100)
        if not pending:
            log.debug("No persisted findings to replay")
            return

        log.info(f"Replaying {len(pending)} persisted findings...")
        for row_id, finding in pending:
            try:
                success = await self._publish_batch(finding.device_id, [finding])
                if success:
                    self.store.mark_finding_published(row_id)
                    await asyncio.sleep(0.1)  # Small delay between publishes
            except Exception as e:
                log.error(f"Error replaying finding {row_id}: {e}")

    async def _subscription_loop(self) -> None:
        """Listen for anomaly confirmations from devices.
        
        Subscribes to dama/{device_id}/anomaly_confirmation topics and processes
        feedback from devices confirming or rejecting findings.
        """
        if not self._client or not self.enabled:
            return

        try:
            # Subscribe to wildcard for all devices
            await self._client.subscribe("dama/+/anomaly_confirmation")
            log.info("Subscribed to anomaly confirmation topics")

            async with self._client.messages() as messages:
                async for message in messages:
                    try:
                        topic = message.topic
                        payload = message.payload.decode()

                        # Parse topic: dama/{device_id}/anomaly_confirmation
                        parts = topic.split("/")
                        if len(parts) >= 2:
                            device_id = parts[1]
                            confirmation = json.loads(payload)

                            claim_id = confirmation.get("claim_id", "")
                            status = confirmation.get("status", "uncertain")

                            # Save confirmation
                            self.store.save_confirmation(device_id, claim_id, status)
                            log.debug(f"Received confirmation: {device_id}/{claim_id} = {status}")

                            # Call callback if set
                            if self._confirmation_callback:
                                try:
                                    await self._confirmation_callback(device_id, claim_id, status)
                                except Exception as e:
                                    log.error(f"Error in confirmation callback: {e}")
                    except json.JSONDecodeError as e:
                        log.error(f"Invalid JSON in confirmation message: {e}")
                    except Exception as e:
                        log.error(f"Error processing confirmation: {e}")
        except asyncio.CancelledError:
            log.debug("Subscription loop cancelled")
        except Exception as e:
            log.error(f"Subscription loop error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Findings Converter
# ─────────────────────────────────────────────────────────────────────────────


def findings_from_deep_think_result(
    result: dict[str, Any],
    device_id: str = "unknown",
    anomaly_threshold: float = 0.5,
) -> list[Finding]:
    """Extract findings from deep_think reasoning result.
    
    Args:
        result: Deep think result dict with structure:
            {
                'final_answer': str,
                'reasoning_chain': [PassResult],
                'validation': ValidationData,
                'pass_cache': list,
                ...
            }
        device_id: Device ID to attach to findings
        anomaly_threshold: Minimum confidence to include as finding
        
    Returns:
        List of Finding objects extracted from result
    """
    findings: list[Finding] = []

    if not result:
        return findings

    try:
        # Extract device_id from metadata if available
        metadata = result.get("metadata", {})
        if isinstance(metadata, dict) and metadata.get("device_id"):
            device_id = metadata["device_id"]

        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        claim_ids: list[str] = []
        anomalies: list[str] = []
        max_confidence = 0.0
        severity = "low"

        # 1. Check validation results
        validation = result.get("validation", {})
        if isinstance(validation, dict):
            # Extract contradictions
            contradictions = validation.get("contradictions", [])
            for contradiction in contradictions:
                if isinstance(contradiction, dict):
                    desc = contradiction.get("description", str(contradiction))
                    anomalies.append(f"Contradiction: {desc}")

            # Extract hallucinations
            hallucination_details = validation.get("hallucination_details", [])
            for hallucination in hallucination_details:
                if isinstance(hallucination, dict):
                    desc = hallucination.get("description", str(hallucination))
                    anomalies.append(f"Hallucination: {desc}")

            # Get confidence
            overall_confidence = validation.get("overall_confidence", 0.5)
            if isinstance(overall_confidence, (int, float)):
                max_confidence = max(max_confidence, float(overall_confidence))

            # Extract claims
            claims = validation.get("claims", [])
            for i, claim in enumerate(claims):
                if isinstance(claim, dict):
                    claim_ids.append(claim.get("id", f"claim_{i}"))

        # 2. Check pass results for high-confidence anomalies
        pass_cache = result.get("pass_cache", [])
        if isinstance(pass_cache, list):
            for pass_result in pass_cache:
                if not isinstance(pass_result, dict):
                    continue

                # Check for validation data in pass
                pass_validation = pass_result.get("validation", {})
                if isinstance(pass_validation, dict):
                    confidence = pass_validation.get("measured_confidence", 0.0)
                    if isinstance(confidence, (int, float)):
                        confidence = float(confidence)
                        if confidence >= anomaly_threshold:
                            max_confidence = max(max_confidence, confidence)
                            anomalies.append(
                                f"Pass {pass_result.get('pass_num', '?')}: "
                                f"{pass_result.get('framing', 'analysis')} "
                                f"(confidence: {confidence:.2f})"
                            )

        # 3. Check measured_confidence if present
        if "measured_confidence" in result:
            conf = result["measured_confidence"]
            if isinstance(conf, (int, float)):
                max_confidence = max(max_confidence, float(conf))

        # 4. Determine severity based on confidence and anomaly count
        if len(anomalies) > 2 and max_confidence > 0.8:
            severity = "critical"
        elif len(anomalies) > 1 and max_confidence > 0.7:
            severity = "high"
        elif len(anomalies) > 0 and max_confidence > 0.6:
            severity = "medium"
        else:
            severity = "low"

        # Create finding if we have sufficient confidence
        # If no anomalies found but confidence is high, add confidence as finding
        if max_confidence >= anomaly_threshold:
            if not anomalies:
                anomalies.append(
                    f"High confidence finding (confidence: {max_confidence:.2f})"
                )
            finding = Finding(
                device_id=device_id,
                claim_ids=claim_ids,
                anomalies=anomalies,
                confidence=max_confidence,
                severity=severity,
                timestamp=timestamp,
                metadata={
                    "anomaly_count": len(anomalies) - (1 if not anomalies else 0),
                    "pass_count": len(pass_cache),
                    "hallucination_count": validation.get("hallucination_count", 0),
                },
            )
            findings.append(finding)
            log.debug(f"Extracted finding for {device_id}: {len(anomalies)} anomalies, {severity}")

    except Exception as e:
        log.error(f"Error extracting findings from result: {e}", exc_info=True)

    return findings


# ─────────────────────────────────────────────────────────────────────────────
# Configuration Loading
# ─────────────────────────────────────────────────────────────────────────────


def load_config_from_env() -> dict[str, Any]:
    """Load MQTTFindingsPublisher configuration from environment.
    
    Returns:
        Configuration dict with keys:
        - mqtt_host, mqtt_port, mqtt_username, mqtt_password
        - batch_size, batch_timeout_ms, max_retries
        - enabled
    """
    return {
        "mqtt_host": os.getenv("MQTT_HOST", "[REDACTED_MQTT_HOST]"),
        "mqtt_port": int(os.getenv("MQTT_PORT", "1883")),
        "mqtt_username": os.getenv("MQTT_USERNAME", "dama"),
        "mqtt_password": os.getenv("MQTT_PASSWORD", ""),
        "batch_size": int(os.getenv("PUBLISHER_BATCH_SIZE", "10")),
        "batch_timeout_ms": int(os.getenv("PUBLISHER_BATCH_TIMEOUT_MS", "5000")),
        "max_retries": int(os.getenv("PUBLISHER_MAX_RETRIES", "8")),
        "enabled": os.getenv("PUBLISHER_ENABLE", "true").lower() in ("true", "1", "yes"),
    }
