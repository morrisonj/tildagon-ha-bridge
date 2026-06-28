"""Tildagon -> Home Assistant MQTT bridge.

Exposes, via Home Assistant MQTT *device-based* discovery (one retained
config message), the following entities under a single "Tildagon Badge" device:

  - 12 individually addressable RGB LEDs   (HA `light`, JSON schema, rgb,
                                            with a "pulse" effect)
  - "All LEDs" bulk control                (HA `light`, JSON schema, rgb)
  - Battery level                          (HA `sensor`, %)
  - TildagonOS version                     (HA diagnostic `sensor`)
  - Active master switch                   (HA `switch` -> blanks the screen
                                            and de-powers the LEDs)
  - Screen layout                          (HA `text`  -> JSON layout, see
                                            `_draw_layout` for the schema)

Open the app once so it connects; pressing CANCEL minimises it (it does NOT
exit) so the bridge keeps running in the background and HA stays live.

Broker credentials live in `config.py` next to this file. Copy
`config_example.py` to `config.py` and fill it in. `config.py` is gitignored;
do not commit it. The badge must already be on your home Wi-Fi.
"""

import app
import json
import math
import time
import wifi

from umqtt.simple import MQTTClient
from app_components import clear_background
from events.input import Buttons, BUTTON_TYPES
from tildagonos import tildagonos
from system.eventbus import eventbus
from system.patterndisplay.events import PatternDisable

# ---- A stable per-badge id (from the ESP32 efuse) ---------------------------
try:
    import machine
    import binascii
    _UID = binascii.hexlify(machine.unique_id()).decode()
except Exception:
    _UID = "0001"

# ---- Broker config (lives in config.py, see config_example.py) -------------
# config.py is gitignored. If it's missing or incomplete the app loads but
# surfaces a clear error on screen rather than connecting with junk creds.
try:
    try:
        from . import config as _cfg     # works when loaded as apps.ha_bridge.app
    except (ImportError, ValueError):
        import config as _cfg            # fallback if sys.path includes us
    MQTT_HOST = getattr(_cfg, "MQTT_HOST", None)
    MQTT_PORT = getattr(_cfg, "MQTT_PORT", 1883)
    MQTT_USER = getattr(_cfg, "MQTT_USER", None)
    MQTT_PASS = getattr(_cfg, "MQTT_PASS", None)
    _CONFIG_ERR = None if MQTT_HOST else "No MQTT_HOST in config.py"
except Exception:
    MQTT_HOST = None
    MQTT_PORT = 1883
    MQTT_USER = None
    MQTT_PASS = None
    _CONFIG_ERR = "Missing config.py"
# -----------------------------------------------------------------------------

NODE = "tildagon_" + _UID
BASE = "tildagon/" + _UID
DISCOVERY_TOPIC = "homeassistant/device/" + NODE + "/config"
STATUS_TOPIC = BASE + "/status"
BATTERY_TOPIC = BASE + "/battery"
VERSION_TOPIC = BASE + "/version"

TELEMETRY_INTERVAL_MS = 10000   # how often to publish battery
RECONNECT_INTERVAL_MS = 15000   # how often to retry while disconnected


def _led_state_topic(n):
    return BASE + "/led/" + str(n)

LED_ALL_STATE = BASE + "/led/all"
LED_ALL_SET = BASE + "/led/all/set"
LAYOUT_STATE = BASE + "/layout"
LAYOUT_SET = BASE + "/layout/set"
ACTIVE_STATE = BASE + "/active"
ACTIVE_SET = BASE + "/active/set"

LED_EFFECTS = ["pulse"]
PULSE_PERIOD_MS = 2000
PULSE_MIN = 0.05  # never fully dark, so the colour is still recognisable

