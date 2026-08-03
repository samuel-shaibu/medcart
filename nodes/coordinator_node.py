"""
coordinator_node.py — MedCart brain
Subscribes to all MQTT topics, orchestrates patient identification,
dispense safety checks, vitals logging, and Telegram notifications.

Run: python3 nodes/coordinator_node.py
"""

import sys
import os
import json
import logging
import signal
from datetime import datetime

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

# ── Local imports ─────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
from patient_service import identify_patient, check_dispense_safety, PatientContext
from database import log_vitals, log_dispense, get_patient_by_id

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", "1883"))

# Topics coordinator subscribes to
SUB_RFID_SCAN       = "medcart/rfid/scan"
SUB_RFID_ERROR      = "medcart/rfid/error"
SUB_DISPENSE_CMD    = "medcart/dispense/command"
SUB_DISPENSE_STATUS = "medcart/dispense/status"
SUB_VITALS_READING  = "medcart/vitals/reading"

# Topics coordinator publishes to
PUB_PATIENT         = "medcart/patient/identified"
PUB_DISPENSE_CMD    = "medcart/dispense/command"
PUB_VITALS_ALERT    = "medcart/vitals/alert"
PUB_NOTIFY          = "medcart/notify/telegram"     # telegram_bot listens here
PUB_STATUS          = "medcart/system/status"

# Vitals thresholds for alerts
VITALS_THRESHOLDS = {
    "temperature_c":  (35.0, 38.5),   # (min, max)
    "spo2_pct":       (94.0, 100.0),
    "heart_rate_bpm": (50,   110),
    "bp_systolic":    (90,   140),
    "bp_diastolic":   (60,   90),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] coordinator — %(message)s"
)
log = logging.getLogger("coordinator")

# ─── State ────────────────────────────────────────────────────────────────────

# Active patient context (last identified patient)
active_patient: PatientContext | None = None


# ─── MQTT helpers ─────────────────────────────────────────────────────────────

def publish(client: mqtt.Client, topic: str, payload: dict):
    msg = json.dumps(payload, default=str)
    client.publish(topic, msg, qos=1)
    log.debug("→ %s", topic)


def notify(client: mqtt.Client, message: str, level: str = "info"):
    """Send a notification to Telegram bot."""
    publish(client, PUB_NOTIFY, {
        "message":   message,
        "level":     level,       # info | warning | error
        "timestamp": datetime.now().isoformat()
    })


# ─── Handlers ─────────────────────────────────────────────────────────────────

def handle_rfid_scan(client: mqtt.Client, payload: dict):
    """RFID scan received — identify patient and notify Telegram."""
    global active_patient

    uid = payload.get("uid", "").strip().upper()
    if not uid:
        return

    log.info("RFID scan: %s", uid)
    ctx = identify_patient(uid)

    if ctx is None:
        log.warning("Unknown RFID: %s", uid)
        notify(client,
               f"⚠️ Unregistered card scanned: {uid}\nPlease register this patient.",
               level="warning")
        return

    active_patient = ctx
    log.info("Patient identified: %s", ctx.full_name)

    # Publish full patient context for telegram_bot to display
    publish(client, PUB_PATIENT, {
        "patient_id":    ctx.patient_id,
        "full_name":     ctx.full_name,
        "ward":          ctx.ward,
        "bed_number":    ctx.bed_number,
        "allergies":     ctx.allergies,
        "prescriptions": ctx.prescriptions,
        "latest_vitals": ctx.latest_vitals,
        "summary":       ctx.summary(),
        "timestamp":     ctx.identified_at,
    })

    notify(client, ctx.summary(), level="info")


def handle_rfid_error(client: mqtt.Client, payload: dict):
    error = payload.get("error", "unknown")
    if error == "unregistered_uid":
        notify(client, f"⚠️ Unknown card scanned.", level="warning")
    elif error == "read_fail":
        log.debug("RFID read_fail ignored (normal hardware noise)")
    else:
        notify(client, f"🔴 RFID reader error: {error}", level="error")

def handle_dispense_command(client: mqtt.Client, payload: dict):
    """
    Dispense command from Telegram bot.
    Payload: {"patient_id": 1, "medication_id": 2, "medication_name": "Paracetamol 500mg",
               "quantity": 2.0, "requested_by": "nurse:123456"}
    """
    global active_patient

    patient_id      = payload.get("patient_id")
    medication_id   = payload.get("medication_id")
    medication_name = payload.get("medication_name", "")
    quantity        = payload.get("quantity", 1.0)
    requested_by    = payload.get("requested_by", "telegram")

    if not patient_id or not medication_id:
        notify(client, "🔴 Dispense command missing patient or medication ID.", level="error")
        return

    # Load patient context if not active
    if active_patient is None or active_patient.patient_id != patient_id:
        patient = get_patient_by_id(patient_id)
        if not patient:
            notify(client, f"🔴 Patient ID {patient_id} not found.", level="error")
            return
        active_patient = identify_patient(patient["rfid_uid"])

    # Safety check
    safe, reason = check_dispense_safety(active_patient, medication_name)
    if not safe:
        log.warning("Dispense blocked: %s", reason)
        notify(client, reason, level="error")
        return

    # Forward to dispenser node
    publish(client, PUB_DISPENSE_CMD, {
        "patient_id":    patient_id,
        "medication_id": medication_id,
        "medication_name": medication_name,
        "quantity":      quantity,
        "requested_by":  requested_by,
        "timestamp":     datetime.now().isoformat()
    })

    log.info("Dispense command forwarded: %s × %s for patient %d",
             quantity, medication_name, patient_id)


