# Tildagon → Home Assistant MQTT Bridge

A MicroPython app for the EMF [Tildagon badge](https://tildagon.badge.emfcamp.org)
that exposes its hardware to Home Assistant over MQTT. Open the app once, press
CANCEL to minimise, and the bridge runs in the background. HA discovers it
automatically, no manual YAML on the HA side.

Tested on Tildagon 2024 / 2026 hardware running TildagonOS.

## What you get in HA

A single device, "Tildagon Badge", with:

- 12 individually addressable RGB LEDs as `light` entities (JSON schema, rgb,
  with a `pulse` effect)
- An "All LEDs" bulk control
- An "Active" master switch, flip it off to blank the screen and de-power
  the LEDs (useful for room-presence automations)
- A "Screen Layout" text entity that accepts a small JSON layout
- Battery level (`sensor`, %)
- OS version (diagnostic `sensor`)

Everything publishes via MQTT *device-based* discovery (one retained config
message). HA picks it up the first time the badge connects.

## What this is not

- Not a host-side tool. The code runs on the badge.
- Not encrypted. MQTT is plain TCP, no TLS. Trusted LAN only.
- Not battery-friendly as an always-on node. The badge sleeps; LWT marks it
  offline when it does. The bridge isn't trying to be a 24/7 sensor.

## Setup

### 1. Broker

If you don't already have one, install the Mosquitto add-on in HA. From the
badge you'll connect to the broker on the **HA host IP** on port 1883, the
`core-mosquitto` hostname is HA-network-internal and won't resolve from
outside.

Create a dedicated, non-admin HA user account for the badge. Don't reuse the
supervisor's `addons` credential; it rotates.

### 2. Config

Copy the example and fill it in.

```bash
cp config_example.py config.py
$EDITOR config.py
```

`config.py` is gitignored. Don't commit it.

Prefer a numeric IP for `MQTT_HOST`, MicroPython's mDNS resolver is flaky
and `homeassistant.local` will sometimes give `OSError -202`.

### 3. Flash

```bash
pip install mpremote
mpremote connect list                                           # find the port
mpremote connect auto fs mkdir :/apps/ha_bridge
mpremote connect auto fs cp app.py :/apps/ha_bridge/            # copy app.py
mpremote connect auto fs cp config.py :/apps/ha_bridge/         # copy config.py
mpremote connect auto fs cp tildagon.toml :/apps/ha_bridge/     # copy tildagon.toml files
mpremote connect auto fs ls :/apps/ha_bridge                    # sanity-check
```

The folder under `/apps/` must use **underscores** (`ha_bridge`, not
`ha-bridge`). Reboot the badge so it rescans `/apps/`; the app appears under
the Apps menu.

After editing `app.py` or `config.py` you need to **re-copy and reboot**,
the running app and the retained MQTT discovery don't update otherwise.

### 4. Run

Open "HA Bridge" from the Apps menu. The status line cycles through
`Starting…` → `WiFi OK` → `Connected`. Press CANCEL to minimise.

In HA: Settings → Devices & Services → Devices → "Tildagon Badge". You
should see all 16 entities.

## Driving the screen

The Screen Layout text entity takes a JSON object. All keys are optional:

```json
{
  "bg": [0, 0, 0],
  "rects": [
    {"x": -120, "y": -30, "w": 240, "h": 60, "c": [200, 0, 0]}
  ],
  "lines": [
    {"t": "ALERT", "s": 36, "y": 5, "c": [255, 255, 255]}
  ]
}
```

- Screen coordinates run **-120 to +120** with origin at centre. Y is
  positive downward.
- Colours are **0–255 RGB ints**, matching the LED entities.
- `lines` are drawn from the text baseline, not the visual centre, so a
  size-48 glyph at `y=0` sits roughly above centre. Tune `y` by trial.
- Drawing order is `bg` → `rects` → `lines`. Use `rects` as backdrops behind
  text.

Sending an empty string clears the layout and falls back to the default
status screen.

HA's `text` entity caps payloads at 255 chars. For longer layouts, publish
directly to `tildagon/<id>/layout/set` via the `mqtt.publish` service, the
badge subscribes regardless of source.

## LED effects

Standard HA JSON-light payload with an `effect` field:

```json
{"state":"ON","color_mode":"rgb","color":{"r":255,"g":0,"b":0},"effect":"pulse"}
```

Only `pulse` is implemented. Cosine envelope, 2 s period, 5% minimum
brightness so the colour stays recognisable at the dip. Tweak
`PULSE_PERIOD_MS` and `PULSE_MIN` at the top of `app.py` if you want it
faster, slower, or fully fading.

LEDs are **1-indexed** (1–12). Convention: LED 1 is at the top of the badge,
LEDs go clockwise.

## Active switch

The Active switch (`switch.tildagon_badge_active`) is the single off-button.
When it's off:

- The screen is blanked
- The LEDs are written black and `set_led_power(False)` cuts their power
- Incoming commands are still accepted and stored, when you flip it back on
  the latest layout / LED state appears immediately

The obvious use is to wire it to a room-presence sensor. Example:

```yaml
- alias: Badge - active when room occupied
  trigger:
    - platform: state
      entity_id: binary_sensor.office_motion
      to: "on"
  action:
    - service: switch.turn_on
      target:
        entity_id: switch.tildagon_badge_active

- alias: Badge - sleep when room empty
  trigger:
    - platform: state
      entity_id: binary_sensor.office_motion
      to: "off"
      for: "00:05:00"
  action:
    - service: switch.turn_off
      target:
        entity_id: switch.tildagon_badge_active
```

## Example: power dial automation

`examples/power_dial.yaml` is the automation I run at home, it turns the badge
into an at-a-glance energy display:

- Screen text: current wattage, today's kWh, current rate in pence,
  today's cost
- LEDs 1–6 (right semicircle): wattage as a coloured bar, 500 W per LED,
  green / amber / red
- LEDs 7–12 (left semicircle): current rate as a coloured bar, 5 p per LED,
  green / white / red
- Plunge-pricing flourish: when the Octopus Agile rate goes negative, LED 7
  pulses blue and the price bar is otherwise empty
- High-usage flourish: above 3 kW, LED 1 pulses red

Adapt the sensor IDs to yours. The accumulating cost sensor is built from a
four-helper chain (W → kWh → £/h → cumulative £ → daily utility meter); see
the comments inside the YAML.

## Topics

| Direction | Topic | Payload |
|---|---|---|
| in | `tildagon/<id>/led/<n>/set` | `{"state":...}` JSON light |
| in | `tildagon/<id>/led/all/set` | same, applies to all 12 |
| in | `tildagon/<id>/layout/set` | layout JSON or empty string |
| in | `tildagon/<id>/active/set` | `ON` / `OFF` |
| out | `tildagon/<id>/led/<n>` | echoed state, retained |
| out | `tildagon/<id>/led/all` | aggregate state, retained |
| out | `tildagon/<id>/layout` | echoed layout, retained |
| out | `tildagon/<id>/active` | echoed active state, retained |
| out | `tildagon/<id>/battery` | integer percent, retained |
| out | `tildagon/<id>/version` | OS version string, retained |
| out | `tildagon/<id>/status` | `online` / `offline` (LWT), retained |
| out | `homeassistant/device/tildagon_<id>/config` | discovery payload, retained |

`<id>` is the hexlified ESP32 efuse unique ID, stable across reboots,
unique per badge. The badge prints its base topic on the default status
screen.

## Security

- MQTT is plain TCP. No TLS. Run on a trusted LAN; don't expose the broker
  to the internet without putting it behind something that does.
- Broker credentials live in plain text in `config.py` on the badge
  filesystem.
- The badge's hardware unique ID is published in retained discovery and
  topic names. It's derived from the ESP32 efuse and is already broadcast
  in BLE / Wi-Fi MACs, so this is not a new leak but it is identifying.
- Wi-Fi credentials are stored by TildagonOS itself, not by this app. This
  bridge has no access to them.
- Don't commit `config.py`. The repo's `.gitignore` excludes it.

## Troubleshooting

- **`config.py: MQTT_HOST not set`** on screen: you missed step 2, or the
  copy didn't include `config.py`. `mpremote ... fs ls :/apps/ha_bridge`
  to check.
- **CONNACK 0** = OK. **CONNACK 5** = "not authorised" bad creds or the
  HA login isn't enabled for MQTT. No CONNACK at all means
  network / port / IP.
- **Discovery rejected with `extra keys not allowed @ data['~']`**:
  you're on an older `app.py`. Device-based discovery doesn't accept the
  `~` topic-prefix abbreviation. Pull latest.
- **Layout `text` entity won't take your JSON**: 255-char cap. Use
  `mqtt.publish` directly.
- **Stale entities after refactoring an entity**: HA will reconcile when
  the new retained discovery arrives. If it doesn't, clear the old config:

  ```bash
  mosquitto_pub -h <HA_IP> -u <user> -P '<pass>' -r \
    -t 'homeassistant/device/tildagon_<id>/config' -m ''
  ```

  Then reload the MQTT integration and reopen the app on the badge to
  republish.
- **Bridge says `Disconnected` and never reconnects**: the umqtt.simple
  client doesn't always notice a half-closed TCP. The reconnect loop
  catches most cases on the 15 s tick. If it gets stuck, minimise + reopen
  the app to force a clean state.

## Reference

- Tildagon app development:
  https://tildagon.badge.emfcamp.org/tildagon-apps/development/
- Hardware reference:
  https://tildagon.badge.emfcamp.org/tildagon-apps/reference/badge-hardware/
- ctx canvas:
  https://tildagon.badge.emfcamp.org/tildagon-apps/reference/ctx/
- HA MQTT discovery:
  https://www.home-assistant.io/integrations/mqtt/
- HA MQTT light (JSON schema):
  https://www.home-assistant.io/integrations/light.mqtt/#json-schema

## Licence

LGPL-3.0-only. See `tildagon.toml`.
