"""
Test MQTT novelty detection with live Pixel 7 telemetry
"""
import asyncio
import json
import logging
from mqtt_novelty_handler import MQTTNoveltyHandler, SensorClaim

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

async def test_mqtt_pipeline():
    """Test receiving live telemetry and scoring novelty."""
    
    handler = MQTTNoveltyHandler(
        mqtt_host="[REDACTED_MQTT_HOST]",
        mqtt_port=1883,
        mqtt_user="dama",
        mqtt_pass="[REDACTED_MQTT_PASSWORD]",
        nova_url="http://[REDACTED_INTERNAL_IP]:30850",
        ollama_url="http://[REDACTED_INTERNAL_IP]:11434",
    )
    
    print("Starting MQTT novelty handler (listening for 30 seconds)...")
    await handler.start()
    
    # Listen for 30 seconds
    try:
        await asyncio.sleep(30)
    except KeyboardInterrupt:
        pass
    finally:
        await handler.stop()
    
    print("MQTT novelty handler test complete")

if __name__ == "__main__":
    asyncio.run(test_mqtt_pipeline())
