"""MQTT handler with Home Assistant auto-discovery for roller shutters."""

import json
import logging
from typing import Callable

import paho.mqtt.client as mqtt

from .config import MqttConfig, ShutterConfig

logger = logging.getLogger(__name__)


class MqttHandler:
    def __init__(self, config: MqttConfig, shutters: list[ShutterConfig]):
        self._config = config
        self._shutters = {s.safe_id: s for s in shutters}
        self._client: mqtt.Client | None = None
        self._on_command: Callable[[str, str], None] | None = None
        self._on_set_position: Callable[[str, int], None] | None = None
        self._on_teach_in: Callable[[str], None] | None = None

    def set_command_callback(self, callback: Callable[[str, str], None]) -> None:
        """Set callback for OPEN/CLOSE/STOP commands. Args: (shutter_safe_id, command)."""
        self._on_command = callback

    def set_position_callback(self, callback: Callable[[str, int], None]) -> None:
        """Set callback for set_position commands. Args: (shutter_safe_id, position 0-100)."""
        self._on_set_position = callback

    def set_teach_in_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for teach-in requests. Args: (shutter_safe_id,)."""
        self._on_teach_in = callback

    def start(self) -> None:
        base = self._config.base_topic

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="shutter-control",
        )

        if self._config.username:
            self._client.username_pw_set(self._config.username, self._config.password)

        # Last Will and Testament for availability
        for shutter in self._shutters.values():
            self._client.will_set(
                f"{base}/cover/{shutter.safe_id}/available",
                payload="offline",
                retain=True,
            )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        logger.info("Connecting to MQTT broker at %s:%d", self._config.host, self._config.port)
        self._client.connect(self._config.host, self._config.port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        if not self._client:
            return

        base = self._config.base_topic

        # Mark all shutters offline
        for shutter in self._shutters.values():
            self._client.publish(
                f"{base}/cover/{shutter.safe_id}/available",
                payload="offline",
                retain=True,
            )

        self._client.loop_stop()
        self._client.disconnect()
        logger.info("MQTT disconnected")

    def publish_state(self, safe_id: str, state: str, position: int) -> None:
        """Publish cover state and position."""
        if not self._client:
            return

        base = self._config.base_topic
        self._client.publish(
            f"{base}/cover/{safe_id}/state", payload=state, retain=True
        )
        self._client.publish(
            f"{base}/cover/{safe_id}/position",
            payload=str(position),
            retain=True,
        )

    def _on_connect(self, client: mqtt.Client, userdata, flags, rc, properties=None) -> None:
        if rc != 0:
            logger.error("MQTT connection failed with code %d", rc)
            return

        logger.info("Connected to MQTT broker")
        base = self._config.base_topic

        # Subscribe to command topics for all shutters
        for shutter in self._shutters.values():
            sid = shutter.safe_id
            client.subscribe(f"{base}/cover/{sid}/set")
            client.subscribe(f"{base}/cover/{sid}/set_position")
            client.subscribe(f"{base}/cover/{sid}/teach_in")

        # Publish HA discovery configs and mark available
        for shutter in self._shutters.values():
            self._publish_discovery(shutter)
            client.publish(
                f"{base}/cover/{shutter.safe_id}/available",
                payload="online",
                retain=True,
            )

    def _on_message(self, client: mqtt.Client, userdata, msg: mqtt.MQTTMessage) -> None:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        base = self._config.base_topic

        logger.debug("MQTT message: %s = %s", topic, payload)

        # Parse topic: {base}/cover/{safe_id}/set or set_position
        parts = topic.split("/")
        if len(parts) < 4:
            return

        safe_id = parts[-2]
        action = parts[-1]

        if safe_id not in self._shutters:
            return

        if action == "set" and self._on_command:
            command = payload.upper()
            if command in ("OPEN", "CLOSE", "STOP"):
                self._on_command(safe_id, command)
            else:
                logger.warning("Unknown command: %s", payload)

        elif action == "teach_in" and self._on_teach_in:
            logger.info("Teach-in requested for %s", safe_id)
            self._on_teach_in(safe_id)
            return

        elif action == "set_position" and self._on_set_position:
            try:
                pos = int(float(payload))
                pos = max(0, min(100, pos))
                self._on_set_position(safe_id, pos)
            except ValueError:
                logger.warning("Invalid position value: %s", payload)

    def _publish_discovery(self, shutter: ShutterConfig) -> None:
        """Publish Home Assistant MQTT discovery config for a cover."""
        base = self._config.base_topic
        sid = shutter.safe_id

        discovery_topic = f"homeassistant/cover/{sid}/config"

        config_payload = {
            "name": shutter.name,
            "unique_id": f"enocean_cover_{sid}",
            "command_topic": f"{base}/cover/{sid}/set",
            "state_topic": f"{base}/cover/{sid}/state",
            "position_topic": f"{base}/cover/{sid}/position",
            "set_position_topic": f"{base}/cover/{sid}/set_position",
            "availability_topic": f"{base}/cover/{sid}/available",
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": "STOP",
            "state_open": "open",
            "state_closed": "closed",
            "state_opening": "opening",
            "state_closing": "closing",
            "position_open": 100,
            "position_closed": 0,
            "device_class": "shutter",
            "device": {
                "identifiers": [f"enocean_{sid}"],
                "name": f"{shutter.name} Shutter",
                "manufacturer": "Eltako",
                "model": "FSB61NP-230V",
                "via_device": "enocean_gateway",
            },
        }

        self._client.publish(
            discovery_topic,
            payload=json.dumps(config_payload),
            retain=True,
        )
        logger.info("Published HA discovery for %s (%s)", shutter.name, sid)
