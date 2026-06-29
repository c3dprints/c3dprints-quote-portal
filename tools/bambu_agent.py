#!/usr/bin/env python3
"""
C3D Prints - Bambu bridge agent.

Runs on a machine at the shop (PC / Raspberry Pi) on the same LAN as your Bambu
printers. It reads each printer's live status over local MQTT and pushes it to
the quote portal's /agent/printer-report endpoint. Your printer access codes
never leave your network; only state + progress are sent to the portal.

Setup:
  1. pip install -r requirements.txt        (paho-mqtt, requests)
  2. On each printer: Settings -> WLAN -> note the "Access Code" and the printer
     serial (Settings -> Device, or the sticker). Put the printer on your LAN.
  3. Fill in PORTAL_URL, AGENT_KEY, and the PRINTERS list below
     (AGENT_KEY must match PRINTER_AGENT_KEY set in the portal's Render env).
  4. In the portal's Production board, add each printer and set its "Bambu serial"
     to the SAME serial used below.
  5. python bambu_agent.py      (leave it running; use a service/pm2 for 24/7)

Notes:
  - Uses Bambu LAN MQTT (TLS 8883, user "bblp", password = access code). This is
    an unofficial protocol; a firmware update could change fields.
  - Only LAN mode is used here (no Bambu cloud account needed).
"""

import json
import os
import ssl
import threading
import time

import requests

try:
    import paho.mqtt.client as mqtt
except ImportError:
    raise SystemExit("Missing dependency. Run: pip install -r requirements.txt")

# ---------------------------------------------------------------------------
# CONFIG - edit these (or set the matching env vars)
# ---------------------------------------------------------------------------
PORTAL_URL = os.getenv("PORTAL_URL", "https://c3dprints-quote-portal.onrender.com")
AGENT_KEY = os.getenv("PRINTER_AGENT_KEY", "")  # must match the portal's PRINTER_AGENT_KEY

# One entry per printer. serial must match the printer's "Bambu serial" in the portal.
PRINTERS = [
    # {"ip": "192.168.1.50", "serial": "01PXXXXXXXXXXXX", "access_code": "12345678"},
]

MIN_POST_INTERVAL = 20      # seconds between posts per printer (throttle)
PUSHALL_INTERVAL = 30       # seconds between full-status requests to the printer
# ---------------------------------------------------------------------------


def post_status(serial, state, progress, remaining_min):
    try:
        r = requests.post(
            f"{PORTAL_URL.rstrip('/')}/agent/printer-report",
            json={"serial": serial, "state": state, "progress": progress, "remaining_min": remaining_min},
            headers={"X-Agent-Key": AGENT_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[{serial}] portal responded {r.status_code}: {r.text[:120]}")
    except Exception as exc:
        print(f"[{serial}] failed to post: {exc}")


def run_printer(cfg):
    serial = cfg["serial"]
    last_post = {"t": 0.0, "key": None}

    def on_connect(client, userdata, flags, rc, *args):
        if rc == 0:
            client.subscribe(f"device/{serial}/report")
            print(f"[{serial}] connected, subscribed")
        else:
            print(f"[{serial}] connect failed rc={rc}")

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8", "ignore"))
        except Exception:
            return
        p = data.get("print") or {}
        state = p.get("gcode_state")             # RUNNING / IDLE / PAUSE / FINISH / FAILED / PREPARE
        progress = p.get("mc_percent")
        remaining = p.get("mc_remaining_time")   # minutes
        if state is None and progress is None:
            return
        key = (state, progress, remaining)
        now = time.time()
        if key != last_post["key"] and now - last_post["t"] >= MIN_POST_INTERVAL:
            last_post["key"] = key
            last_post["t"] = now
            post_status(serial, state, progress, remaining)

    client = mqtt.Client()
    client.username_pw_set("bblp", cfg["access_code"])
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_message

    while True:
        try:
            client.connect(cfg["ip"], 8883, keepalive=60)
            client.loop_start()
            # Periodically ask the printer for a full status report.
            while True:
                client.publish(
                    f"device/{serial}/request",
                    json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}),
                )
                time.sleep(PUSHALL_INTERVAL)
        except Exception as exc:
            print(f"[{serial}] connection error: {exc}; retrying in 15s")
            try:
                client.loop_stop()
            except Exception:
                pass
            time.sleep(15)


def main():
    if not AGENT_KEY:
        raise SystemExit("Set PRINTER_AGENT_KEY (must match the portal's env var).")
    if not PRINTERS:
        raise SystemExit("Add at least one printer to the PRINTERS list.")
    for cfg in PRINTERS:
        threading.Thread(target=run_printer, args=(cfg,), daemon=True).start()
        print(f"started agent for {cfg['serial']} @ {cfg['ip']}")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
