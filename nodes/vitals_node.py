"""
vitals_node.py — MedCart vitals reading node (Pi 4 Direct I2C)
Listens for vitals trigger from coordinator via MQTT,
reads directly from MAX30102 sensor via I2C,
publishes result back to coordinator via MQTT.

MQTT:
    Subscribes: medcart/vitals/trigger   ← coordinator triggers reading
    Publishes:  medcart/vitals/reading   ← SpO2 + HR result
    Publishes:  medcart/vitals/error     ← Error messages

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

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

MQTT_BROKER  = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT    = int(os.getenv("MQTT_PORT", "1883"))

SUB_TRIGGER  = "medcart/vitals/trigger"
PUB_READING  = "medcart/vitals/reading"
PUB_ERROR    = "medcart/vitals/error"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] vitals_node — %(message)s"
)
log = logging.getLogger("vitals_node")


# ─── Sensor Initialization ────────────────────────────────────────────────────

def open_sensor():
    """Attempt to initialize the MAX30102 sensor over I2C."""
    try:
        from max30102 import MAX30102
        sensor = MAX30102()
        log.info("MAX30102 sensor initialized via I2C")
        return sensor
    except Exception as e:
        log.warning("MAX30102 not found or library missing (%s) — running in simulator mode.", e)
        return None


# ─── Vitals reading ───────────────────────────────────────────────────────────

def request_vitals(sensor, patient_id: int, client: mqtt.Client):
    """
    Reads directly from MAX30102 and filters the data.
    Runs in a background thread so MQTT loop isn't blocked.
    """
    log.info("Requesting vitals for patient %d…", patient_id)

    try:
        from max30102 import hrcalc
        
        # Take up to 4 sequential readings (approx 1-2 seconds each) 
        # to allow the sensor to stabilize and find a valid reading
        max_attempts = 4
        
        for attempt in range(max_attempts):
            # read_sequential() blocks while it gathers 100 samples
            red, ir = sensor.read_sequential()
            hr, hr_valid, spo2, spo2_valid = hrcalc.calc_hr_and_spo2(ir, red)
            
            # Apply strict filtering to ignore -999 and physically impossible bounds
            if hr_valid and spo2_valid and spo2 != -999 and hr != -999:
                if 40 <= hr <= 150 and 90 <= spo2 <= 100:
                    payload = {
                        "patient_id":    patient_id,
                        "spo2_pct":      float(spo2),
                        "heart_rate_bpm": int(hr),
                        "timestamp":     datetime.now().isoformat()
                    }
                    client.publish(PUB_READING, json.dumps(payload), qos=1)
                    log.info("Vitals published: SpO2=%.1f%% HR=%dbpm", spo2, hr)
                    return
                else:
                    log.debug("Reading out of bounds (Attempt %d): HR=%s, SpO2=%s", attempt + 1, hr, spo2)

        # Timed out / exhausted attempts with noisy data
        log.warning("Invalid vitals reading — signal too noisy or finger not detected.")
        client.publish(PUB_ERROR, json.dumps({
            "patient_id": patient_id,
            "error":      "invalid_reading",
            "message":    "Place finger firmly on sensor and try again.",
            "timestamp":  datetime.now().isoformat()
        }), qos=1)

    except Exception as e:
        log.error("Sensor error during vitals read: %s", e)
        client.publish(PUB_ERROR, json.dumps({
            "patient_id": patient_id,
            "error":      "sensor_error",
            "message":    str(e),
            "timestamp":  datetime.now().isoformat()
        }), qos=1)


def simulate_vitals(patient_id: int, client: mqtt.Client):
    """Simulator mode when no hardware is connected."""
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

def build_mqtt_client(sensor) -> mqtt.Client:
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
        if sensor:
            t = threading.Thread(
                target=request_vitals,
                args=(sensor, patient_id, c),
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
    sensor = open_sensor()
    client = build_mqtt_client(sensor)

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
    except ConnectionRefusedError:
        log.error("MQTT broker not reachable. Start Mosquitto first.")
        sys.exit(1)

    def shutdown(sig, frame):
        log.info("Shutting down vitals_node…")
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    mode = "hardware" if sensor else "simulator"
    log.info("Vitals node running (%s mode). Waiting for trigger…", mode)

    # Keep main thread alive
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()