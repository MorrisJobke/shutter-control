"""Shutter Control: EnOcean-to-MQTT bridge for Eltako FSB61NP-230V."""

import asyncio
import logging
import signal
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

from bs4 import XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from .config import ShutterConfig, load_config
from .enocean_gateway import Direction, EnOceanGateway, StatusEvent
from .mqtt_handler import MqttHandler
from .position_tracker import (
    MotionState,
    PositionTracker,
    ShutterState,
    ShutterTrackerConfig,
)

logger = logging.getLogger("shutter_control")

# Lookup from safe_id -> ShutterConfig
_shutters_by_safe_id: dict[str, ShutterConfig] = {}
# Lookup from EnOcean device ID (upper-case) -> safe_id
_id_to_safe: dict[str, str] = {}
# Lookup from safe_id -> sender offset (0, 1, 2, ...) for unique addressing
_sender_offsets: dict[str, int] = {}

COMMAND_ACK_TIMEOUT = 5.0  # seconds to wait for RPS moving confirmation before retrying


@dataclass
class _PendingCommand:
    command: str   # "OPEN" or "CLOSE"
    sent_at: float
    retried: bool = False


_pending_commands: dict[str, _PendingCommand] = {}


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _invert(direction: Direction) -> Direction:
    """Swap UP/DOWN for actuators with reversed motor wiring."""
    if direction == Direction.UP:
        return Direction.DOWN
    if direction == Direction.DOWN:
        return Direction.UP
    return direction


def _handle_command(
    safe_id: str,
    command: str,
    gateway: EnOceanGateway,
    tracker: PositionTracker,
) -> None:
    """Handle OPEN/CLOSE/STOP from MQTT."""
    shutter = _shutters_by_safe_id.get(safe_id)
    if not shutter:
        return

    dest = shutter.device_id
    offset = _sender_offsets.get(safe_id, 0)

    inv = shutter.invert_direction

    if command == "OPEN":
        gateway.send_command(dest, _invert(Direction.UP) if inv else Direction.UP, shutter.full_open_time, offset)
        tracker.start_moving(safe_id, MotionState.OPENING)
    elif command == "CLOSE":
        gateway.send_command(dest, _invert(Direction.DOWN) if inv else Direction.DOWN, shutter.full_close_time, offset)
        tracker.start_moving(safe_id, MotionState.CLOSING)
    elif command == "STOP":
        gateway.send_command(dest, Direction.STOP, sender_offset=offset)
        tracker.stop(safe_id)


def _on_mqtt_command(
    safe_id: str,
    command: str,
    gateway: EnOceanGateway,
    tracker: PositionTracker,
) -> None:
    """Handle OPEN/CLOSE/STOP from MQTT and track pending acknowledgements."""
    _handle_command(safe_id, command, gateway, tracker)
    if command in ("OPEN", "CLOSE"):
        _pending_commands[safe_id] = _PendingCommand(command=command, sent_at=time.monotonic())
    elif command == "STOP":
        _pending_commands.pop(safe_id, None)


def _handle_set_position(
    safe_id: str,
    target: int,
    gateway: EnOceanGateway,
    tracker: PositionTracker,
) -> None:
    """Handle set_position (0-100) from MQTT."""
    shutter = _shutters_by_safe_id.get(safe_id)
    if not shutter:
        return

    current_state = tracker.get_state(safe_id)
    if not current_state:
        return

    current_pos = current_state.position
    diff = target - current_pos

    if abs(diff) < 2:
        # Already close enough
        return

    dest = shutter.device_id

    offset = _sender_offsets.get(safe_id, 0)

    inv = shutter.invert_direction

    if diff > 0:
        # Need to open (move up)
        travel_fraction = diff / 100.0
        drive_time = travel_fraction * shutter.full_open_time
        direction = _invert(Direction.UP) if inv else Direction.UP
        gateway.send_command(dest, direction, drive_time, offset)
        tracker.start_moving(safe_id, MotionState.OPENING, target_position=float(target))
    else:
        # Need to close (move down)
        travel_fraction = abs(diff) / 100.0
        drive_time = travel_fraction * shutter.full_close_time
        direction = _invert(Direction.DOWN) if inv else Direction.DOWN
        gateway.send_command(dest, direction, drive_time, offset)
        tracker.start_moving(safe_id, MotionState.CLOSING, target_position=float(target))


