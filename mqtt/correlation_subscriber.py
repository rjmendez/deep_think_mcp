"""MQTT subscriber for correlation engine integration.

Listens to dama/+/findings topic, feeds findings to CorrelationEngine,
publishes CorrelationFindings to dama/correlations/+ and database.
"""

import asyncio
import json
import logging
from typing import Callable, Optional
import paho.mqtt.client as mqtt

from .correlation_engine import CorrelationEngine
from .models import Finding, CorrelationFinding
from .feedback_store import FeedbackStore


logger = logging.getLogger(__name__)


class CorrelationSubscriber:
    """MQTT subscriber for real-time correlation detection.
    
    Subscribes to findings stream (dama/+/findings), processes through
    CorrelationEngine, and publishes correlations.
    """
    
    def __init__(self, 
                 mqtt_client: mqtt.Client,
                 feedback_store: FeedbackStore,
                 on_correlation: Optional[Callable] = None):
        """Initialize correlation subscriber.
        
        Args:
            mqtt_client: Connected MQTT client
            feedback_store: FeedbackStore for persistence
            on_correlation: Optional callback when correlation is found
        """
        self.mqtt_client = mqtt_client
        self.feedback_store = feedback_store
        self.on_correlation = on_correlation
        
        # Initialize correlation engine
        self.engine = CorrelationEngine(
            time_window_sec=10,
            location_radius_m=10,
            min_devices_for_correlation=2
        )
        
        # Subscribe to findings
        self.mqtt_client.subscribe("dama/+/findings", qos=0)
        self.mqtt_client.message_callback_add(
            "dama/+/findings",
            self._on_finding_message
        )
        
        logger.info("CorrelationSubscriber initialized")
    
    def _on_finding_message(self, client: mqtt.Client, userdata, msg: mqtt.MQTTMessage) -> None:
        """Handle incoming finding from MQTT.
        
        Args:
            client: MQTT client
            userdata: User data (unused)
            msg: MQTT message
        """
        try:
            payload = json.loads(msg.payload.decode())
            
            # Reconstruct Finding object
            finding = Finding.from_dict(payload)
            
            # Process through correlation engine (in async context)
            asyncio.create_task(self._process_finding(finding))
        
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to parse finding message: {e}")
        except Exception as e:
            logger.error(f"Error processing finding: {e}", exc_info=True)
    
    async def _process_finding(self, finding: Finding) -> None:
        """Process finding through correlation engine.
        
        Args:
            finding: The finding to process
        """
        try:
            # Feed to correlation engine
            correlation = await self.engine.on_finding(finding)
            
            if correlation:
                await self._handle_correlation(correlation)
        
        except Exception as e:
            logger.error(f"Error in correlation processing: {e}", exc_info=True)
    
    async def _handle_correlation(self, correlation: CorrelationFinding) -> None:
        """Handle a completed correlation finding.
        
        Args:
            correlation: The correlation finding
        """
        try:
            # Persist to database
            await self.feedback_store.record_correlation(
                correlation_id=correlation.id,
                timestamp=correlation.timestamp,
                location_hash=correlation.location_hash,
                observing_devices=correlation.observing_devices,
                sensor_snapshot=correlation.sensor_snapshot,
                novelty_score=correlation.novelty_score,
                fleet_prevalence=correlation.fleet_prevalence,
                entropy_breakdown=correlation.entropy_breakdown,
                is_anomalous_cluster=correlation.is_anomalous_cluster,
                anomaly_details=correlation.anomaly_details,
            )
            
            # Publish to MQTT
            self._publish_correlation(correlation)
            
            # Call user callback if provided
            if self.on_correlation:
                self.on_correlation(correlation)
            
            logger.info(
                f"Correlation complete: {correlation.id} "
                f"novelty={correlation.novelty_score:.3f}"
            )
        
        except Exception as e:
            logger.error(f"Error handling correlation: {e}", exc_info=True)
    
    def _publish_correlation(self, correlation: CorrelationFinding) -> None:
        """Publish correlation to MQTT topic.
        
        Args:
            correlation: The correlation to publish
        """
        try:
            topic = f"dama/correlations/{correlation.id}"
            payload = json.dumps(correlation.to_dict(), default=str)
            
            self.mqtt_client.publish(topic, payload, qos=1, retain=False)
            logger.debug(f"Published correlation to {topic}")
        
        except Exception as e:
            logger.error(f"Failed to publish correlation: {e}")


class CorrelationPublisher:
    """MQTT publisher for manual correlation findings.
    
    Can publish pre-computed correlations directly without subscriber.
    """
    
    def __init__(self, mqtt_client: mqtt.Client):
        """Initialize publisher.
        
        Args:
            mqtt_client: Connected MQTT client
        """
        self.mqtt_client = mqtt_client
    
    async def publish_correlation(self, correlation: CorrelationFinding) -> None:
        """Publish a correlation finding.
        
        Args:
            correlation: The correlation to publish
        """
        try:
            topic = f"dama/correlations/{correlation.id}"
            payload = json.dumps(correlation.to_dict(), default=str)
            
            self.mqtt_client.publish(topic, payload, qos=1, retain=False)
            logger.info(f"Published correlation: {correlation.id}")
        
        except Exception as e:
            logger.error(f"Failed to publish correlation: {e}")
