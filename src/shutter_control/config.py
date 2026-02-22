import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class EnOceanConfig:
    port: str = "/dev/ttyUSB0"


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str = ""
    password: str = ""
    base_topic: str = "enocean"


@dataclass
class ShutterConfig:
    id: str
    name: str
    full_close_time: float = 25.0
    full_open_time: float = 23.0
    sender_offset: int | None = None
    invert_direction: bool = False

    @property
    def device_id(self) -> list[int]:
        """Parse '05:12:34:56' into [0x05, 0x12, 0x34, 0x56]."""
        return [int(x, 16) for x in self.id.split(":")]

    @property
    def safe_id(self) -> str:
        """Return ID suitable for use in MQTT topics (no colons)."""
        return self.id.replace(":", "").lower()


@dataclass
class AppConfig:
    enocean: EnOceanConfig = field(default_factory=EnOceanConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    shutters: list[ShutterConfig] = field(default_factory=list)
    position_file: str = "positions.json"


def load_options(path: str | Path) -> AppConfig:
    """Load config from the HA Supervisor's /data/options.json."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Options file not found: {path}")

    with open(path) as f:
        raw = json.load(f)

    enocean_cfg = EnOceanConfig(port=raw["enocean_port"])
    mqtt_cfg = MqttConfig(
        host=raw["mqtt_host"],
        port=raw["mqtt_port"],
        username=raw.get("mqtt_username", ""),
        password=raw.get("mqtt_password", ""),
        base_topic=raw.get("mqtt_base_topic", "enocean"),
    )

    shutters = [ShutterConfig(**s) for s in raw.get("shutters", [])]
    if not shutters:
        raise ValueError("No shutters defined in options")

    return AppConfig(
        enocean=enocean_cfg,
        mqtt=mqtt_cfg,
        shutters=shutters,
        position_file="/data/positions.json",
    )


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    enocean_cfg = EnOceanConfig(**raw.get("enocean", {}))
    mqtt_cfg = MqttConfig(**raw.get("mqtt", {}))

    shutters = []
    for s in raw.get("shutters", []):
        shutters.append(ShutterConfig(**s))

    if not shutters:
        raise ValueError("No shutters defined in config")

    return AppConfig(
        enocean=enocean_cfg,
        mqtt=mqtt_cfg,
        shutters=shutters,
        position_file=raw.get("position_file", "positions.json"),
    )
