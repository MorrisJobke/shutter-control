"""Shutter Control: EnOcean-to-MQTT bridge for Eltako FSB61NP-230V."""

import asyncio
import logging
import signal
import sys
import warnings
from pathlib import Path

from bs4 import XMLParsedAsHTMLWarning
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from .config import ButtonConfig, ShutterConfig, load_config
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
# Lookup from button EnOcean ID (upper-case) -> list of shutter safe_ids
_button_to_shutters: dict[str, list[str]] = {}
# Lookup from safe_id -> sender offset (0, 1, 2, ...) for unique addressing
_sender_offsets: dict[str, int] = {}


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


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

    if command == "OPEN":
        gateway.send_command(dest, Direction.UP, shutter.full_open_time, offset)
        tracker.start_moving(safe_id, MotionState.OPENING)
    elif command == "CLOSE":
        gateway.send_command(dest, Direction.DOWN, shutter.full_close_time, offset)
        tracker.start_moving(safe_id, MotionState.CLOSING)
    elif command == "STOP":
        gateway.send_command(dest, Direction.STOP, sender_offset=offset)
        tracker.stop(safe_id)


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

    if diff > 0:
        # Need to open (move up)
        travel_fraction = diff / 100.0
        drive_time = travel_fraction * shutter.full_open_time
        gateway.send_command(dest, Direction.UP, drive_time, offset)
        tracker.start_moving(safe_id, MotionState.OPENING, target_position=float(target))
    else:
        # Need to close (move down)
        travel_fraction = abs(diff) / 100.0
        drive_time = travel_fraction * shutter.full_close_time
        gateway.send_command(dest, Direction.DOWN, drive_time, offset)
        tracker.start_moving(safe_id, MotionState.CLOSING, target_position=float(target))


def _handle_enocean_status(
    event: StatusEvent,
    tracker: PositionTracker,
) -> None:
    """Handle status telegrams from EnOcean devices (actuators and wall buttons)."""
    sender = event.sender_id.upper()

    # Check if this is a known actuator
    safe_id = _id_to_safe.get(sender)
    if safe_id:
        _apply_status_to_shutter(safe_id, event, tracker)
        return

    # Check if this is a known wall button
    shutter_ids = _button_to_shutters.get(sender)
    if shutter_ids:
        _handle_button_event(shutter_ids, event, tracker)
        return

    logger.debug("Ignoring status from unknown device %s", event.sender_id)


def _handle_button_event(
    shutter_ids: list[str],
    event: StatusEvent,
    tracker: PositionTracker,
) -> None:
    """Handle a wall button press for associated shutters.

    Wall buttons are momentary switches — the release does not stop the motor.
    The actuator uses toggle logic:
      - If stopped: start moving in the pressed direction
      - If already moving in same direction: stop
      - If moving in opposite direction: reverse
    """
    if event.stopped or event.direction is None:
        # Momentary release — ignore, the actuator keeps running
        return

    pressed_motion = (
        MotionState.OPENING if event.direction == Direction.UP else MotionState.CLOSING
    )

    for sid in shutter_ids:
        state = tracker.get_state(sid)
        if not state:
            continue

        if state.motion == MotionState.STOPPED:
            logger.info("Button → %s shutter %s", pressed_motion.name, sid)
            tracker.start_moving(sid, pressed_motion)
        elif state.motion == pressed_motion:
            logger.info("Button → STOP shutter %s (same direction toggle)", sid)
            tracker.stop(sid)
        else:
            logger.info("Button → reverse shutter %s to %s", sid, pressed_motion.name)
            tracker.start_moving(sid, pressed_motion)


def _apply_status_to_shutter(
    safe_id: str,
    event: StatusEvent,
    tracker: PositionTracker,
) -> None:
    """Apply a status event to a single shutter's position tracker."""
    if event.stopped:
        tracker.stop(safe_id)
    elif event.direction == Direction.UP:
        tracker.start_moving(safe_id, MotionState.OPENING)
    elif event.direction == Direction.DOWN:
        tracker.start_moving(safe_id, MotionState.CLOSING)


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

    # Build button -> shutters lookup
    for button in config.buttons:
        shutter_safe_ids = []
        for shutter_id in button.shutters:
            safe = shutter_id.replace(":", "").lower()
            if safe not in _shutters_by_safe_id:
                logger.warning(
                    "Button %s references unknown shutter %s", button.id, shutter_id
                )
                continue
            shutter_safe_ids.append(safe)
        if shutter_safe_ids:
            _button_to_shutters[button.safe_id] = shutter_safe_ids
            logger.info(
                "Button %s controls %d shutter(s): %s",
                button.id,
                len(shutter_safe_ids),
                ", ".join(shutter_safe_ids),
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
        lambda sid, cmd: _handle_command(sid, cmd, gateway, tracker)
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
        "Shutter control started with %d shutter(s) and %d button(s)",
        len(config.shutters),
        len(config.buttons),
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
