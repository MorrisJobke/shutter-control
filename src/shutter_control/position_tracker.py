"""Time-based position tracker for roller shutters.

Since the Eltako FSB61NP-230V does not report absolute position,
we estimate it from motor travel time.

Convention: 0 = fully closed, 100 = fully open (matches Home Assistant).
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


class MotionState(Enum):
    STOPPED = auto()
    OPENING = auto()
    CLOSING = auto()


@dataclass
class ShutterState:
    position: float = 0.0  # 0=closed, 100=open
    motion: MotionState = MotionState.STOPPED
    _move_start_time: float = 0.0
    _move_start_position: float = 0.0
    _target_position: float | None = None
    _full_travel_time: float = 25.0  # for current direction

    @property
    def ha_state(self) -> str:
        """Return Home Assistant cover state string."""
        if self.motion == MotionState.OPENING:
            return "opening"
        if self.motion == MotionState.CLOSING:
            return "closing"
        return "open" if self.position > 0 else "closed"

    @property
    def ha_position(self) -> int:
        """Return position as integer 0-100 for HA."""
        return max(0, min(100, round(self.position)))


@dataclass
class ShutterTrackerConfig:
    shutter_id: str
    full_close_time: float  # seconds, open->closed
    full_open_time: float   # seconds, closed->open


class PositionTracker:
    """Tracks estimated positions for multiple shutters."""

    def __init__(self, persistence_path: str | Path = "positions.json"):
        self._shutters: dict[str, ShutterState] = {}
        self._configs: dict[str, ShutterTrackerConfig] = {}
        self._persistence_path = Path(persistence_path)
        self._on_update: Callable[[str, ShutterState], None] | None = None

    def set_update_callback(self, callback: Callable[[str, ShutterState], None]) -> None:
        self._on_update = callback

    def register_shutter(self, config: ShutterTrackerConfig) -> None:
        self._configs[config.shutter_id] = config
        if config.shutter_id not in self._shutters:
            self._shutters[config.shutter_id] = ShutterState()

    def load_positions(self) -> None:
        if not self._persistence_path.exists():
            logger.info("No saved positions found at %s", self._persistence_path)
            return
        try:
            with open(self._persistence_path) as f:
                data = json.load(f)
            for shutter_id, pos in data.items():
                if shutter_id in self._shutters:
                    self._shutters[shutter_id].position = float(pos)
                    logger.info("Restored position for %s: %.1f%%", shutter_id, pos)
        except Exception:
            logger.exception("Failed to load positions from %s", self._persistence_path)

    def save_positions(self) -> None:
        try:
            data = {
                sid: state.position
                for sid, state in self._shutters.items()
            }
            with open(self._persistence_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            logger.exception("Failed to save positions to %s", self._persistence_path)

    def get_state(self, shutter_id: str) -> ShutterState | None:
        state = self._shutters.get(shutter_id)
        if state and state.motion != MotionState.STOPPED:
            self._interpolate(shutter_id)
        return state

    def start_moving(
        self, shutter_id: str, direction: MotionState, target_position: float | None = None
    ) -> None:
        """Record that a shutter has started moving."""
        state = self._shutters.get(shutter_id)
        config = self._configs.get(shutter_id)
        if not state or not config:
            logger.warning("Unknown shutter: %s", shutter_id)
            return

        # Interpolate current position first if already moving
        if state.motion != MotionState.STOPPED:
            self._interpolate(shutter_id)

        state.motion = direction
        state._move_start_time = time.monotonic()
        state._move_start_position = state.position
        state._target_position = target_position

        if direction == MotionState.OPENING:
            state._full_travel_time = config.full_open_time
        else:
            state._full_travel_time = config.full_close_time

        logger.info(
            "Shutter %s: %s from %.1f%% (target=%s)",
            shutter_id,
            direction.name,
            state.position,
            f"{target_position:.0f}%" if target_position is not None else "end",
        )
        self._notify(shutter_id)

    def stop(self, shutter_id: str) -> None:
        """Record that a shutter has stopped."""
        state = self._shutters.get(shutter_id)
        if not state:
            return

        if state.motion != MotionState.STOPPED:
            self._interpolate(shutter_id)

        state.motion = MotionState.STOPPED
        state._target_position = None

        # Clamp to 0/100 if very close
        if state.position < 1:
            state.position = 0.0
        elif state.position > 99:
            state.position = 100.0

        logger.info("Shutter %s: stopped at %.1f%%", shutter_id, state.position)
        self.save_positions()
        self._notify(shutter_id)

    def check_targets(self) -> list[str]:
        """Check if any moving shutters have reached their target position.

        Returns list of shutter IDs that need a stop command.
        """
        stop_ids = []
        now = time.monotonic()

        for shutter_id, state in self._shutters.items():
            if state.motion == MotionState.STOPPED:
                continue

            self._interpolate(shutter_id)

            # Check if reached physical limits
            if state.position <= 0 and state.motion == MotionState.CLOSING:
                state.position = 0.0
                state.motion = MotionState.STOPPED
                state._target_position = None
                self.save_positions()
                self._notify(shutter_id)
                continue

            if state.position >= 100 and state.motion == MotionState.OPENING:
                state.position = 100.0
                state.motion = MotionState.STOPPED
                state._target_position = None
                self.save_positions()
                self._notify(shutter_id)
                continue

            # Check if reached target
            if state._target_position is not None:
                reached = False
                if state.motion == MotionState.OPENING:
                    reached = state.position >= state._target_position
                elif state.motion == MotionState.CLOSING:
                    reached = state.position <= state._target_position

                if reached:
                    state.position = state._target_position
                    stop_ids.append(shutter_id)

        return stop_ids

    def _interpolate(self, shutter_id: str) -> None:
        """Update position based on elapsed time."""
        state = self._shutters[shutter_id]
        if state.motion == MotionState.STOPPED:
            return

        elapsed = time.monotonic() - state._move_start_time
        travel_fraction = elapsed / state._full_travel_time
        travel_percent = travel_fraction * 100.0

        if state.motion == MotionState.OPENING:
            state.position = min(100.0, state._move_start_position + travel_percent)
        elif state.motion == MotionState.CLOSING:
            state.position = max(0.0, state._move_start_position - travel_percent)

    def _notify(self, shutter_id: str) -> None:
        if self._on_update:
            state = self._shutters[shutter_id]
            self._on_update(shutter_id, state)
