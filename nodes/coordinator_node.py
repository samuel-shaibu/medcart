"""
coordinator_node.py — MedCart brain
Orchestrates patient identification, CV bed verification,
dispense safety, vitals logging, and Telegram notifications.

Dispense flow:
    1. cv_node detects bed number (active_bed set)
    2. Patient scans RFID (active_patient set)
    3. Coordinator cross-checks patient.bed_number == active_bed
    4. If match: Telegram shows dispense button
    5. Nurse confirms: dispenser_node triggers servo for patient's slot
    6. Logged to database

Run: python3 nodes/coordinator_node.py
"""

import sys
import os
import json
import logging
import signal
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
from patient_service import identify_patient, PatientContext
from database import log_vitals, log_dispense, get_patient_by_id

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT   = int(os.getenv("MQTT_PORT", "1883"))

# Topics subscribed to
SUB_RFID_SCAN        = "medcart/rfid/scan"
SUB_RFID_ERROR       = "medcart/rfid/error"
SUB_DISPENSE_REQUEST = "medcart/dispense/request"    # from Telegram nurse button
SUB_DISPENSE_STATUS  = "medcart/dispense/status"     # from dispenser_node result
SUB_VITALS_READING   = "medcart/vitals/reading"
SUB_CV_DETECTED      = "medcart/cv/bed_detected"
SUB_CV_ERROR         = "medcart/cv/error"
PUB_VITALS_TRIGGER   = "medcart/vitals/trigger"

# Topics published to
PUB_PATIENT          = "medcart/patient/identified"
PUB_DISPENSE_EXECUTE = "medcart/dispense/execute"    # to dispenser_node
PUB_VITALS_ALERT     = "medcart/vitals/alert"
PUB_NOTIFY           = "medcart/notify/telegram"
PUB_STATUS           = "medcart/system/status"