def _handle_enocean_status(
    event: StatusEvent,
    tracker: PositionTracker,
) -> None:
    """Handle status telegrams from EnOcean actuators."""
    sender = event.sender_id.upper()

    safe_id = _id_to_safe.get(sender)
    if safe_id:
        _apply_status_to_shutter(safe_id, event, tracker)
        return

    logger.debug("Ignoring status from unknown device %s", event.sender_id)


def _apply_status_to_shutter(
    safe_id: str,
    event: StatusEvent,
    tracker: PositionTracker,
) -> None:
    """Apply a status event from an actuator to the position tracker.

    Ignores standard-rocker-format telegrams from actuators, as these
    are end-position notifications (sent minutes after the motor stopped),
    not real movement status.
    """
    if event.stopped:
        _pending_commands.pop(safe_id, None)
        tracker.stop(safe_id)
        return

    if event.is_standard_rocker:
        logger.debug(
            "Ignoring end-position notification from actuator %s", safe_id
        )
        return

    # Motor confirmed moving — command acknowledged
    _pending_commands.pop(safe_id, None)

    direction = event.direction
    shutter = _shutters_by_safe_id.get(safe_id)
    if shutter and shutter.invert_direction and direction is not None:
        direction = _invert(direction)

    # Preserve any active target (e.g. a set_position in progress)
    active_target = tracker.get_target(safe_id)

    if direction == Direction.UP:
        tracker.start_moving(safe_id, MotionState.OPENING, target_position=active_target)
    elif direction == Direction.DOWN:
        tracker.start_moving(safe_id, MotionState.CLOSING, target_position=active_target)


def _handle_teach_in(safe_id: str, gateway: EnOceanGateway) -> None:
    """Send teach-in telegram for a shutter."""
    shutter = _shutters_by_safe_id.get(safe_id)
    if not shutter:
        return
    offset = _sender_offsets.get(safe_id, 0)
    gateway.send_teach_in(shutter.device_id, offset)


def _handle_position_update(
    safe_id: str,
    state: ShutterState,
    mqtt_handler: MqttHandler,
) -> None:
    """Publish updated position/state to MQTT."""
    mqtt_handler.publish_state(safe_id, state.ha_state, state.ha_position)


async def _position_check_loop(
    tracker: PositionTracker,
    gateway: EnOceanGateway,
    mqtt_handler: MqttHandler,
    interval: float = 0.5,
) -> None:
    """Periodically check if shutters reached their target and publish updates."""
    while True:
        # Retry commands that were never acknowledged by the actuator
        now = time.monotonic()
        for safe_id, pending in list(_pending_commands.items()):
            if now - pending.sent_at < COMMAND_ACK_TIMEOUT:
                continue
            shutter = _shutters_by_safe_id.get(safe_id)
            name = shutter.name if shutter else safe_id
            if pending.retried:
                logger.warning(
                    "Shutter %s (%s): no response after retry, giving up",
                    name, safe_id,
                )
                del _pending_commands[safe_id]
            else:
                logger.warning(
                    "Shutter %s (%s): no response to %s after %.0fs, retrying",
                    name, safe_id, pending.command, COMMAND_ACK_TIMEOUT,
                )
                _handle_command(safe_id, pending.command, gateway, tracker)
                _pending_commands[safe_id] = _PendingCommand(
                    command=pending.command, sent_at=time.monotonic(), retried=True
                )

        # Check for shutters that reached their target
        stop_ids = tracker.check_targets()
        for safe_id in stop_ids:
            shutter = _shutters_by_safe_id.get(safe_id)
            if shutter:
                offset = _sender_offsets.get(safe_id, 0)
                gateway.send_command(shutter.device_id, Direction.STOP, sender_offset=offset)
                tracker.stop(safe_id)

        # Publish current positions for moving shutters
        for safe_id in _shutters_by_safe_id:
            state = tracker.get_state(safe_id)
            if state and state.motion != MotionState.STOPPED:
                mqtt_handler.publish_state(safe_id, state.ha_state, state.ha_position)

        await asyncio.sleep(interval)


