"""
MQTT Novelty Detection Handler
Verifies sensor readings against Great Library + scores with Ollama.

Architecture:
  1. Subscribe to dama/{deviceId}/telemetry
  2. Extract sensor claim (WiFi + location + cellular)
  3. Query Nova for historical matches
  4. Score novelty with Ollama
  5. Publish dama/{deviceId}/novelty_verification
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

import aiohttp
import paho.mqtt.client as mqtt

try:
    import pyotp
except ImportError:
    pyotp = None

log = logging.getLogger(__name__)


@dataclass
class SensorClaim:
    """Extracted sensor reading from DAMA telemetry."""
    device_id: str
    timestamp: str
    wifi_ssid: Optional[str] = None
    wifi_bssid: Optional[str] = None
    wifi_rssi: Optional[int] = None
    cell_carrier: Optional[str] = None
    cell_rsrp: Optional[int] = None
    location_lat: Optional[float] = None
    location_lon: Optional[float] = None
    location_accuracy: Optional[float] = None

    def to_claim_text(self) -> str:
        """Convert sensor reading to natural language claim."""
        parts = []
        if self.wifi_ssid and self.wifi_bssid:
            parts.append(f"WiFi SSID={self.wifi_ssid}, BSSID={self.wifi_bssid} (signal={self.wifi_rssi}dBm)")
        if self.location_lat and self.location_lon:
            parts.append(f"at location [{self.location_lat:.4f}, {self.location_lon:.4f}] (accuracy={self.location_accuracy}m)")
        if self.cell_carrier and self.cell_rsrp:
            parts.append(f"with {self.cell_carrier} cellular (signal={self.cell_rsrp}dBm)")
        return ", ".join(parts) if parts else "unknown sensor reading"


class NoveltyScorer:
    """Scores sensor novelty using Nova + Ollama."""

    def __init__(
        self,
        nova_url: str = "http://100.73.200.19:30850",
        ollama_url: str = "http://100.73.200.19:11434",
        nova_token: Optional[str] = None,
        nova_totp_seed: Optional[str] = None,
    ):
        self.nova_url = nova_url
        self.ollama_url = ollama_url
        self.nova_token = nova_token or os.getenv("NOVA_TOKEN", "")
        self.nova_totp_seed = nova_totp_seed or os.getenv("NOVA_TOTP_SEED", "")

    async def score_novelty(self, claim: SensorClaim) -> Dict[str, Any]:
        """
        Score novelty of sensor reading.

        Returns:
            {
                "claim": "WiFi SSID=...",
                "historical_match": {"found": bool, "similar_count": int, "closest_match_age_days": int},
                "novelty_score": 0-100,
                "confidence": 0.0-1.0,
                "reasoning": "..."
            }
        """
        claim_text = claim.to_claim_text()
        log.info(f"Scoring novelty for {claim.device_id}: {claim_text}")

        # Step 1: Query Nova for historical matches
        historical_match = await self._query_nova(claim_text)

        # Step 2: Score with Ollama
        novelty_score, reasoning = await self._score_with_ollama(
            claim_text, historical_match
        )

        return {
            "claim": claim_text,
            "historical_match": historical_match,
            "novelty_score": novelty_score,
            "confidence": historical_match.get("confidence", 0.8),
            "reasoning": reasoning,
        }

    async def _query_nova(self, claim_text: str) -> Dict[str, Any]:
        """Query Nova for historical matches of this sensor reading."""
        try:
            async with aiohttp.ClientSession() as session:
                # Call nova_search endpoint with Bearer token auth
                payload = {
                    "query": claim_text,
                    "top": 3,  # Get top 3 similar readings
                }
                
                headers = {}
                if self.nova_token:
                    headers["Authorization"] = f"Bearer {self.nova_token}"

                async with session.post(
                    f"{self.nova_url}/search",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        return {
                            "found": len(results) > 0,
                            "similar_count": len(results),
                            "closest_match_age_days": results[0].get("age_days", 999) if results else 999,
                            "confidence": results[0].get("score", 0.0) if results else 0.0,
                        }
                    else:
                        log.warning(f"Nova search failed: {resp.status}")
                        return {"found": False, "similar_count": 0, "closest_match_age_days": 999, "confidence": 0.0}

        except asyncio.TimeoutError:
            log.warning("Nova search timeout")
            return {"found": False, "similar_count": 0, "closest_match_age_days": 999, "confidence": 0.0}
        except Exception as e:
            log.error(f"Nova query failed: {e}")
            return {"found": False, "similar_count": 0, "closest_match_age_days": 999, "confidence": 0.0}

    async def _score_with_ollama(
        self, claim_text: str, historical_match: Dict[str, Any]
    ) -> tuple[int, str]:
        """Use Ollama to score novelty of this reading."""
        try:
            prompt = f"""You are a sensor data analyst. Given a sensor reading and historical context, score how novel (new/unseen) this reading is.