def handle_dispense_status(client: mqtt.Client, payload: dict):
    """
    Dispense result from dispenser_node.
    Payload: {"patient_id": 1, "medication_id": 2, "quantity": 2.0,
               "status": "success"|"failed", "msg": "..."}
    """
    status        = payload.get("status", "unknown")
    patient_id    = payload.get("patient_id")
    medication_id = payload.get("medication_id")
    quantity      = payload.get("quantity", 0)
    msg           = payload.get("msg", "")

    if status == "success":
        # Log to database
        log_dispense(patient_id, medication_id, quantity,
                     dispensed_by="auto", status="success")
        notify(client,
               f"✅ Dispensed {quantity} × {payload.get('medication_name', '')} successfully.",
               level="info")
    else:
        log_dispense(patient_id, medication_id, quantity,
                     dispensed_by="auto", status="failed")
        notify(client,
               f"🔴 Dispense failed: {msg}",
               level="error")


def handle_vitals_reading(client: mqtt.Client, payload: dict):
    """
    Vitals reading from vitals_node.
    Payload: {"patient_id": 1, "temperature_c": 36.8, "spo2_pct": 98.0,
               "heart_rate_bpm": 72, "bp_systolic": 120, "bp_diastolic": 80}
    """
    patient_id = payload.get("patient_id")
    if not patient_id:
        log.warning("Vitals reading missing patient_id")
        return

    # Log to database
    log_vitals(
        patient_id,
        temperature_c   = payload.get("temperature_c"),
        spo2_pct        = payload.get("spo2_pct"),
        heart_rate_bpm  = payload.get("heart_rate_bpm"),
        bp_systolic     = payload.get("bp_systolic"),
        bp_diastolic    = payload.get("bp_diastolic"),
    )

    # Check thresholds and alert if abnormal
    alerts = []
    checks = {
        "temperature_c":  payload.get("temperature_c"),
        "spo2_pct":       payload.get("spo2_pct"),
        "heart_rate_bpm": payload.get("heart_rate_bpm"),
        "bp_systolic":    payload.get("bp_systolic"),
        "bp_diastolic":   payload.get("bp_diastolic"),
    }

    for metric, value in checks.items():
        if value is None:
            continue
        low, high = VITALS_THRESHOLDS[metric]
        if not (low <= value <= high):
            alerts.append(f"{metric.replace('_', ' ')}: {value} (normal {low}–{high})")

    if alerts:
        patient_name = (active_patient.full_name
                        if active_patient and active_patient.patient_id == patient_id
                        else f"Patient #{patient_id}")
        alert_msg = (
            f"🚨 Abnormal vitals for {patient_name}:\n" +
            "\n".join(f"  • {a}" for a in alerts)
        )
        log.warning("Vitals alert: %s", alert_msg)
        publish(client, PUB_VITALS_ALERT, {
            "patient_id": patient_id,
            "alerts":     alerts,
            "timestamp":  datetime.now().isoformat()
        })
        notify(client, alert_msg, level="warning")
    else:
        log.info("Vitals logged for patient %d — all normal", patient_id)


# ─── MQTT callbacks ───────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, reason_code, properties):
    rc = reason_code
    if rc != 0:
        log.error("MQTT connect failed rc=%d", rc)
        return
    log.info("MQTT connected → %s:%d", MQTT_BROKER, MQTT_PORT)

    # Subscribe to all relevant topics
    topics = [
        SUB_RFID_SCAN,
        SUB_RFID_ERROR,
        SUB_DISPENSE_CMD,
        SUB_DISPENSE_STATUS,
        SUB_VITALS_READING,
    ]
    for topic in topics:
        client.subscribe(topic, qos=1)
        log.info("Subscribed: %s", topic)

    publish(client, PUB_STATUS, {
        "status":    "online",
        "timestamp": datetime.now().isoformat()
    })


def on_message(client, userdata, msg):
    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except json.JSONDecodeError:
        log.error("Invalid JSON on topic %s", topic)
        return

    log.debug("← %s", topic)

    if topic == SUB_RFID_SCAN:
        handle_rfid_scan(client, payload)
    elif topic == SUB_RFID_ERROR:
        handle_rfid_error(client, payload)
    elif topic == SUB_DISPENSE_CMD:
        handle_dispense_command(client, payload)
    elif topic == SUB_DISPENSE_STATUS:
        handle_dispense_status(client, payload)
    elif topic == SUB_VITALS_READING:
        handle_vitals_reading(client, payload)
    else:
        log.debug("Unhandled topic: %s", topic)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="coordinator", clean_session=True)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    except ConnectionRefusedError:
        log.error("MQTT broker not reachable at %s:%d", MQTT_BROKER, MQTT_PORT)
        log.error("Start Mosquitto first: sudo systemctl start mosquitto")
        sys.exit(1)

    def shutdown(sig, frame):
        log.info("Shutting down coordinator…")
        publish(client, PUB_STATUS, {
            "status":    "offline",
            "timestamp": datetime.now().isoformat()
        })
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Coordinator running. Waiting for events…")
    client.loop_forever()


if __name__ == "__main__":
    main()