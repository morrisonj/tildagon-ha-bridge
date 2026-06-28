"""Tildagon HA Bridge config.

Copy this file to `config.py` (next to `app.py`) and fill in your values.
`config.py` is gitignored. Don't commit it.
"""

# MQTT broker. Prefer a numeric IP — MicroPython's mDNS is flaky and
# `homeassistant.local` will sometimes give OSError -202 on the badge.
MQTT_HOST = "192.168.1.10"
MQTT_PORT = 1883

# Use a dedicated non-admin Home Assistant user account here.
# Don't reuse the supervisor's `addons` credential — it rotates.
MQTT_USER = "tildagon"
MQTT_PASS = "change-me"