Sensor Reading: {claim_text}

Historical Context:
- Similar readings found: {historical_match['similar_count']}
- Age of closest match: {historical_match['closest_match_age_days']} days
- Confidence in match: {historical_match['confidence']:.2f}

Based on this context, provide:
1. A novelty score (0-100, where 100 = completely novel/never seen before)
2. Brief reasoning (1-2 sentences)

Format your response as JSON:
{{"novelty_score": <0-100>, "reasoning": "<brief explanation>"}}"""

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.ollama_url}/api/generate",
                    json={
                        "model": "llama3.1:8b",
                        "prompt": prompt,
                        "stream": False,
                        "temperature": 0.5,
                    },
                    timeout=aiohttp.ClientTimeout(total=60),  # Increased to 60s for inference
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        response_text = data.get("response", "")

                        # Parse JSON from response
                        try:
                            # Extract JSON from response (Ollama might add extra text)
                            json_start = response_text.find("{")
                            json_end = response_text.rfind("}") + 1
                            if json_start >= 0 and json_end > json_start:
                                json_str = response_text[json_start:json_end]
                                result = json.loads(json_str)
                                return (
                                    min(100, max(0, result.get("novelty_score", 50))),
                                    result.get("reasoning", "Ollama evaluation"),
                                )
                        except json.JSONDecodeError:
                            log.warning(f"Failed to parse Ollama response: {response_text}")

                        # Fallback: extract number from response
                        import re
                        match = re.search(r"(\d+)", response_text)
                        score = int(match.group(1)) if match else 50
                        return (score, "Ollama evaluation (parsed from text)")

                    else:
                        log.warning(f"Ollama generate failed: {resp.status}")
                        return (50, "Ollama unavailable")

        except asyncio.TimeoutError:
            log.warning("Ollama timeout (inference may be slow on first call)")
            return (50, "Ollama timeout")
        except Exception as e:
            log.error(f"Ollama scoring failed: {e}")
            return (50, f"Ollama error: {str(e)}")


class MQTTNoveltyPublisher:
    """Publishes novelty verification results back to MQTT."""

    def __init__(
        self,
        broker_host: str = "botnet.floppydicks.net",
        broker_port: int = 1883,
        username: str = "dama",
        password: str = "",
    ):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.username = username
        self.password = password
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.connected = False

    def connect(self):
        """Connect to MQTT broker."""
        self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.connect(self.broker_host, self.broker_port, keepalive=60)
        self.client.loop_start()
        return self

    def disconnect(self):
        """Disconnect from MQTT broker."""
        self.client.loop_stop()
        self.client.disconnect()

    def publish_novelty(self, device_id: str, result: Dict[str, Any]) -> bool:
        """Publish novelty verification result to MQTT."""
        topic = f"dama/{device_id}/novelty_verification"
        payload = json.dumps(result)
        msg_info = self.client.publish(topic, payload, qos=1)
        log.info(f"Published to {topic}: novelty_score={result.get('novelty_score')}")
        return msg_info.is_published()

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties):
        if reason_code == 0:
            self.connected = True
            log.info("MQTT connected")
        else:
            log.error(f"MQTT connection failed: {reason_code}")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected = False
        log.warning(f"MQTT disconnected: {reason_code}")


class MQTTNoveltyHandler:
    """Main handler: subscribe to telemetry, score novelty, publish results."""

    def __init__(
        self,
        mqtt_host: str = "botnet.floppydicks.net",
        mqtt_port: int = 1883,
        mqtt_user: str = "dama",
        mqtt_pass: str = "",
        nova_url: str = "http://100.73.200.19:30850",
        ollama_url: str = "http://100.73.200.19:11434",
    ):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_user = mqtt_user
        self.mqtt_pass = mqtt_pass

        self.scorer = NoveltyScorer(nova_url=nova_url, ollama_url=ollama_url)
        self.publisher = MQTTNoveltyPublisher(
            broker_host=mqtt_host,
            broker_port=mqtt_port,
            username=mqtt_user,
            password=mqtt_pass,
        )

        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.running = False

    async def start(self):
        """Start the MQTT novelty handler."""
        log.info("Starting MQTT novelty handler...")

        # Setup MQTT client
        self.client.username_pw_set(self.mqtt_user, self.mqtt_pass)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        # Connect in a thread
        self.client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
        self.client.loop_start()

        # Subscribe to telemetry
        self.client.subscribe("dama/+/telemetry", qos=1)
        self.running = True
        log.info("MQTT novelty handler started")

    async def stop(self):
        """Stop the MQTT novelty handler."""
        self.running = False
        self.client.loop_stop()
        self.client.disconnect()
        log.info("MQTT novelty handler stopped")

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected, subscribing to dama/+/telemetry")
            client.subscribe("dama/+/telemetry", qos=1)
        else:
            log.error(f"MQTT connection failed: {reason_code}")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT disconnected (clean)")
        else:
            log.warning(f"MQTT disconnected: {reason_code}")

    def _on_message(self, client, userdata, msg):
        """Handle incoming telemetry message."""
        try:
            payload = json.loads(msg.payload.decode())
            device_id = payload.get("device_id", "unknown")

            # Extract sensor claim from nested structure
            wifi_data = payload.get("wifi", {})
            gps_data = payload.get("gps", {})
            cell_data = payload.get("cell", {})
            
            claim = SensorClaim(
                device_id=device_id,
                timestamp=str(payload.get("ts", "")),
                wifi_ssid=wifi_data.get("ssid"),
                wifi_bssid=wifi_data.get("bssid"),
                wifi_rssi=wifi_data.get("rssi"),
                cell_carrier=cell_data.get("nearby_cells", [{}])[0].get("cell_id", "").split("-")[0] if cell_data.get("nearby_cells") else None,
                cell_rsrp=cell_data.get("nearby_cells", [{}])[0].get("rsrp") if cell_data.get("nearby_cells") else None,
                location_lat=gps_data.get("latitude"),
                location_lon=gps_data.get("longitude"),
                location_accuracy=gps_data.get("accuracy_m"),
            )

            # Score novelty (synchronous in thread)
            import concurrent.futures
            if not hasattr(self, '_executor'):
                self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
            
            future = self._executor.submit(self._handle_telemetry_sync, claim)

        except Exception as e:
            log.error(f"Failed to process message: {e}")

    def _handle_telemetry_sync(self, claim: SensorClaim):
        """Process telemetry claim synchronously (runs in thread pool)."""
        try:
            import asyncio
            
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # Score novelty
            result = loop.run_until_complete(self.scorer.score_novelty(claim))
            
            # Publish result
            self.publisher.connect()
            self.publisher.publish_novelty(claim.device_id, result)
            self.publisher.disconnect()
            
            loop.close()

        except Exception as e:
            log.error(f"Failed to handle telemetry: {e}")


# Command-line testing
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Test scoring
    async def test():
        scorer = NoveltyScorer()
        claim = SensorClaim(
            device_id="pixel7",
            timestamp="2026-05-01T14:49:49Z",
            wifi_ssid="MyNetwork",
            wifi_bssid="AA:BB:CC:DD:EE:FF",
            wifi_rssi=-45,
            location_lat=40.7128,
            location_lon=-74.0060,
            location_accuracy=5.2,
        )

        result = await scorer.score_novelty(claim)
        print(json.dumps(result, indent=2))

    asyncio.run(test())
