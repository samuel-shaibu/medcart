"""
rfid_node.py — RFID serial reader node
Reads JSON from Arduino over USB serial, filters for RFID messages only,
publishes scan events to MQTT. Ignores motor acks and system messages.

Arduino sends:  {"type":"rfid","uid":"A1B2C3D4","status":"ok"}
                {"type":"rfid","uid":"","status":"error","msg":"read_fail"}
                {"type":"move","cmd":"F","speed":180}   ← ignored here
                {"type":"system","status":"ready"}      ← ignored here

MQTT publishes: medcart/rfid/scan  → {"uid": "A1B2C3D4", "timestamp": "..."}
                medcart/rfid/error → {"error": "...", "timestamp": "..."}

Dependencies:
    pip install pyserial paho-mqtt python-dotenv
"""

import sys
import os
import json
import time
import signal
import logging
from datetime import datetime

import serial
import serial.tools.list_ports
import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

SERIAL_PORT  = os.getenv("ARDUINO_PORT", "/dev/ttyACM0")
SERIAL_BAUD  = int(os.getenv("ARDUINO_BAUD", "9600"))
MQTT_BROKER  = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT    = int(os.getenv("MQTT_PORT", "1883"))

TOPIC_SCAN   = "medcart/rfid/scan"
TOPIC_ERROR  = "medcart/rfid/error"
SCAN_COOLDOWN = 3.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] rfid_node — %(message)s"
)
log = logging.getLogger("rfid_node")


# ─── MQTT ─────────────────────────────────────────────────────────────────────

def build_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="rfid_node",
        clean_session=True
    )

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected → %s:%d", MQTT_BROKER, MQTT_PORT)
        else:
            log.error("MQTT connect failed rc=%s", reason_code)

    client.on_connect    = on_connect
    client.on_disconnect = lambda c, u, flags, rc, p=None: log.warning("MQTT disconnected")
    return client


def publish(client: mqtt.Client, topic: str, payload: dict):
    msg = json.dumps(payload, default=str)
    result = client.publish(topic, msg, qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        log.error("Publish failed on %s", topic)
    else:
        log.debug("→ %s : %s", topic, msg)


# ─── Serial ───────────────────────────────────────────────────────────────────

def find_arduino_port() -> str | None:
    for port in serial.tools.list_ports.comports():
        desc = (port.description or "").lower()
        if any(k in desc for k in ("arduino", "ch340", "cp210", "ftdi", "usb serial")):
            log.info("Auto-detected Arduino on %s (%s)", port.device, port.description)
            return port.device
    return None


def open_serial() -> serial.Serial:
    port = SERIAL_PORT
    if not os.path.exists(port):
        detected = find_arduino_port()
        if detected:
            port = detected
        else:
            log.error("Arduino not found on %s and auto-detect failed.", SERIAL_PORT)
            sys.exit(1)

    ser = serial.Serial(port, SERIAL_BAUD, timeout=1)
    time.sleep(2)
    log.info("Serial open: %s @ %d baud", port, SERIAL_BAUD)
    return ser


# ─── Main loop ────────────────────────────────────────────────────────────────

def run(client: mqtt.Client, ser: serial.Serial):
    log.info("Listening for Arduino messages…")

    last_uid  = None
    last_time = 0.0

    while True:
        try:
            raw = ser.readline().decode("utf-8").strip()
        except (serial.SerialException, UnicodeDecodeError) as e:
            log.error("Serial read error: %s", e)
            time.sleep(1)
            continue

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("Non-JSON line: %s", raw)
            continue

        # ── Filter: only handle RFID messages ────────────────────────────────
        msg_type = data.get("type", "")

        if msg_type == "system":
            log.info("Arduino: %s", data.get("status", ""))
            continue

        if msg_type == "move":
            log.debug("Motor ack ignored: %s", raw)
            continue

        if msg_type != "rfid":
            log.debug("Unknown message type '%s' — ignored", msg_type)
            continue

        # ── Process RFID message ──────────────────────────────────────────────
        status = data.get("status", "ok")

        if status != "ok":
            publish(client, TOPIC_ERROR, {
                "error":     data.get("msg", "arduino_error"),
                "timestamp": datetime.now().isoformat()
            })
            continue

        uid = data.get("uid", "").strip().upper()
        if not uid:
            continue

        now = time.time()

        # Debounce
        if uid == last_uid and (now - last_time) < SCAN_COOLDOWN:
            continue

        last_uid  = uid
        last_time = now

        log.info("Card scanned: %s", uid)
        publish(client, TOPIC_SCAN, {
            "uid":       uid,
            "timestamp": datetime.now().isoformat()
        })


# ─── Simulator ────────────────────────────────────────────────────────────────

def run_simulator(client: mqtt.Client):
    log.info("SIMULATOR mode — type a UID to simulate a scan.")
    log.info("Test UID: A1B2C3D4")
    try:
        while True:
            uid = input("\nScan UID (q to quit): ").strip().upper()
            if uid in ("Q", "QUIT"):
                break
            if not uid:
                continue
            publish(client, TOPIC_SCAN, {
                "uid":       uid,
                "timestamp": datetime.now().isoformat()
            })
    except (KeyboardInterrupt, EOFError):
        pass


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    client = build_mqtt_client()
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
    except ConnectionRefusedError:
        log.warning("MQTT broker unreachable — publishes will be skipped.")

    def shutdown(sig, frame):
        log.info("Shutting down rfid_node…")
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        ser = open_serial()
        run(client, ser)
    except SystemExit:
        run_simulator(client)


if __name__ == "__main__":
    main()