def _build_discovery(version):
    """The single device-based discovery payload describing every entity."""
    cmps = {
        "led_all": {
            "p": "light",
            "name": "All LEDs",
            "schema": "json",
            "supported_color_modes": ["rgb"],
            "effect": True,
            "effect_list": LED_EFFECTS,
            "command_topic": LED_ALL_SET,
            "state_topic": LED_ALL_STATE,
            "unique_id": NODE + "_led_all",
        },
        "active": {
            "p": "switch",
            "name": "Active",
            "icon": "mdi:power",
            "command_topic": ACTIVE_SET,
            "state_topic": ACTIVE_STATE,
            "payload_on": "ON",
            "payload_off": "OFF",
            "unique_id": NODE + "_active",
        },
        "battery": {
            "p": "sensor",
            "name": "Battery",
            "device_class": "battery",
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "state_topic": BASE + "/battery",
            "unique_id": NODE + "_battery",
        },
        "version": {
            "p": "sensor",
            "name": "OS Version",
            "entity_category": "diagnostic",
            "state_topic": BASE + "/version",
            "unique_id": NODE + "_version",
        },
        "layout": {
            "p": "text",
            "name": "Screen Layout",
            "command_topic": LAYOUT_SET,
            "state_topic": LAYOUT_STATE,
            "max": 255,
            "unique_id": NODE + "_layout",
        },
    }
    for n in range(1, 13):
        cmps["led_%d" % n] = {
            "p": "light",
            "name": "LED %d" % n,
            "schema": "json",
            "supported_color_modes": ["rgb"],
            "effect": True,
            "effect_list": LED_EFFECTS,
            "command_topic": "%s/led/%d/set" % (BASE, n),
            "state_topic": "%s/led/%d" % (BASE, n),
            "unique_id": "%s_led_%d" % (NODE, n),
        }
    return {
        "dev": {
            "ids": NODE,
            "name": "Tildagon Badge",
            "mf": "EMF",
            "mdl": "Tildagon",
            "sw": str(version),
        },
        "o": {"name": "tildagon-ha-bridge"},
        "avty_t": BASE + "/status",
        "qos": 0,
        "cmps": cmps,
    }

