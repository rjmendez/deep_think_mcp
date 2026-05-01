"""Example: Integrating MQTTFindingsPublisher with deep_think engine.

This file demonstrates the recommended integration patterns:
1. Initializing publisher at engine startup
2. Extracting findings from reasoning results
3. Publishing findings asynchronously
4. Handling errors gracefully
5. Subscribing to confirmation feedback
"""

import asyncio
import logging
from typing import Any, Optional

from mqtt.publisher import (
    MQTTFindingsPublisher,
    findings_from_deep_think_result,
    load_config_from_env,
)

log = logging.getLogger(__name__)


class DeepThinkEngineWithFindings:
    """Deep think engine integration with MQTT findings publisher."""

    def __init__(self):
        """Initialize engine with MQTT publisher."""
        self.mqtt_publisher: Optional[MQTTFindingsPublisher] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize MQTT publisher at startup.
        
        Should be called in your engine's startup sequence.
        """
        try:
            # Load configuration from environment
            config = load_config_from_env()
            log.info("Loaded MQTT config: %s:%s", config["mqtt_host"], config["mqtt_port"])

            # Initialize publisher
            self.mqtt_publisher = MQTTFindingsPublisher(**config)

            # Register confirmation callback (optional)
            self.mqtt_publisher.set_confirmation_callback(self._on_confirmation)

            # Start publisher (connects to broker, loads persisted findings)
            await self.mqtt_publisher.start()
            self._initialized = True
            log.info("MQTT publisher started successfully")

        except Exception as e:
            log.error("Failed to initialize MQTT publisher: %s", e, exc_info=True)
            # Continue without publisher (graceful degradation)
            self.mqtt_publisher = None

    async def shutdown(self) -> None:
        """Shutdown MQTT publisher at engine shutdown.
        
        Should be called in your engine's shutdown sequence.
        Ensures all pending findings are published before exit.
        """
        if self.mqtt_publisher:
            try:
                await self.mqtt_publisher.stop()
                log.info("MQTT publisher stopped")
            except Exception as e:
                log.error("Error stopping MQTT publisher: %s", e)

    async def deep_think_with_findings(
        self,
        question: str,
        device_id: str = "unknown",
        passes: int = 3,
        **kwargs,
    ) -> dict[str, Any]:
        """Run deep_think reasoning and publish findings.
        
        This is the main integration point: reasoning result → findings extraction →
        MQTT publication.
        
        Args:
            question: The reasoning question
            device_id: Device that prompted this reasoning
            passes: Number of reasoning passes
            **kwargs: Additional arguments passed to deep_think_passes()
            
        Returns:
            Deep think result (same as deep_think_passes)
        """
        # Run deep_think reasoning
        # In real code, this calls your actual deep_think_passes() function
        result = await self._run_deep_think_reasoning(
            question,
            passes=passes,
            device_id=device_id,
            **kwargs
        )

        # Extract findings from result
        # This is where anomalies, contradictions, hallucinations are identified
        try:
            findings = findings_from_deep_think_result(
                result,
                device_id=device_id,
                anomaly_threshold=0.5,  # Configurable threshold
            )

            log.info(f"Extracted {len(findings)} finding(s) from reasoning result")

            # Publish findings asynchronously (non-blocking)
            await self._publish_findings(findings)

        except Exception as e:
            log.error("Error extracting findings: %s", e, exc_info=True)
            # Continue processing, don't fail reasoning due to findings extraction

        return result

    async def _publish_findings(self, findings: list[Any]) -> None:
        """Publish findings to MQTT.
        
        This is designed to be non-blocking and graceful:
        - If publisher not initialized, logs warning but continues
        - If MQTT unavailable, findings persisted to SQLite
        - Findings queued for batching (publish triggered by size or timeout)
        
        Args:
            findings: List of Finding objects to publish
        """
        if not self.mqtt_publisher:
            log.warning("MQTT publisher not initialized; skipping findings publication")
            return

        if not findings:
            return

        # Publish each finding (they're queued for batching)
        for finding in findings:
            try:
                # This returns immediately; actual publishing happens in background
                success = await self.mqtt_publisher.publish_finding(finding)

                if success:
                    log.debug(
                        "Queued finding for %s: severity=%s, confidence=%.2f",
                        finding.device_id,
                        finding.severity,
                        finding.confidence,
                    )
                else:
                    log.warning(
                        "Failed to queue finding for %s (will retry or persist)",
                        finding.device_id,
                    )

            except Exception as e:
                log.error("Error publishing finding: %s", e, exc_info=True)
                # Continue publishing other findings

    async def _on_confirmation(
        self, device_id: str, claim_id: str, status: str
    ) -> None:
        """Handle anomaly confirmation feedback from device.
        
        Called when device sends confirmation via MQTT subscription:
        Topic: dama/{device_id}/anomaly_confirmation
        Payload: {"claim_id": "...", "status": "confirmed|rejected|uncertain"}
        
        Args:
            device_id: Device providing feedback
            claim_id: Claim ID being confirmed
            status: Confirmation status
        """
        log.info(
            "Received confirmation: device=%s, claim=%s, status=%s",
            device_id,
            claim_id,
            status,
        )

        # Example: Update your ML model / confidence scoring / etc.
        # This is called asynchronously when confirmations arrive
        try:
            if status == "confirmed":
                log.info("Claim %s validated by device %s", claim_id, device_id)
                # Update model confidence, trigger re-analysis, etc.
                await self._on_claim_confirmed(device_id, claim_id)

            elif status == "rejected":
                log.warning("Claim %s rejected by device %s", claim_id, device_id)
                # Lower confidence, investigate false positive, etc.
                await self._on_claim_rejected(device_id, claim_id)

            elif status == "uncertain":
                log.debug("Claim %s marked uncertain by device %s", claim_id, device_id)
                # Mark for manual review, schedule re-analysis, etc.
                await self._on_claim_uncertain(device_id, claim_id)

        except Exception as e:
            log.error("Error processing confirmation: %s", e, exc_info=True)

    async def _on_claim_confirmed(self, device_id: str, claim_id: str) -> None:
        """Handle confirmed claim."""
        # TODO: Update ML model, increment confidence score, etc.
        pass

    async def _on_claim_rejected(self, device_id: str, claim_id: str) -> None:
        """Handle rejected claim."""
        # TODO: Lower confidence, investigate false positive, etc.
        pass

    async def _on_claim_uncertain(self, device_id: str, claim_id: str) -> None:
        """Handle uncertain claim."""
        # TODO: Mark for manual review, schedule re-analysis, etc.
        pass

    async def _run_deep_think_reasoning(
        self, question: str, device_id: str = "unknown", **kwargs
    ) -> dict[str, Any]:
        """Run deep_think reasoning.
        
        This is a placeholder. In real code, this calls your actual
        deep_think_passes() function.
        
        Args:
            question: Reasoning question
            device_id: Device context for logging
            **kwargs: Additional reasoning parameters
            
        Returns:
            Deep think result with validation, pass_cache, etc.
        """
        # In real code, this would call:
        # result = await deep_think_passes(
        #     question,
        #     device_id=device_id,
        #     **kwargs
        # )
        # For demonstration, return a mock result

        return {
            "final_answer": "Analysis complete",
            "validation": {
                "overall_confidence": 0.75,
                "contradictions": [{"description": "Potential inconsistency found"}],
                "hallucination_details": [],
                "claims": [{"id": "claim_1"}],
            },
            "pass_cache": [],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Usage Example
# ─────────────────────────────────────────────────────────────────────────────


async def main():
    """Example: Run reasoning and publish findings."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    engine = DeepThinkEngineWithFindings()

    # Initialize at startup
    await engine.initialize()

    try:
        # Run reasoning for a specific device
        result = await engine.deep_think_with_findings(
            question="Is this network traffic anomalous?",
            device_id="ant_001",
            passes=3,
        )

        log.info("Reasoning result: %s", result.get("final_answer", "N/A"))

        # Simulate more reasoning tasks
        for i in range(3):
            result = await engine.deep_think_with_findings(
                question=f"Analyze scenario {i+1}",
                device_id=f"ant_{i+1:03d}",
                passes=2,
            )
            await asyncio.sleep(1)  # Small delay between tasks

    finally:
        # Shutdown at teardown
        await engine.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
