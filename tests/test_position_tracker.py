"""Unit tests for the position tracker."""

import json
import time
from unittest.mock import patch

import pytest

from shutter_control.position_tracker import (
    MotionState,
    PositionTracker,
    ShutterTrackerConfig,
)


@pytest.fixture
def tracker(tmp_path):
    t = PositionTracker(persistence_path=tmp_path / "positions.json")
    t.register_shutter(
        ShutterTrackerConfig(
            shutter_id="test1",
            full_close_time=20.0,  # 20s to close
            full_open_time=18.0,   # 18s to open
        )
    )
    return t


class TestBasicPosition:
    def test_initial_position_is_zero(self, tracker):
        state = tracker.get_state("test1")
        assert state is not None
        assert state.position == 0.0
        assert state.motion == MotionState.STOPPED

    def test_unknown_shutter_returns_none(self, tracker):
        assert tracker.get_state("nonexistent") is None


class TestMotionTracking:
    def test_start_opening(self, tracker):
        tracker.start_moving("test1", MotionState.OPENING)
        state = tracker.get_state("test1")
        assert state.motion == MotionState.OPENING

    def test_start_closing(self, tracker):
        # Set position to 100 first
        tracker._shutters["test1"].position = 100.0
        tracker.start_moving("test1", MotionState.CLOSING)
        state = tracker.get_state("test1")
        assert state.motion == MotionState.CLOSING

    def test_stop(self, tracker):
        tracker.start_moving("test1", MotionState.OPENING)
        tracker.stop("test1")
        state = tracker.get_state("test1")
        assert state.motion == MotionState.STOPPED

    def test_position_interpolation_opening(self, tracker):
        """After moving up for half the travel time, position should be ~50%."""
        tracker._shutters["test1"].position = 0.0

        with patch("shutter_control.position_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            tracker.start_moving("test1", MotionState.OPENING)

            # Simulate 9 seconds elapsed (half of 18s open time)
            mock_time.monotonic.return_value = 1009.0
            state = tracker.get_state("test1")
            assert 49.0 <= state.position <= 51.0

    def test_position_interpolation_closing(self, tracker):
        """After closing for half the travel time, position should be ~50%."""
        tracker._shutters["test1"].position = 100.0

        with patch("shutter_control.position_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            tracker.start_moving("test1", MotionState.CLOSING)

            # Simulate 10 seconds elapsed (half of 20s close time)
            mock_time.monotonic.return_value = 1010.0
            state = tracker.get_state("test1")
            assert 49.0 <= state.position <= 51.0

    def test_position_clamped_at_100(self, tracker):
        """Position should not exceed 100%."""
        tracker._shutters["test1"].position = 90.0

        with patch("shutter_control.position_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            tracker.start_moving("test1", MotionState.OPENING)

            # Simulate way more time than needed
            mock_time.monotonic.return_value = 1050.0
            state = tracker.get_state("test1")
            assert state.position == 100.0

    def test_position_clamped_at_0(self, tracker):
        """Position should not go below 0%."""
        tracker._shutters["test1"].position = 10.0

        with patch("shutter_control.position_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            tracker.start_moving("test1", MotionState.CLOSING)

            mock_time.monotonic.return_value = 1050.0
            state = tracker.get_state("test1")
            assert state.position == 0.0


class TestTargetPosition:
    def test_check_targets_opening(self, tracker):
        """Shutter should signal stop when reaching target during opening."""
        tracker._shutters["test1"].position = 0.0

        with patch("shutter_control.position_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            tracker.start_moving("test1", MotionState.OPENING, target_position=50.0)

            # Not yet at target (4.5s = 25%)
            mock_time.monotonic.return_value = 1004.5
            stop_ids = tracker.check_targets()
            assert stop_ids == []

            # At target (9s = 50%)
            mock_time.monotonic.return_value = 1009.0
            stop_ids = tracker.check_targets()
            assert "test1" in stop_ids

    def test_check_targets_closing(self, tracker):
        """Shutter should signal stop when reaching target during closing."""
        tracker._shutters["test1"].position = 100.0

        with patch("shutter_control.position_tracker.time") as mock_time:
            mock_time.monotonic.return_value = 1000.0
            tracker.start_moving("test1", MotionState.CLOSING, target_position=50.0)

            # At target (10s = moved 50%)
            mock_time.monotonic.return_value = 1010.0
            stop_ids = tracker.check_targets()
            assert "test1" in stop_ids


class TestHaState:
    def test_ha_state_closed(self, tracker):
        state = tracker.get_state("test1")
        assert state.ha_state == "closed"
        assert state.ha_position == 0

    def test_ha_state_open(self, tracker):
        tracker._shutters["test1"].position = 100.0
        state = tracker.get_state("test1")
        assert state.ha_state == "open"
        assert state.ha_position == 100

    def test_ha_state_partially_open(self, tracker):
        tracker._shutters["test1"].position = 50.0
        state = tracker.get_state("test1")
        assert state.ha_state == "open"
        assert state.ha_position == 50

    def test_ha_state_opening(self, tracker):
        tracker.start_moving("test1", MotionState.OPENING)
        state = tracker.get_state("test1")
        assert state.ha_state == "opening"

    def test_ha_state_closing(self, tracker):
        tracker._shutters["test1"].position = 100.0
        tracker.start_moving("test1", MotionState.CLOSING)
        state = tracker.get_state("test1")
        assert state.ha_state == "closing"


class TestPersistence:
    def test_save_and_load(self, tmp_path):
        path = tmp_path / "positions.json"

        # Save
        t1 = PositionTracker(persistence_path=path)
        t1.register_shutter(
            ShutterTrackerConfig("s1", full_close_time=20, full_open_time=18)
        )
        t1._shutters["s1"].position = 42.5
        t1.save_positions()

        # Load into new tracker
        t2 = PositionTracker(persistence_path=path)
        t2.register_shutter(
            ShutterTrackerConfig("s1", full_close_time=20, full_open_time=18)
        )
        t2.load_positions()
        assert t2.get_state("s1").position == 42.5

    def test_load_missing_file(self, tmp_path):
        t = PositionTracker(persistence_path=tmp_path / "missing.json")
        t.register_shutter(
            ShutterTrackerConfig("s1", full_close_time=20, full_open_time=18)
        )
        t.load_positions()  # should not raise
        assert t.get_state("s1").position == 0.0

    def test_stop_saves_positions(self, tracker, tmp_path):
        tracker._shutters["test1"].position = 75.0
        tracker.start_moving("test1", MotionState.OPENING)
        tracker.stop("test1")
        # Check file was written
        assert tracker._persistence_path.exists()


class TestUpdateCallback:
    def test_callback_on_start(self, tracker):
        updates = []
        tracker.set_update_callback(lambda sid, state: updates.append((sid, state.motion)))
        tracker.start_moving("test1", MotionState.OPENING)
        assert len(updates) == 1
        assert updates[0] == ("test1", MotionState.OPENING)

    def test_callback_on_stop(self, tracker):
        updates = []
        tracker.set_update_callback(lambda sid, state: updates.append((sid, state.motion)))
        tracker.start_moving("test1", MotionState.OPENING)
        tracker.stop("test1")
        assert len(updates) == 2
        assert updates[1] == ("test1", MotionState.STOPPED)