class HABridgeApp(app.App):
    def __init__(self):
        self.button_states = Buttons(self)

        # Stop the firmware's default LED pattern so we own the LEDs.
        eventbus.emit(PatternDisable())
        try:
            tildagonos.set_led_power(True)
        except Exception:
            pass
        self._led_power_on = True

        self.client = None
        self.status = "Starting..."
        # When False the screen is blanked and the LEDs are de-powered. Target
        # state (self.leds / self.layout_data) is preserved so flipping back to
        # True snaps the latest commanded state straight onto the badge.
        self.active = True
        # Raw layout JSON (echoed back to HA) and its parsed form. When
        # layout_data is non-None it takes over the whole screen.
        self.layout = ""
        self.layout_data = None
        # Local colour cache. Index 0 unused; the LEDs are numbered 1-12.
        self.leds = [(0, 0, 0)] * 13
        # Per-LED effect (None or "pulse"); index 0 unused.
        self.led_effects = [None] * 13
        # Last colour commanded via the "All LEDs" entity (defaults to white
        # so toggling ON without a colour gives a sensible result).
        self._all_color = (255, 255, 255)
        self._all_effect = None

        self._version = "unknown"
        try:
            import ota
            self._version = ota.get_version()
        except Exception:
            pass

        self._last_telemetry = time.ticks_ms()
        # Make the first connection attempt fire immediately.
        self._last_attempt = time.ticks_add(time.ticks_ms(),
                                             -RECONNECT_INTERVAL_MS)

    # ---- connection ---------------------------------------------------------
    def _ensure_wifi(self):
        # NOTE: wifi.wait() blocks until connected or timeout. We only reach
        # here on a rate-limited schedule, so the UI hitches at most once per
        # RECONNECT_INTERVAL_MS while the network is down.
        try:
            if wifi.status():
                return True
            wifi.connect()
            return wifi.wait()
        except Exception as e:
            self.status = "WiFi err: %s" % e
            return False

    def _connect(self):
        if _CONFIG_ERR:
            self.status = _CONFIG_ERR
            return False
        if not self._ensure_wifi():
            self.status = "No WiFi"
            return False
        try:
            self.status = "WiFi OK"
            c = MQTTClient(NODE, MQTT_HOST, port=MQTT_PORT,
                           user=MQTT_USER, password=MQTT_PASS, keepalive=60)
            # If the badge drops off, the broker publishes this for us so HA
            # shows the device as unavailable.
            c.set_last_will(STATUS_TOPIC, b"offline", retain=True, qos=0)
            c.set_callback(self._on_message)
            c.connect()
            c.subscribe(BASE + "/led/+/set")
            c.subscribe(LAYOUT_SET)
            c.subscribe(ACTIVE_SET)
            self.client = c
            self._announce()
            self.status = "Connected"
            return True
        except Exception as e:
            self.status = "MQTT err: %s" % e
            self.client = None
            return False

    def _announce(self):
        # Retained discovery + states so HA repopulates after any restart.
        self.client.publish(
            DISCOVERY_TOPIC,
            json.dumps(_build_discovery(self._version)).encode(),
            retain=True, qos=0)
        self.client.publish(STATUS_TOPIC, b"online", retain=True, qos=0)
        self.client.publish(VERSION_TOPIC, str(self._version).encode(),
                            retain=True, qos=0)
        self.client.publish(LAYOUT_STATE, self.layout.encode(),
                            retain=True, qos=0)
        self._publish_active()
        for n in range(1, 13):
            self._publish_led(n)
        self._publish_led_all()
        self._publish_battery()

    # ---- publishing ---------------------------------------------------------
    def _publish_battery(self):
        try:
            import power
            level = int(power.BatteryLevel())
            self.client.publish(BATTERY_TOPIC, str(level).encode(),
                                retain=True, qos=0)
        except Exception:
            pass

    def _led_payload(self, n):
        r, g, b = self.leds[n]
        if r or g or b:
            payload = {"state": "ON", "color_mode": "rgb",
                       "color": {"r": r, "g": g, "b": b}}
        else:
            payload = {"state": "OFF"}
        if self.led_effects[n]:
            payload["effect"] = self.led_effects[n]
        return json.dumps(payload)

    def _publish_led(self, n):
        if self.client:
            self.client.publish(_led_state_topic(n),
                                self._led_payload(n).encode(),
                                retain=True, qos=0)

    def _publish_led_all(self):
        if not self.client:
            return
        any_on = any(self.leds[i] != (0, 0, 0) for i in range(1, 13))
        if any_on:
            r, g, b = self._all_color
            payload = {"state": "ON", "color_mode": "rgb",
                       "color": {"r": r, "g": g, "b": b}}
        else:
            payload = {"state": "OFF"}
        if self._all_effect:
            payload["effect"] = self._all_effect
        self.client.publish(LED_ALL_STATE, json.dumps(payload).encode(),
                            retain=True, qos=0)

    def _publish_active(self):
        if self.client:
            self.client.publish(ACTIVE_STATE,
                                b"ON" if self.active else b"OFF",
                                retain=True, qos=0)

    # ---- incoming commands --------------------------------------------------
    def _on_message(self, topic, msg):
        topic = topic.decode() if isinstance(topic, bytes) else topic
        try:
            if topic == LAYOUT_SET:
                self._apply_layout(msg)
                return
            if topic == ACTIVE_SET:
                self._apply_active(msg)
                return
            # LED command topic looks like: tildagon/<id>/led/<n>/set
            # or tildagon/<id>/led/all/set for the bulk control.
            parts = topic.split("/")
            if len(parts) == 5 and parts[2] == "led" and parts[4] == "set":
                if parts[3] == "all":
                    self._apply_led_all(msg)
                else:
                    n = int(parts[3])
                    if 1 <= n <= 12:
                        self._apply_led(n, msg)
        except Exception as e:
            self.status = "Cmd err: %s" % e

    def _apply_led(self, n, msg):
        data = json.loads(msg)
        if data.get("state") == "OFF":
            self.leds[n] = (0, 0, 0)
            self.led_effects[n] = None
        else:
            col = data.get("color")
            if col:
                self.leds[n] = (int(col.get("r", 0)),
                                int(col.get("g", 0)),
                                int(col.get("b", 0)))
            elif self.leds[n] == (0, 0, 0):
                # Turned ON with no colour given -> default to white.
                self.leds[n] = (255, 255, 255)
            if "effect" in data:
                eff = data.get("effect")
                self.led_effects[n] = eff if eff in LED_EFFECTS else None
        self._publish_led(n)
        # Aggregate state may have flipped (e.g. last ON LED turned off).
        self._publish_led_all()

    def _apply_active(self, msg):
        state = msg.decode().strip().upper()
        self.active = (state == "ON")
        self._publish_active()

    def _apply_layout(self, msg):
        self.layout = msg.decode()
        if self.layout.strip():
            try:
                self.layout_data = json.loads(self.layout)
            except Exception as e:
                self.layout_data = None
                self.status = "Layout err: %s" % e
        else:
            self.layout_data = None
        self.client.publish(LAYOUT_STATE, self.layout.encode(),
                            retain=True, qos=0)

    def _apply_led_all(self, msg):
        data = json.loads(msg)
        if data.get("state") == "OFF":
            color = (0, 0, 0)
            self._all_effect = None
        else:
            col = data.get("color")
            if col:
                color = (int(col.get("r", 0)),
                         int(col.get("g", 0)),
                         int(col.get("b", 0)))
                self._all_color = color
            else:
                # Turned ON with no colour given -> reuse last all-colour.
                color = self._all_color
            if "effect" in data:
                eff = data.get("effect")
                self._all_effect = eff if eff in LED_EFFECTS else None
        effect = self._all_effect
        for n in range(1, 13):
            self.leds[n] = color
            self.led_effects[n] = effect
        for n in range(1, 13):
            self._publish_led(n)
        self._publish_led_all()

    # ---- lifecycle ----------------------------------------------------------
    def update(self, delta):
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            # Minimise rather than exit so the bridge keeps running.
            self.minimise()

    def background_update(self, delta):
        now = time.ticks_ms()
        # Always drive the LED hardware so pulse animations & the active
        # switch take effect even when the app is minimised.
        self._render_leds()
        if self.client is None:
            if time.ticks_diff(now, self._last_attempt) >= RECONNECT_INTERVAL_MS:
                self._last_attempt = now
                self._connect()
            return
        try:
            self.client.check_msg()  # non-blocking; dispatches LED/layout cmds
            if time.ticks_diff(now, self._last_telemetry) >= TELEMETRY_INTERVAL_MS:
                self._last_telemetry = now
                self._publish_battery()
        except Exception:
            self.status = "Disconnected"
            self.client = None

    def _render_leds(self):
        if not self.active:
            if self._led_power_on:
                for n in range(1, 13):
                    tildagonos.leds[n] = (0, 0, 0)
                tildagonos.leds.write()
                try:
                    tildagonos.set_led_power(False)
                except Exception:
                    pass
                self._led_power_on = False
            return
        if not self._led_power_on:
            try:
                tildagonos.set_led_power(True)
            except Exception:
                pass
            self._led_power_on = True
        # Compute the shared pulse envelope once per frame.
        factor = 1.0
        if any(e == "pulse" for e in self.led_effects[1:]):
            phase = (time.ticks_ms() % PULSE_PERIOD_MS) / PULSE_PERIOD_MS
            wave = (1 - math.cos(phase * 2 * math.pi)) / 2  # 0 -> 1 -> 0
            factor = PULSE_MIN + (1 - PULSE_MIN) * wave
        for n in range(1, 13):
            r, g, b = self.leds[n]
            if self.led_effects[n] == "pulse":
                tildagonos.leds[n] = (int(r * factor),
                                      int(g * factor),
                                      int(b * factor))
            else:
                tildagonos.leds[n] = (r, g, b)
        tildagonos.leds.write()

    # ---- display ------------------------------------------------------------
    def draw(self, ctx):
        if not self.active:
            clear_background(ctx)
            return
        if self.layout_data is not None:
            self._draw_layout(ctx)
            return

        clear_background(ctx)
        ctx.save()
        ctx.text_align = ctx.CENTER

        ctx.font_size = 22
        ctx.rgb(1, 1, 1).move_to(0, -70).text("Tildagon -> HA")

        ctx.font_size = 18
        ctx.rgb(0.6, 0.8, 1).move_to(0, -40).text(self.status)

        ctx.font_size = 13
        ctx.rgb(0.6, 0.6, 0.6).move_to(0, -12).text(BASE)

        ctx.font_size = 13
        ctx.rgb(0.5, 0.5, 0.5).move_to(0, 95).text("CANCEL = run in background")
        ctx.restore()

    def _draw_layout(self, ctx):
        """Render a user-supplied JSON layout. Schema (all keys optional):

          {
            "bg":    [r, g, b],            # 0-255, fills the whole screen
            "lines": [
              {
                "t": "text",               # the text to draw
                "s": 22,                   # font size
                "c": [r, g, b],            # 0-255 text colour
                "x": 0, "y": -40,          # position; origin is screen centre
                "a": "center"              # "left" | "center" | "right"
              }, ...
            ],
            "rects": [
              {"x": -120, "y": 50, "w": 240, "h": 30, "c": [255, 0, 0]}
            ]
          }
        """
        data = self.layout_data
        clear_background(ctx)
        ctx.save()

        bg = data.get("bg")
        if bg and len(bg) >= 3:
            ctx.rgb(bg[0] / 255.0, bg[1] / 255.0, bg[2] / 255.0)
            ctx.rectangle(-120, -120, 240, 240).fill()

        for r in data.get("rects", []) or []:
            col = r.get("c", [255, 255, 255])
            ctx.rgb(col[0] / 255.0, col[1] / 255.0, col[2] / 255.0)
            ctx.rectangle(r.get("x", -120), r.get("y", -120),
                          r.get("w", 240), r.get("h", 240)).fill()

        for line in data.get("lines", []) or []:
            text = line.get("t", "")
            if not text:
                continue
            ctx.font_size = line.get("s", 18)
            col = line.get("c", [255, 255, 255])
            ctx.rgb(col[0] / 255.0, col[1] / 255.0, col[2] / 255.0)
            align = line.get("a", "center")
            if align == "left":
                ctx.text_align = ctx.LEFT
            elif align == "right":
                ctx.text_align = ctx.RIGHT
            else:
                ctx.text_align = ctx.CENTER
            ctx.move_to(line.get("x", 0), line.get("y", 0)).text(str(text))
        ctx.restore()


__app_export__ = HABridgeApp
