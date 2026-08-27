"""
vitals_node.py — MedCart vitals reading node
Listens for vitals trigger from coordinator via MQTT,
sends {"cmd":"vitals"} to Arduino over serial,
receives MAX30102 SpO2 and heart rate JSON,
publishes result back to coordinator via MQTT.

MQTT:
    Subscribes: medcart/vitals/trigger   ← coordinator triggers reading
    Publishes:  medcart/vitals/reading   ← SpO2 + HR result

Serial to Arduino:
    Send:    {"cmd":"vitals"}\n
    Receive: {"type":"vitals","spo2":98,"hr":72,"valid":true}

Run: python3 nodes/vitals_node.py
"""

import os
import sys
import json
import time
import signal
import logging
import threading
from datetime import datetime

import serial
import serial.tools.list_ports
import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

SERIAL_PORT  = os.getenv("ARDUINO_PORT", "/dev/ttyUSB0")
SERIAL_BAUD  = int(os.getenv("ARDUINO_BAUD", "9600"))
MQTT_BROKER  = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT    = int(os.getenv("MQTT_PORT", "1883"))

SUB_TRIGGER  = "medcart/vitals/trigger"
PUB_READING  = "medcart/vitals/reading"
PUB_ERROR    = "medcart/vitals/error"

# Timeout waiting for Arduino vitals response (seconds)
# MAX30102 needs ~4 seconds to collect 100 samples at 100Hz
VITALS_TIMEOUT = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] vitals_node — %(message)s"
)
log = logging.getLogger("vitals_node")


# ─── Serial ───────────────────────────────────────────────────────────────────

def find_arduino_port() -> str | None:
    for port in serial.tools.list_ports.comports():
        desc = (port.description or "").lower()
        if any(k in desc for k in ("arduino", "ch340", "cp210", "ftdi", "usb serial")):
            log.info("Auto-detected Arduino on %s", port.device)
            return port.device
    return None


def open_serial() -> serial.Serial | None:
    port = SERIAL_PORT
    if not os.path.exists(port):
        detected = find_arduino_port()
        if detected:
            port = detected
        else:
            log.warning("Arduino not found — running in simulator mode.")
            return None

    ser = serial.Serial(port, SERIAL_BAUD, timeout=VITALS_TIMEOUT)
    time.sleep(2)
    log.info("Serial open: %s @ %d baud", port, SERIAL_BAUD)
    return ser


# ─── Vitals reading ───────────────────────────────────────────────────────────

def request_vitals(ser: serial.Serial, patient_id: int,
                   client: mqtt.Client):
    """
    Send vitals command to Arduino and wait for response.
    Runs in a background thread so MQTT loop isn't blocked.
    MAX30102 takes ~4 seconds to collect samples.
    """
    log.info("Requesting vitals for patient %d…", patient_id)

    try:
        # Flush any stale data
        ser.reset_input_buffer()

        # Send command to Arduino
        ser.write(b'{"cmd":"vitals"}\n')
        log.info("Sent vitals command to Arduino")

        # Wait for response — read lines until we get vitals type
        deadline = time.time() + VITALS_TIMEOUT
        while time.time() < deadline:
            raw = ser.readline().decode("utf-8").strip()
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if data.get("type") != "vitals":
                continue

            # Got vitals response
            valid   = data.get("valid", False)
            spo2    = data.get("spo2", -1)
            hr      = data.get("hr", -1)

            if valid and spo2 > 0 and hr > 0:
                payload = {
                    "patient_id":    patient_id,
                    "spo2_pct":      float(spo2),
                    "heart_rate_bpm": int(hr),
                    "timestamp":     datetime.now().isoformat()
                }
                client.publish(PUB_READING, json.dumps(payload), qos=1)
                log.info("Vitals published: SpO2=%s%% HR=%sbpm", spo2, hr)
            else:
                # Sensor returned invalid reading
                client.publish(PUB_ERROR, json.dumps({
                    "patient_id": patient_id,
                    "error":      "invalid_reading",
                    "message":    "Place finger firmly on sensor and try again.",
                    "raw":        data,
                    "timestamp":  datetime.now().isoformat()
                }), qos=1)
                log.warning("Invalid vitals reading: %s", data)
            return

        # Timed out
        log.error("Vitals timeout — no response from Arduino")
        client.publish(PUB_ERROR, json.dumps({
            "patient_id": patient_id,
            "error":      "timeout",
            "message":    "Arduino did not respond in time. Check sensor connection.",
            "timestamp":  datetime.now().isoformat()
        }), qos=1)

    except serial.SerialException as e:
        log.error("Serial error during vitals read: %s", e)
        client.publish(PUB_ERROR, json.dumps({
            "patient_id": patient_id,
            "error":      "serial_error",
            "message":    str(e),
            "timestamp":  datetime.now().isoformat()
        }), qos=1)


def simulate_vitals(patient_id: int, client: mqtt.Client):
    """Simulator mode when no Arduino connected."""
    import random
    log.info("SIMULATOR: generating fake vitals for patient %d", patient_id)
    time.sleep(2)   # simulate reading delay

    spo2 = round(random.uniform(95.0, 99.0), 1)
    hr   = random.randint(60, 100)

    payload = {
        "patient_id":     patient_id,
        "spo2_pct":       spo2,
        "heart_rate_bpm": hr,
        "timestamp":      datetime.now().isoformat(),
        "simulated":      True
    }
    client.publish(PUB_READING, json.dumps(payload), qos=1)
    log.info("Simulated vitals: SpO2=%.1f%% HR=%dbpm", spo2, hr)


# ─── MQTT ─────────────────────────────────────────────────────────────────────

def build_mqtt_client(ser: serial.Serial | None) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="vitals_node",
        clean_session=True
    )

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected → %s:%d", MQTT_BROKER, MQTT_PORT)
            c.subscribe(SUB_TRIGGER, qos=1)
            log.info("Subscribed: %s", SUB_TRIGGER)
        else:
            log.error("MQTT connect failed rc=%s", reason_code)

    def on_message(c, userdata, msg):
        try:
            payload    = json.loads(msg.payload.decode("utf-8"))
            patient_id = payload.get("patient_id", 0)
        except (json.JSONDecodeError, KeyError):
            log.error("Invalid vitals trigger payload")
            return

        log.info("Vitals trigger received for patient %d", patient_id)

        # Run in background thread — reading takes several seconds
        if ser:
            t = threading.Thread(
                target=request_vitals,
                args=(ser, patient_id, c),
                daemon=True
            )
        else:
            t = threading.Thread(
                target=simulate_vitals,
                args=(patient_id, c),
                daemon=True
            )
        t.start()

    client.on_connect = on_connect
    client.on_message = on_message
    return client


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    ser    = open_serial()
    client = build_mqtt_client(ser)

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
    except ConnectionRefusedError:
        log.error("MQTT broker not reachable. Start Mosquitto first.")
        sys.exit(1)

    def shutdown(sig, frame):
        log.info("Shutting down vitals_node…")
        if ser:
            ser.close()
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    mode = "hardware" if ser else "simulator"
    log.info("Vitals node running (%s mode). Waiting for trigger…", mode)

    # Keep main thread alive
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()