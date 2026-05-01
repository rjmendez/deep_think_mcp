#!/usr/bin/env python3
"""
MQTT Novelty Verification Service
Runs as a k3s deployment sidecar to Nova.

Environment variables:
  MQTT_HOST              MQTT broker hostname (default: [REDACTED_MQTT_HOST])
  MQTT_PORT              MQTT broker port (default: 1883)
  MQTT_USERNAME          MQTT username (default: dama)
  MQTT_PASSWORD          MQTT password (required)
  NOVA_URL               Nova endpoint (default: http://[REDACTED_INTERNAL_IP]:30850)
  OLLAMA_URL             Ollama endpoint (default: http://[REDACTED_INTERNAL_IP]:11434)
  LOG_LEVEL              Logging level (default: INFO)
"""

import asyncio
import logging
import os
import signal
import sys
from mqtt_novelty_handler import MQTTNoveltyHandler


def main():
    # Setup logging
    log_level = os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    log = logging.getLogger("mqtt_novelty_service")

    # Load configuration from environment
    mqtt_host = os.getenv("MQTT_HOST", "[REDACTED_MQTT_HOST]")
    mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
    mqtt_user = os.getenv("MQTT_USERNAME", "dama")
    mqtt_pass = os.getenv("MQTT_PASSWORD", "")
    nova_url = os.getenv("NOVA_URL", "http://[REDACTED_INTERNAL_IP]:30850")
    ollama_url = os.getenv("OLLAMA_URL", "http://[REDACTED_INTERNAL_IP]:11434")

    if not mqtt_pass:
        log.error("MQTT_PASSWORD not set!")
        sys.exit(1)

    log.info(f"Starting MQTT novelty service")
    log.info(f"  MQTT: {mqtt_host}:{mqtt_port}")
    log.info(f"  Nova: {nova_url}")
    log.info(f"  Ollama: {ollama_url}")

    # Create handler
    handler = MQTTNoveltyHandler(
        mqtt_host=mqtt_host,
        mqtt_port=mqtt_port,
        mqtt_user=mqtt_user,
        mqtt_pass=mqtt_pass,
        nova_url=nova_url,
        ollama_url=ollama_url,
    )

    # Setup graceful shutdown
    def signal_handler(sig, frame):
        log.info("Shutdown signal received, stopping service...")
        asyncio.create_task(handler.stop())

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Run
    async def run():
        await handler.start()
        try:
            while handler.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await handler.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Service interrupted")
        sys.exit(0)


if __name__ == "__main__":
    main()
