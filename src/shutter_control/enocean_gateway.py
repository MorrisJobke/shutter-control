"""EnOcean gateway for Eltako FSB61NP-230V actuators.

Uses 4BS (RORG 0xA5) telegrams with the proprietary Eltako protocol.
The FSB61NP does NOT use the standard EEP D2-05-00 (VLD) profile.

Command telegram format (4 data bytes):
  DB3 (data[1]): time MSB
  DB2 (data[2]): time LSB
  DB1 (data[3]): direction + flags
  DB0 (data[4]): teach-in / command flags

Time is encoded as int(seconds * 10), range 0-3000.
Direction: 0x01 = up (open), 0x02 = down (close).
Stop: send time=0.

Status telegrams from the device use RPS (RORG 0xF6):
  Rocker value 0 = moving up (opening)
  Rocker value 1 = moving down (closing)
  No rocker action = stopped
"""

import logging
import threading
import time
from enum import IntEnum
from typing import Callable

from enocean.communicators import SerialCommunicator
from enocean.protocol.constants import PACKET, RORG
from enocean.protocol.packet import RadioPacket

logger = logging.getLogger(__name__)


class Direction(IntEnum):
    STOP = 0
    UP = 1    # open
    DOWN = 2  # close


class StatusEvent:
    """Parsed status event from an FSB61NP actuator."""

    def __init__(self, sender_id: str, direction: Direction | None, stopped: bool):
        self.sender_id = sender_id
        self.direction = direction
        self.stopped = stopped

    def __repr__(self) -> str:
        return f"StatusEvent({self.sender_id}, dir={self.direction}, stopped={self.stopped})"


class EnOceanGateway:
    def __init__(self, port: str):
        self._port = port
        self._communicator: SerialCommunicator | None = None
        self._base_id: list[int] | None = None
        self._on_status: Callable[[StatusEvent], None] | None = None
        self._receive_thread: threading.Thread | None = None
        self._running = False

    @property
    def base_id(self) -> list[int] | None:
        return self._base_id

    def set_status_callback(self, callback: Callable[[StatusEvent], None]) -> None:
        self._on_status = callback

    def start(self) -> None:
        logger.info("Starting EnOcean gateway on %s", self._port)
        self._communicator = SerialCommunicator(port=self._port)
        self._communicator.start()

        # Wait for the communicator to read the base ID from the dongle
        for attempt in range(10):
            time.sleep(0.5)
            if not self._communicator.is_alive():
                raise RuntimeError(
                    f"EnOcean communicator died — is another process using {self._port}? "
                    "Check if the HA EnOcean integration is active."
                )
            self._base_id = self._communicator.base_id
            if self._base_id:
                break

        if self._base_id:
            logger.info("EnOcean base ID: %s", _format_id(self._base_id))
        else:
            raise RuntimeError("Could not read EnOcean base ID after 5 seconds")

        self._running = True
        self._receive_thread = threading.Thread(
            target=self._receive_loop, daemon=True, name="enocean-rx"
        )
        self._receive_thread.start()

    def stop(self) -> None:
        logger.info("Stopping EnOcean gateway")
        self._running = False
        if self._communicator:
            self._communicator.stop()

    def send_command(
        self, destination: list[int], direction: Direction, time_sec: float = 0
    ) -> None:
        """Send a move/stop command to an FSB61NP actuator.

        Args:
            destination: 4-byte device ID, e.g. [0x05, 0x12, 0x34, 0x56]
            direction: UP, DOWN, or STOP
            time_sec: drive time in seconds (0 = stop)
        """
        if not self._communicator:
            logger.error("Cannot send: communicator not started")
            return

        if not self._base_id:
            self._base_id = self._communicator.base_id
            if not self._base_id:
                logger.error("Cannot send: base ID not available")
                return

        if direction == Direction.STOP:
            time_sec = 0

        time_val = min(int(time_sec * 10), 3000)
        time_msb = (time_val >> 8) & 0xFF
        time_lsb = time_val & 0xFF

        # DB1: direction bits
        if direction == Direction.UP:
            db1 = 0x01
        elif direction == Direction.DOWN:
            db1 = 0x02
        else:
            db1 = 0x00

        # DB0: 0x08 = normal command (teach-in bit set = no teach-in)
        db0 = 0x08

        packet = RadioPacket.create(
            rorg=RORG.BS4,
            rorg_func=0x3F,
            rorg_type=0x7F,
            destination=destination,
            sender=self._base_id,
            learn=False,
        )

        # Overwrite data bytes with Eltako FSB61 command format
        # packet.data layout: [RORG, DB3, DB2, DB1, DB0, sender..., status]
        packet.data[1] = time_msb   # DB3: time MSB
        packet.data[2] = time_lsb   # DB2: time LSB
        packet.data[3] = db1        # DB1: direction
        packet.data[4] = db0        # DB0: flags

        logger.info(
            "Sending %s to %s (time=%.1fs, raw=%02x %02x %02x %02x)",
            direction.name,
            _format_id(destination),
            time_sec,
            time_msb,
            time_lsb,
            db1,
            db0,
        )
        self._communicator.send(packet)

    def _receive_loop(self) -> None:
        """Process incoming EnOcean packets."""
        while self._running and self._communicator and self._communicator.is_alive():
            try:
                packet = self._communicator.receive.get(timeout=1.0)
            except Exception:
                continue

            if packet.packet_type != PACKET.RADIO:
                continue

            self._handle_radio_packet(packet)

    def _handle_radio_packet(self, packet: RadioPacket) -> None:
        sender_id = _format_id(packet.sender)

        if packet.rorg == RORG.RPS:
            # RPS telegram from FSB61NP: rocker switch status
            self._handle_rps_status(sender_id, packet)
        elif packet.rorg == RORG.BS4:
            # 4BS response (less common for FSB61NP status)
            logger.debug("Received 4BS from %s: %s", sender_id, packet.data.hex())
        else:
            logger.debug(
                "Received RORG 0x%02x from %s", packet.rorg, sender_id
            )

    def _handle_rps_status(self, sender_id: str, packet: RadioPacket) -> None:
        """Parse RPS (F6-02-02) status telegram from FSB61NP.

        The device sends rocker switch telegrams:
        - Rocker action with value 0 = moving up (opening)
        - Rocker action with value 1 = moving down (closing)
        - No action (release) = stopped
        """
        if len(packet.data) < 2:
            return

        status_byte = packet.data[1]  # DB0 of RPS
        # T21 and NU bits in status
        raw_status = packet.status if hasattr(packet, "status") else 0

        # NU bit (bit 4 of status): 1 = button pressed, 0 = button released
        nu_bit = (raw_status >> 4) & 0x01

        if nu_bit:
            # Button action: extract rocker value from upper nibble of data
            rocker_value = (status_byte >> 5) & 0x07
            if rocker_value == 0:
                direction = Direction.UP
            elif rocker_value == 1:
                direction = Direction.DOWN
            else:
                direction = None

            event = StatusEvent(sender_id, direction=direction, stopped=False)
        else:
            # Button released = motor stopped
            event = StatusEvent(sender_id, direction=None, stopped=True)

        logger.debug("Status from %s: %s", sender_id, event)
        if self._on_status:
            self._on_status(event)


def _format_id(id_bytes: list[int]) -> str:
    """Format a 4-byte ID as 'XX:XX:XX:XX'."""
    return ":".join(f"{b:02X}" for b in id_bytes[-4:])