# Vitals thresholds
VITALS_THRESHOLDS = {
    "temperature_c":  (35.0, 38.5),
    "humidity_pct":   (30.0, 80.0),
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

active_patient: PatientContext | None = None   # set on RFID scan
active_bed:     str | None            = None   # set on CV detection


# ─── MQTT helpers ─────────────────────────────────────────────────────────────

def publish(client: mqtt.Client, topic: str, payload: dict):
    msg = json.dumps(payload, default=str)
    client.publish(topic, msg, qos=1)
    log.debug("→ %s", topic)


def notify(client: mqtt.Client, message: str, level: str = "info"):
    publish(client, PUB_NOTIFY, {
        "message":   message,
        "level":     level,
        "timestamp": datetime.now().isoformat()
    })


# ─── CV handlers ──────────────────────────────────────────────────────────────

def handle_cv_detection(client: mqtt.Client, payload: dict):
    """Bed number detected by CV node."""
    global active_bed

    bed = str(payload.get("bed_number", "")).strip()
    conf = payload.get("confidence", 0.0)
    if not bed:
        return

    # Only notify if bed changed
    if bed != active_bed:
        active_bed = bed
        log.info("Bed confirmed by CV: %s (conf=%.2f)", bed, conf)
        notify(client, f"📷 Bed {bed} detected", level="info")

        # If patient already identified, re-check bed match
        if active_patient:
            _check_bed_match(client)


def handle_cv_error(client: mqtt.Client, payload: dict):
    error = payload.get("error", "unknown")
    if error == "no_bed_detected":
        log.debug("CV: no bed detected")
    else:
        notify(client, f"🔴 CV error: {error}", level="error")


def _check_bed_match(client: mqtt.Client) -> bool:
    global active_patient, active_bed

    if not active_patient or not active_bed:
        return False

    patient_bed = str(active_patient.bed_number).strip()

    if active_bed == patient_bed:
        log.info("Bed match confirmed: patient=%s bed=%s", active_patient.full_name, active_bed)
        
        # 1. Notify Telegram of automation
        notify(client,
               f"✅ Location confirmed — Bed {active_bed}\n"
               f"👤 {active_patient.full_name} is assigned here.\n"
               f"⚙️ Auto-dispensing Slot {active_patient.slot_number}...\n"
               f"⏱️ Taking vitals in 4 seconds.",
               level="info")
        
        # 2. Fire Dispense Command Instantly
        publish(client, PUB_DISPENSE_EXECUTE, {
            "patient_id":   active_patient.patient_id,
            "full_name":    active_patient.full_name,
            "slot_number":  active_patient.slot_number,
            "bed_confirmed": active_bed,
            "requested_by": "auto",
            "timestamp":    datetime.now().isoformat()
        })
        
        # 3. Fire Vitals Trigger with Grace Period
        def delayed_trigger():
            time.sleep(4)
            publish(client, PUB_VITALS_TRIGGER, {
                "patient_id": active_patient.patient_id,
                "timestamp":  datetime.now().isoformat()
            })
            log.info("Vitals triggered for patient %d", active_patient.patient_id)
            
        threading.Thread(target=delayed_trigger, daemon=True).start()
        return True

# ─── RFID handler ─────────────────────────────────────────────────────────────

def handle_rfid_scan(client: mqtt.Client, payload: dict):
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

    # Publish patient context to Telegram
    publish(client, PUB_PATIENT, {
        "patient_id":   ctx.patient_id,
        "full_name":    ctx.full_name,
        "ward":         ctx.ward,
        "bed_number":   ctx.bed_number,
        "slot_number":  ctx.slot_number,
        "allergies":    ctx.allergies,
        "latest_vitals": ctx.latest_vitals,
        "summary":      ctx.summary(),
        "timestamp":    ctx.identified_at,
    })

    notify(client, ctx.summary(), level="info")

    # Auto-trigger vitals reading
    # publish(client, PUB_VITALS_TRIGGER, {
    #    "patient_id": ctx.patient_id,
    #   "timestamp":  datetime.now().isoformat()
    # })
    #log.info("Vitals trigger sent for patient %d", ctx.patient_id)

    # Immediately check bed match
    _check_bed_match(client)


def handle_rfid_error(client: mqtt.Client, payload: dict):
    error = payload.get("error", "unknown")
    if error == "read_fail":
        log.debug("RFID read_fail ignored")
    elif error == "unregistered_uid":
        notify(client, "⚠️ Unknown card scanned.", level="warning")
    else:
        notify(client, f"🔴 RFID error: {error}", level="error")

# ─── Dispense handlers ────────────────────────────────────────────────────────

def handle_dispense_request(client: mqtt.Client, payload: dict):
    """
    Nurse tapped dispense button in Telegram.
    Re-verify bed match then forward to dispenser_node.
    """
    global active_patient, active_bed

    req_patient_id = payload.get("patient_id")
    requested_by   = payload.get("requested_by", "telegram")

    if not active_patient:
        notify(client, "⚠️ No patient identified. Scan RFID wristband first.", level="warning")
        return

    # SAFETY NET 1: If Telegram drops the ID, fallback to the active patient
    if req_patient_id is None:
        req_patient_id = active_patient.patient_id

    # Safely compare IDs as strings to prevent network type mismatches
    if str(active_patient.patient_id) != str(req_patient_id):
        notify(client, "⚠️ Patient mismatch. Scan wristband again.", level="warning")
        return

    if not active_bed:
        notify(client,
               "⛔ Dispense blocked — bed not confirmed.\n"
               "Point camera at the bed number sign first.",
               level="error")
        return

    patient_bed = str(active_patient.bed_number).strip()
    if active_bed != patient_bed:
        notify(client,
               f"⛔ Dispense blocked — wrong bed.\n"
               f"Camera sees Bed {active_bed}, patient belongs to Bed {patient_bed}.",
               level="error")
        return

    if not active_patient.slot_number:
        notify(client,
               f"⛔ Dispense blocked — no slot assigned to {active_patient.full_name}.\n"
               f"Update patient record with a slot number.",
               level="error")
        return

    # GUARANTEE the patient_id is an integer so the SQLite database doesn't crash
    safe_patient_id = int(active_patient.patient_id) if active_patient.patient_id is not None else 0

    log.info("Dispense approved: patient=%s slot=%d bed=%s",
             active_patient.full_name, active_patient.slot_number, active_bed)

    publish(client, PUB_DISPENSE_EXECUTE, {
        "patient_id":   safe_patient_id,
        "full_name":    active_patient.full_name,
        "slot_number":  int(active_patient.slot_number),
        "bed_confirmed": active_bed,
        "requested_by": requested_by,
        "timestamp":    datetime.now().isoformat()
    })

    notify(client,
           f"💊 Dispensing tray for {active_patient.full_name}\n"
           f"Slot {active_patient.slot_number} — Bed {active_bed}",
           level="info")


def handle_dispense_status(client: mqtt.Client, payload: dict):
    """Result back from dispenser_node."""
    global active_patient
    
    status      = payload.get("status", "unknown")
    patient_id  = payload.get("patient_id")
    slot_number = payload.get("slot_number")
    bed         = payload.get("bed_confirmed")
    msg         = payload.get("msg", "")

    # SAFETY NET 2: Catch missing IDs returning from the dispenser node
    if patient_id is None and active_patient:
        patient_id = active_patient.patient_id

    # Ensure patient_id is an integer for the database
    try:
        patient_id = int(patient_id)
    except (TypeError, ValueError):
        patient_id = 0

    # The Arduino returns "ok", but simulator returns "success". Accept both!
    if status in ("success", "ok"):
        log_dispense(patient_id, slot_number,
                     dispensed_by="auto", status="success",
                     bed_confirmed=bed)
        notify(client,
               f"✅ Tray dispensed successfully.\n"
               f"Slot {slot_number} — Bed {bed}",
               level="info")
    else:
        log_dispense(patient_id, slot_number,
                     dispensed_by="auto", status="failed",
                     bed_confirmed=bed, notes=msg)
        notify(client, f"🔴 Dispense failed: {msg}", level="error")

# ─── Vitals handler ───────────────────────────────────────────────────────────

def handle_vitals_reading(client: mqtt.Client, payload: dict):
    patient_id = payload.get("patient_id")
    if not patient_id:
        log.warning("Vitals missing patient_id")
        return

    log_vitals(
        patient_id,
        #temperature_c   = payload.get("temperature_c"),
        #humidity_pct    = payload.get("humidity_pct"),
        spo2_pct        = payload.get("spo2_pct"),
        heart_rate_bpm  = payload.get("heart_rate_bpm"),
        bp_systolic     = payload.get("bp_systolic"),
        bp_diastolic    = payload.get("bp_diastolic"),
    )

    # Check thresholds
    alerts = []
    checks = {
        "temperature_c":  payload.get("temperature_c"),
        "humidity_pct":   payload.get("humidity_pct"),
        "spo2_pct":       payload.get("spo2_pct"),
        "heart_rate_bpm": payload.get("heart_rate_bpm"),
        "bp_systolic":    payload.get("bp_systolic"),
        "bp_diastolic":   payload.get("bp_diastolic"),
    }

    for metric, value in checks.items():
        if value is None:
            continue
        if metric not in VITALS_THRESHOLDS:
            continue
        low, high = VITALS_THRESHOLDS[metric]
        if not (low <= value <= high):
            alerts.append(f"{metric.replace('_', ' ')}: {value} (normal {low}–{high})")

    if alerts:
        patient_name = (
            active_patient.full_name
            if active_patient and active_patient.patient_id == patient_id
            else f"Patient #{patient_id}"
        )
        alert_msg = (
            f"🚨 Abnormal vitals for {patient_name}:\n" +
            "\n".join(f"  • {a}" for a in alerts)
        )
        log.warning(alert_msg)
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
    if reason_code != 0:
        log.error("MQTT connect failed rc=%s", reason_code)
        return

    log.info("MQTT connected → %s:%d", MQTT_BROKER, MQTT_PORT)

    topics = [
        SUB_RFID_SCAN,
        SUB_RFID_ERROR,
        SUB_DISPENSE_REQUEST,
        SUB_DISPENSE_STATUS,
        SUB_VITALS_READING,
        SUB_CV_DETECTED,
        SUB_CV_ERROR,
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
        log.error("Invalid JSON on %s", topic)
        return

    log.debug("← %s", topic)

    handlers = {
        SUB_RFID_SCAN:        handle_rfid_scan,
        SUB_RFID_ERROR:       handle_rfid_error,
        SUB_DISPENSE_REQUEST: handle_dispense_request,
        SUB_DISPENSE_STATUS:  handle_dispense_status,
        SUB_VITALS_READING:   handle_vitals_reading,
        SUB_CV_DETECTED:      handle_cv_detection,
        SUB_CV_ERROR:         handle_cv_error,
    }

    handler = handlers.get(topic)
    if handler:
        handler(client, payload)
    else:
        log.debug("Unhandled topic: %s", topic)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="coordinator",
        clean_session=True
    )
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    except ConnectionRefusedError:
        log.error("MQTT broker not reachable. Start Mosquitto first.")
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