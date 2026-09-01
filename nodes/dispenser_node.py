"""
dispenser_node.py — MedCart dispenser node
Receives dispense commands from coordinator via MQTT,
sends slot command to Arduino over serial,
waits for confirmation and publishes result back.

MQTT:
    Subscribes: medcart/dispense/execute   ← from coordinator
    Publishes:  medcart/dispense/status    ← result back to coordinator

Serial to Arduino:
    Send:    {"cmd":"dispense","slot":1}\n
    Receive: {"type":"dispense","slot":1,"status":"ok"}
             {"type":"dispense","slot":1,"status":"error","msg":"..."}

Slot mapping:
    Slot 1 → Servo 1 (Pin A0) → Patient with slot_number=1
    Slot 2 → Servo 2 (Pin A2) → Patient with slot_number=2

Run: python3 nodes/dispenser_node.py
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

SERIAL_PORT      = os.getenv("ARDUINO_PORT", "/dev/ttyUSB0")
SERIAL_BAUD      = int(os.getenv("ARDUINO_BAUD", "9600"))
MQTT_BROKER      = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT        = int(os.getenv("MQTT_PORT", "1883"))

SUB_EXECUTE      = "medcart/dispense/execute"
PUB_STATUS       = "medcart/dispense/status"

DISPENSE_TIMEOUT = 5    # seconds to wait for Arduino confirmation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] dispenser_node — %(message)s"
)
log = logging.getLogger("dispenser_node")


# ─── Serial ───────────────────────────────────────────────────────────────────

def find_arduino_port() -> str | None:
    for port in serial.tools.list_ports.comports():
        desc = (port.description or "").lower()
        if any(k in desc for k in ("arduino", "ch340", "cp210", "ftdi")):
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

    ser = serial.Serial(port, SERIAL_BAUD, timeout=DISPENSE_TIMEOUT)
    time.sleep(2)
    log.info("Serial open: %s @ %d baud", port, SERIAL_BAUD)
    return ser


# ─── Dispense logic ───────────────────────────────────────────────────────────

def do_dispense(ser: serial.Serial | None, payload: dict,
                client: mqtt.Client):
    """
    Send dispense command to Arduino and wait for confirmation.
    Runs in background thread — servo takes ~1.5 seconds to complete.
    """
    patient_id   = payload.get("patient_id")
    slot_number  = payload.get("slot_number")
    bed_confirmed = payload.get("bed_confirmed", "unknown")
    requested_by = payload.get("requested_by", "auto")

    if not slot_number:
        log.error("Dispense command missing slot_number")
        client.publish(PUB_STATUS, json.dumps({
            "patient_id":   patient_id,
            "slot_number":  slot_number,
            "status":       "failed",
            "msg":          "missing slot_number",
            "bed_confirmed": bed_confirmed,
            "timestamp":    datetime.now().isoformat()
        }), qos=1)
        return

    log.info("Dispensing slot %d for patient %s (bed %s)",
             slot_number, patient_id, bed_confirmed)

    if ser is None:
        # Simulator mode
        log.info("SIMULATOR: dispensing slot %d", slot_number)
        time.sleep(1.5)
        client.publish(PUB_STATUS, json.dumps({
            "patient_id":    patient_id,
            "slot_number":   slot_number,
            "bed_confirmed": bed_confirmed,
            "status":        "success",
            "msg":           "simulated",
            "timestamp":     datetime.now().isoformat()
        }), qos=1)
        return

    try:
        # Flush stale input
        ser.reset_input_buffer()

        # Send command to Arduino
        cmd = json.dumps({"cmd": "dispense", "slot": slot_number}, separators=(',', ':')) + "\n"
        ser.write(cmd.encode("utf-8"))
        log.info("Sent to Arduino: %s", cmd.strip())

        # Wait for confirmation
        deadline = time.time() + DISPENSE_TIMEOUT
        while time.time() < deadline:
            raw = ser.readline().decode("utf-8", errors="ignore").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if data.get("type") != "dispense":
                continue

            # Got dispense response
            status = data.get("status", "unknown")
            client.publish(PUB_STATUS, json.dumps({
                "patient_id":    patient_id,
                "slot_number":   slot_number,
                "bed_confirmed": bed_confirmed,
                "status":        status,
                "msg":           data.get("msg", ""),
                "timestamp":     datetime.now().isoformat()
            }), qos=1)
            log.info("Dispense result: slot=%d status=%s", slot_number, status)
            return

        # Timed out
        log.error("Dispense timeout — no response from Arduino")
        client.publish(PUB_STATUS, json.dumps({
            "patient_id":    patient_id,
            "slot_number":   slot_number,
            "bed_confirmed": bed_confirmed,
            "status":        "failed",
            "msg":           "Arduino timeout",
            "timestamp":     datetime.now().isoformat()
        }), qos=1)

    except serial.SerialException as e:
        log.error("Serial error during dispense: %s", e)
        client.publish(PUB_STATUS, json.dumps({
            "patient_id":    patient_id,
            "slot_number":   slot_number,
            "bed_confirmed": bed_confirmed,
            "status":        "failed",
            "msg":           str(e),
            "timestamp":     datetime.now().isoformat()
        }), qos=1)


# ─── MQTT ─────────────────────────────────────────────────────────────────────

def build_mqtt_client(ser: serial.Serial | None) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="dispenser_node",
        clean_session=True
    )

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected → %s:%d", MQTT_BROKER, MQTT_PORT)
            c.subscribe(SUB_EXECUTE, qos=1)
            log.info("Subscribed: %s", SUB_EXECUTE)
        else:
            log.error("MQTT connect failed rc=%s", reason_code)

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            log.error("Invalid JSON on dispense topic")
            return

        log.info("Dispense command received: slot=%s patient=%s",
                 payload.get("slot_number"), payload.get("patient_id"))

        # Run in background thread — servo operation blocks for ~1.5s
        threading.Thread(
            target=do_dispense,
            args=(ser, payload, c),
            daemon=True
        ).start()

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
        log.info("Shutting down dispenser_node…")
        if ser:
            ser.close()
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    mode = "hardware" if ser else "simulator"
    log.info("Dispenser node running (%s mode). Waiting for commands…", mode)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()