"""
MQTT Publisher for the cycling AI agent.

Pushes the daily training prescription to an MQTT broker
(e.g., Home Assistant) for display on a local dashboard.
"""

import json
import logging
import os
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None  # type: ignore

from src import config

_initialized = False


def _ensure_init() -> None:
    """Lazily initialize config on first use."""
    global _initialized
    if not _initialized:
        config.setup()
        _initialized = True

logger = logging.getLogger(__name__)

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "cycling/agent/prescription")
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_QOS = int(os.getenv("MQTT_QOS", "1"))


def publish(prescription: str, metadata: dict[str, Any] | None = None) -> bool:
    """
    Publish a training prescription to the MQTT broker.

    Args:
        prescription: The LLM-generated training plan text.
        metadata: Optional dict with readiness state, thresholds, etc.

    Returns:
        True if published successfully, False otherwise.
    """
    _ensure_init()
    if mqtt is None:
        logger.warning("paho-mqtt not installed; skipping MQTT publish")
        return False

    payload = {
        "prescription": prescription,
        "metadata": metadata or {},
    }

    client = mqtt.Client(mqtt.CallbackAPIVersion.AUTO)  # type: ignore

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    published = False

    def on_connect(client, userdata, flags, rc, properties=None):
        nonlocal published
        if rc == 0:
            client.publish(MQTT_TOPIC, json.dumps(payload), qos=MQTT_QOS)
            published = True
            logger.info(f"Published prescription to {MQTT_TOPIC}")
        else:
            logger.error(f"MQTT connect failed with code {rc}")

    def on_disconnect(client, userdata, rc, properties=None):
        if rc != 0:
            logger.error(f"Unexpected MQTT disconnect with code {rc}")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 10)
        client.loop_start()
        client.loop_wait()
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        logger.error(f"MQTT publish failed: {e}")
        return False

    return published