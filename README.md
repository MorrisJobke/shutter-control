# Shutter Control

EnOcean-to-MQTT bridge for **Eltako FSB61NP-230V** roller shutter actuators, designed to run as a Docker container on Home Assistant OS.

## Features

- Control Eltako FSB61NP shutters from Home Assistant via MQTT
- Home Assistant auto-discovery (no manual HA configuration needed)
- Time-based position tracking with persistence across restarts
- Queued sending with inter-packet delay — reliably controls many shutters simultaneously without overloading the USB gateway
- Unique sender addressing — each actuator gets its own sender ID so commands don't cross-trigger
- Teach-in support via MQTT
- Device discovery — logs teach-in requests from unknown actuators so you can find their IDs without reading the label

## Requirements

- **EnOcean USB 300** gateway (or compatible transceiver)
- **Eltako FSB61NP-230V** actuators
- **MQTT broker** (e.g. Mosquitto, typically included with Home Assistant)
- **Docker** (for deployment on Home Assistant OS)
- Python 3.11+ (for local development)

## Quick Start

1. Copy and edit the configuration:

   ```bash
   cp config.example.yaml config.yaml
   ```

2. Add your shutter IDs. The decimal number on the device label needs to be converted to hex:

   ```bash
   printf "%02X:%02X:%02X:%02X\n" $(( NUM >> 24 & 0xFF )) $(( NUM >> 16 & 0xFF )) $(( NUM >> 8 & 0xFF )) $(( NUM & 0xFF ))
   ```

3. Deploy to Home Assistant OS:

   ```bash
   ./deploy.sh <haos-ip>
   ```

4. Teach-in each actuator (see [Teach-in](#teach-in) below).

## Configuration

See [`config.example.yaml`](config.example.yaml) for a fully commented example.

```yaml
enocean:
  port: /dev/ttyUSB0

mqtt:
  host: 192.168.1.100
  port: 1883
  username: ""
  password: ""
  base_topic: enocean

shutters:
  - id: "05:12:34:56"
    name: "Living Room"
    full_close_time: 25       # seconds, fully open -> fully closed
    full_open_time: 23        # seconds, fully closed -> fully open
```

### Shutters

| Field | Description |
|---|---|
| `id` | EnOcean device ID (hex, colon-separated) |
| `name` | Display name in Home Assistant |
| `full_close_time` | Seconds from fully open to fully closed |
| `full_open_time` | Seconds from fully closed to fully open |
| `sender_offset` | Optional override for the sender address offset (0-127) |
| `invert_direction` | Set to `true` if the motor is wired in reverse (swaps open/close) |

## Teach-in

Each FSB61NP must be paired with the USB 300 gateway before it will respond to commands. Each actuator is taught with a unique sender address derived from its device ID, so commands only reach the intended actuator.

1. On the FSB61NP, turn the upper rotary switch to **LRN**. The LED starts blinking (~1 minute window).
2. Send a teach-in telegram via MQTT:
   - **Topic:** `enocean/cover/<safe_id>/teach_in`
   - **Payload:** `1`
   - `safe_id` is the device ID without colons, e.g. `05123456`
   - You can use HA Developer Tools > Services > MQTT: Publish.
3. The actuator confirms by turning the LED off.
4. Turn the upper rotary switch back to the desired mode (e.g. **GS1**).

Repeat for each shutter.

## Sender Offset and Collision Detection

The Eltako FSB61NP only checks the **sender ID** of incoming commands, not the destination address. If all actuators were taught with the same sender, every command would trigger every actuator.

To prevent this, each shutter automatically gets a unique sender address: `base_id + (last_byte_of_device_id % 128)`. This is stable — adding or reordering shutters does not change existing offsets and does not require re-teaching.

On startup, each shutter logs its offset:

```
Shutter Living Room (05:12:34:56) using sender offset 86
Shutter Bedroom (05:12:34:57) using sender offset 87
```

If two actuators happen to produce the same offset, the application **refuses to start** with an error:

```
ValueError: Sender offset collision: Bedroom (05:12:34:57) and Living Room (05:12:34:56)
both use offset 86. Set sender_offset on one of them to resolve this.
```

To resolve, set `sender_offset` on one of the colliding shutters in the config and re-teach only that actuator:

```yaml
shutters:
  - id: "05:12:34:56"
    name: "Living Room"
    full_close_time: 25
    full_open_time: 23

  - id: "05:12:34:57"
    name: "Bedroom"
    full_close_time: 25
    full_open_time: 23
    sender_offset: 42          # manual override to resolve collision
```

## Inverted Motor Direction

Some actuators may have their motor wired in reverse, causing "open" commands to close the shutter and vice versa. Set `invert_direction: true` on the affected shutter to swap the direction for commands and status tracking:

```yaml
shutters:
  - id: "04:2C:86:88"
    name: "HWR"
    full_close_time: 18
    full_open_time: 19
    invert_direction: true
```

## Discovering Device IDs

If the device label is not accessible, you can discover actuator IDs over the air:

1. Start the bridge.
2. Turn the actuator's rotary switch to **LRN**. The log will show:
   ```
   TEACH-IN request from 04:2C:XX:XX — add this ID to config.yaml to control it
   ```
3. Copy the ID into `config.yaml` and turn the switch back.

Alternatively, operating a shutter via a wall button will also reveal its ID in the logs:

```
Ignoring status from unknown device 04:2C:XX:XX
```

Actuator IDs can also be read from the decimal number printed on the device label and converted to hex.

## Position Tracking

Since the FSB61NP does not report absolute position, the bridge estimates position from motor travel time. Positions are persisted to disk and restored on restart.

- `0` = fully closed, `100` = fully open (matches Home Assistant convention)
- The tracker accounts for different open/close travel times
- Actuator confirmation telegrams and HA commands both update the position

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## MQTT Topics

For each shutter (where `<id>` is the device ID without colons):

| Topic | Direction | Payload |
|---|---|---|
| `enocean/cover/<id>/set` | Command | `OPEN` / `CLOSE` / `STOP` |
| `enocean/cover/<id>/set_position` | Command | `0`-`100` |
| `enocean/cover/<id>/teach_in` | Command | `1` |
| `enocean/cover/<id>/state` | Status | `open` / `closed` / `opening` / `closing` |
| `enocean/cover/<id>/position` | Status | `0`-`100` |
| `enocean/cover/<id>/available` | Status | `online` / `offline` |