async def _run(config_path: str) -> None:
    config = load_config(config_path)

    # Build lookup tables — each shutter gets a stable sender offset
    # derived from its device ID so that adding/reordering shutters
    # doesn't require re-teaching actuators.  Can be overridden via
    # sender_offset in the config to resolve collisions.
    seen_offsets: dict[int, ShutterConfig] = {}
    for shutter in config.shutters:
        _shutters_by_safe_id[shutter.safe_id] = shutter
        _id_to_safe[shutter.id.upper()] = shutter.safe_id

        if shutter.sender_offset is not None:
            offset = shutter.sender_offset
        else:
            offset = shutter.device_id[-1] % 128

        if offset in seen_offsets:
            other = seen_offsets[offset]
            raise ValueError(
                f"Sender offset collision: {shutter.name} ({shutter.id}) and "
                f"{other.name} ({other.id}) both use offset {offset}. "
                f"Set sender_offset on one of them to resolve this."
            )

        seen_offsets[offset] = shutter
        _sender_offsets[shutter.safe_id] = offset
        logger.info(
            "Shutter %s (%s) using sender offset %d%s",
            shutter.name,
            shutter.id,
            offset,
            " (from config)" if shutter.sender_offset is not None else "",
        )

    # Initialize position tracker
    tracker = PositionTracker(persistence_path=config.position_file)
    for shutter in config.shutters:
        tracker.register_shutter(
            ShutterTrackerConfig(
                shutter_id=shutter.safe_id,
                full_close_time=shutter.full_close_time,
                full_open_time=shutter.full_open_time,
            )
        )
    tracker.load_positions()

    # Initialize MQTT
    mqtt_handler = MqttHandler(config.mqtt, config.shutters)

    # Wire callbacks
    mqtt_handler.set_command_callback(
        lambda sid, cmd: _on_mqtt_command(sid, cmd, gateway, tracker)
    )
    mqtt_handler.set_position_callback(
        lambda sid, pos: _handle_set_position(sid, pos, gateway, tracker)
    )
    mqtt_handler.set_teach_in_callback(
        lambda sid: _handle_teach_in(sid, gateway)
    )
    tracker.set_update_callback(
        lambda sid, state: _handle_position_update(sid, state, mqtt_handler)
    )

    # Initialize EnOcean gateway
    gateway = EnOceanGateway(config.enocean.port)
    gateway.set_status_callback(
        lambda event: _handle_enocean_status(event, tracker)
    )

    # Start everything
    gateway.start()
    mqtt_handler.start()

    logger.info(
        "Shutter control started with %d shutter(s)",
        len(config.shutters),
    )

    # Set up shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(sig: signal.Signals) -> None:
        logger.info("Received %s, shutting down...", sig.name)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)

    # Run position check loop until shutdown
    check_task = asyncio.create_task(
        _position_check_loop(tracker, gateway, mqtt_handler)
    )

    await stop_event.wait()

    check_task.cancel()
    try:
        await check_task
    except asyncio.CancelledError:
        pass

    # Cleanup
    mqtt_handler.stop()
    gateway.stop()
    tracker.save_positions()
    logger.info("Shutdown complete")


def main() -> None:
    _setup_logging()

    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    if not Path(config_path).exists():
        logger.error("Config file not found: %s", config_path)
        logger.error("Copy config.example.yaml to config.yaml and edit it")
        sys.exit(1)

    asyncio.run(_run(config_path))


if __name__ == "__main__":
    main()
