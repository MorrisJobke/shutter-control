# 🪟 Shutter Control

EnOcean-to-MQTT bridge for **Eltako FSB61NP-230V** roller shutter actuators, designed to run as a Home Assistant Add-On or as a standalone Docker container.

## Features

- Control Eltako FSB61NP shutters from Home Assistant via MQTT
- Home Assistant auto-discovery (no manual HA configuration needed)
- **Native HA Add-On** with UI configuration — no YAML editing required
- Time-based position tracking with persistence across restarts
- Partial position control (`set_position`) stops the shutter at the requested percentage
- Queued sending with inter-packet delay — reliably controls many shutters simultaneously without overloading the USB gateway
- Unique sender addressing — each actuator gets its own sender ID so commands don't cross-trigger
- Command retry with backoff — unacknowledged commands are retried up to 2 times (after 5 s and 10 s); if all retries fail the tracked position is reverted to its pre-command value
- Teach-in support via MQTT
- Device discovery — logs teach-in requests from unknown actuators so you can find their IDs without reading the label

## Requirements

- **EnOcean USB 300** gateway (or compatible transceiver)
- **Eltako FSB61NP-230V** actuators
- **Home Assistant** with the Mosquitto Broker add-on (for Add-On installation), or any MQTT broker (for manual deployment)
- Python 3.11+ (for local development only)

## Installation

### Option A: Home Assistant Add-On (recommended)

1. In Home Assistant go to **Settings → Apps → Install App → ⋮ → Repositories**.
2. Add `https://github.com/morrisjobke/shutter-control` and click **Add**.
3. Find **Shutter Control** in the store and click **Install**.
4. Open the add-on's **Configuration** tab and fill in your settings (see [Configuration](#configuration) below).
5. Start the add-on.
6. Teach-in each actuator (see [Teach-in](#teach-in) below).

The EnOcean USB stick is selected from a device picker. MQTT defaults point to the local Mosquitto add-on (`core-mosquitto:1883`).

### Option B: Manual deployment via deploy.sh

1. Copy and edit the local config:

   ```bash
   cp config.example.yaml config.local.yaml
   # edit config.local.yaml with your shutter IDs and MQTT credentials
   ```

2. Convert the decimal ID from the device label to hex if needed:

   ```bash
   printf "%02X:%02X:%02X:%02X\n" $(( NUM >> 24 & 0xFF )) $(( NUM >> 16 & 0xFF )) $(( NUM >> 8 & 0xFF )) $(( NUM & 0xFF ))
   ```

3. Deploy to Home Assistant OS:

   ```bash
   ./deploy.sh <haos-ip>
   ```

4. Teach-in each actuator (see [Teach-in](#teach-in) below).

## Configuration

### Add-On UI (Option A)

All settings are configured in the **Configuration** tab of the add-on in HA. No files to edit.

| Field | Default | Description |
|---|---|---|
| `enocean_port` | device picker | Serial port of the EnOcean USB 300 stick |
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname |
| `mqtt_port` | `1883` | MQTT broker port |
| `mqtt_username` | _(empty)_ | MQTT username |
| `mqtt_password` | _(empty)_ | MQTT password (stored encrypted) |
| `mqtt_base_topic` | `enocean` | MQTT topic prefix |
| `shutters` | _(list)_ | List of shutters (add/remove via UI) |

Each shutter entry:

| Field | Required | Description |
|---|---|---|
| `id` | yes | EnOcean device ID (hex, colon-separated, e.g. `04:2C:6E:83`) |
| `name` | yes | Display name in Home Assistant |
| `full_close_time` | yes | Seconds from fully open to fully closed |
| `full_open_time` | yes | Seconds from fully closed to fully open |
| `sender_offset` | no | Manual override for sender address offset (0–127) |
| `invert_direction` | no | Set to `true` if the motor is wired in reverse |

### YAML file (Option B — local/manual)

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

The shutter fields are the same as in the table above.

## Teach-in

Each FSB61NP must be paired with the USB 300 gateway before it will respond to commands. Each actuator is taught with a unique sender address derived from its device ID, so commands only reach the intended actuator.

1. On the FSB61NP, turn the upper rotary switch to **LRN**. The LED starts blinking (~1 minute window).
2. Send a teach-in telegram via MQTT:
   - **Topic:** `enocean/cover/<safe_id>/teach_in`
   - **Payload:** `1`
   - `safe_id` is the device ID without colons, e.g. `042c6e83`
   - You can use HA Developer Tools → Services → MQTT: Publish.
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

To resolve, set `sender_offset` on one of the colliding shutters and re-teach only that actuator:

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

Some actuators may have their motor wired in reverse, causing "open" commands to close the shutter and vice versa. Set `invert_direction: true` on the affected shutter to swap the direction:

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
   TEACH-IN request from 04:2C:XX:XX — add this ID to config to control it
   ```
3. Copy the ID into your configuration and turn the switch back.

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
- `set_position` moves the shutter to an exact percentage and sends a stop command when the calculated travel time has elapsed

## Command Reliability

When a command is sent and no acknowledgement is received from the actuator within 5 seconds, the command is automatically retried. Up to 2 retries are attempted with a linear backoff (5 s, 10 s, 15 s). If all retries fail:

- A warning is logged
- The shutter's tracked position is reverted to what it was before the command, so Home Assistant reflects the actual (unchanged) state rather than a position that was never reached

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

For local testing, copy and edit the example config:

```bash
cp config.example.yaml config.local.yaml
shutter-control config.local.yaml
```

## MQTT Topics

For each shutter (where `<id>` is the device ID without colons, e.g. `042c6e83`):

| Topic | Direction | Payload |
|---|---|---|
| `enocean/cover/<id>/set` | Command | `OPEN` / `CLOSE` / `STOP` |
| `enocean/cover/<id>/set_position` | Command | `0`–`100` |
| `enocean/cover/<id>/teach_in` | Command | `1` |
| `enocean/cover/<id>/state` | Status | `open` / `closed` / `opening` / `closing` |
| `enocean/cover/<id>/position` | Status | `0`–`100` |
| `enocean/cover/<id>/available` | Status | `online` / `offline` |


# License

MIT License

Copyright 2026 Morris Jobke

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
