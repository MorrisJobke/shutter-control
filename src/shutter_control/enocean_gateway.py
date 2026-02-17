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
from enocean.protocol.packet import Packet, RadioPacket

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

    def _sender_with_offset(self, offset: int) -> list[int]:
        """Return base_id + offset as a 4-byte sender address."""
        base = self._base_id
        raw = (base[0] << 24 | base[1] << 16 | base[2] << 8 | base[3]) + offset
        return [(raw >> 24) & 0xFF, (raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF]

    def send_teach_in(self, destination: list[int], sender_offset: int = 0) -> None:
        """Send a teach-in telegram to pair with an FSB61NP actuator.

        The actuator must be in learn mode (LED blinking) when this is sent.
        Each actuator should use a unique sender_offset (0, 1, 2, ...) so
        that commands only reach the intended actuator.
        """
        if not self._communicator or not self._base_id:
            logger.error("Cannot send teach-in: communicator not ready")
            return

        sender = self._sender_with_offset(sender_offset)
        packet = Packet(packet_type=PACKET.RADIO)
        packet.data = bytearray([
            RORG.BS4,       # RORG
            0xFF,           # DB3
            0xF8,           # DB2
            0x0D,           # DB1
            0x80,           # DB0: teach-in bit cleared
            sender[0], sender[1], sender[2], sender[3],
            0x30,           # status
        ])
        packet.optional = bytearray([
            0x03,
            destination[0], destination[1], destination[2], destination[3],
            0xFF,
            0x00,
        ])

        logger.info(
            "Sending TEACH-IN to %s (sender=%s, offset=%d)",
            _format_id(destination),
            _format_id(sender),
            sender_offset,
        )
        self._communicator.send(packet)

    def send_command(
        self,
        destination: list[int],
        direction: Direction,
        time_sec: float = 0,
        sender_offset: int = 0,
    ) -> None:
        """Send a move/stop command to an FSB61NP actuator.

        Args:
            destination: 4-byte device ID, e.g. [0x05, 0x12, 0x34, 0x56]
            direction: UP, DOWN, or STOP
            time_sec: drive time in seconds (0 = stop)
            sender_offset: offset from base_id to use as sender address
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

        # DB1: direction bits (Eltako: 0x01 = up/open, 0x02 = down/close)
        if direction == Direction.UP:
            db1 = 0x01
        elif direction == Direction.DOWN:
            db1 = 0x02
        else:
            db1 = 0x00

        # DB0: 0x08 = normal command (teach-in bit set = no teach-in)
        db0 = 0x08

        # Build raw 4BS radio packet — we can't use RadioPacket.create()
        # because the Eltako FSB61 EEP is proprietary and not in the library.
        #
        # ESP3 Radio packet structure:
        #   data:     [RORG, DB3, DB2, DB1, DB0, sender(4), status]
        #   optional: [sub_tel, dest(4), dBm, security]
        sender = self._sender_with_offset(sender_offset)
        packet = Packet(packet_type=PACKET.RADIO)
        packet.data = bytearray([
            RORG.BS4,       # RORG
            time_msb,       # DB3: time MSB
            time_lsb,       # DB2: time LSB
            db1,            # DB1: direction
            db0,            # DB0: flags
            sender[0], sender[1], sender[2], sender[3],
            0x30,           # status (T21 + NU)
        ])
        packet.optional = bytearray([
            0x03,           # sub_tel_num: 3 (send)
            destination[0], destination[1], destination[2], destination[3],
            0xFF,           # dBm (max)
            0x00,           # security level
        ])

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
            self._handle_4bs_status(sender_id, packet)
        else:
            logger.debug(
                "Received RORG 0x%02x from %s", packet.rorg, sender_id
            )

    def _handle_rps_status(self, sender_id: str, packet: RadioPacket) -> None:
        """Parse RPS status telegram from actuators and wall buttons.

        Standard F6-02-02 rocker switches (wall buttons) encode the
        rocker channel in the upper 3 bits of DB0:
          0 (AI) / 2 (BI) = up/open
          1 (AO) / 3 (BO) = down/close

        Eltako FSB61NP actuators use a proprietary encoding where the
        direction is in the low bits of DB0:
          0x02 = up/open, 0x01 = down/close
        (rocker value is always 0 in this case)
        """
        if len(packet.data) < 2:
            return

        status_byte = packet.data[1]  # DB0 of RPS
        # T21 and NU bits in status
        raw_status = packet.status if hasattr(packet, "status") else 0

        # NU bit (bit 4 of status): 1 = button pressed, 0 = button released
        nu_bit = (raw_status >> 4) & 0x01

        if nu_bit:
            rocker_value = (status_byte >> 5) & 0x07

            if rocker_value in (1, 3):
                # Standard AO or BO = down/close
                direction = Direction.DOWN
            elif rocker_value in (0, 2):
                # Standard AI or BI = up/open, BUT for Eltako actuators
                # rocker_value is always 0 and the direction is in the
                # low bits: 0x01 = up, 0x02 = down.
                if rocker_value == 0 and (status_byte & 0x0F) == 0x02:
                    direction = Direction.DOWN
                else:
                    direction = Direction.UP
            else:
                direction = None

            event = StatusEvent(sender_id, direction=direction, stopped=False)
        else:
            # Button released = motor stopped
            event = StatusEvent(sender_id, direction=None, stopped=True)

        logger.debug("Status from %s: %s", sender_id, event)
        if self._on_status:
            self._on_status(event)

    def _handle_4bs_status(self, sender_id: str, packet: RadioPacket) -> None:
        """Parse 4BS status telegram from FSB61NP.

        The actuator sends a 4BS telegram when it stops, reporting the
        elapsed run time and direction.  DB0 bit 3 (teach-in bit) is set
        for normal data telegrams and cleared for teach-in requests.
        """
        if len(packet.data) < 5:
            return

        db0 = packet.data[4]

        # Ignore teach-in telegrams (bit 3 cleared)
        if not (db0 & 0x08):
            logger.debug("4BS teach-in from %s, ignoring", sender_id)
            return

        # Actuator stopped — report as a stop event
        event = StatusEvent(sender_id, direction=None, stopped=True)
        logger.debug("4BS stop from %s: %s", sender_id, bytes(packet.data).hex())
        if self._on_status:
            self._on_status(event)


def _format_id(id_bytes: list[int]) -> str:
    """Format a 4-byte ID as 'XX:XX:XX:XX'."""
    return ":".join(f"{b:02X}" for b in id_bytes[-4:])
