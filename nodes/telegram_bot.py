"""
telegram_bot.py — MedCart Telegram operator interface

Commands:
    /start    — welcome message
    /patient  — active patient info
    /dispense — dispense tray for active patient
    /vitals   — latest vitals
    /scan     — trigger CV bed number scan
    /move     — movement control pad
    /status   — system status

Run: python3 nodes/telegram_bot.py
"""

import sys
import os
import json
import logging
import asyncio
import queue
from datetime import datetime
from telegram.request import HTTPXRequest

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.error import TimedOut, NetworkError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
MQTT_BROKER      = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT        = int(os.getenv("MQTT_PORT", "1883"))

SUB_NOTIFY       = "medcart/notify/telegram"
SUB_PATIENT      = "medcart/patient/identified"
SUB_VITALS_ALERT = "medcart/vitals/alert"
SUB_STATUS       = "medcart/system/status"
SUB_VITALS_READING = "medcart/vitals/reading"
SUB_VITALS_ERROR   = "medcart/vitals/error"

PUB_DISPENSE     = "medcart/dispense/request"     # updated topic
PUB_MOVEMENT     = "medcart/movement/command"
PUB_CV_TRIGGER   = "medcart/cv/trigger"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] telegram_bot — %(message)s"
)
log = logging.getLogger("telegram_bot")

# ─── Shared state ─────────────────────────────────────────────────────────────

active_patient: dict | None = None
message_queue:  queue.Queue = queue.Queue()
mqtt_client:    mqtt.Client | None = None


# ─── MQTT ─────────────────────────────────────────────────────────────────────

def mqtt_publish(topic: str, payload: dict):
    if mqtt_client:
        mqtt_client.publish(topic, json.dumps(payload, default=str), qos=1)


def on_mqtt_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        log.info("MQTT connected → %s:%d", MQTT_BROKER, MQTT_PORT)
        # Add the two new topics to this list
        for topic in [SUB_NOTIFY, SUB_PATIENT, SUB_VITALS_ALERT, SUB_STATUS, SUB_VITALS_READING, SUB_VITALS_ERROR]:
            client.subscribe(topic, qos=1)
            log.info("Subscribed: %s", topic)
    else:
        log.error("MQTT connect failed rc=%s", reason_code)


def on_mqtt_message(client, userdata, msg):
    global active_patient
    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except json.JSONDecodeError:
        return

    if topic == SUB_PATIENT:
        active_patient = payload
        log.info("Active patient updated: %s", payload.get("full_name"))

    elif topic == SUB_NOTIFY:
        message = payload.get("message", "")
        level   = payload.get("level", "info")
        icon    = {"info": "ℹ️", "warning": "⚠️", "error": "🔴"}.get(level, "ℹ️")
        message_queue.put(f"{icon} {message}")

    elif topic == SUB_VITALS_ALERT:
        alerts = payload.get("alerts", [])
        if alerts:
            text = "🚨 <b>Vitals Alert</b>\n" + "\n".join(f"• {a}" for a in alerts)
            message_queue.put(text)

    elif topic == SUB_STATUS:
        status = payload.get("status", "")
        if status == "offline":
            message_queue.put("🔴 MedCart coordinator went offline.")
        elif status == "online":
            message_queue.put("✅ MedCart coordinator is online.")

    elif topic == SUB_VITALS_READING:
        hr = payload.get("heart_rate_bpm")
        spo2 = payload.get("spo2_pct")
        
        # Only push if we actually got valid numbers
        if hr is not None and spo2 is not None:
            now_12hr = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
            text = (
                f"✅ <b>New Vitals Saved</b>\n"
                f"💓 Heart rate: {hr} bpm\n"
                f"🩸 SpO2: {spo2}%\n"
                f"🕐 Recorded: {now_12hr}"
            )
            message_queue.put(text)

    elif topic == SUB_VITALS_ERROR:
        # Catch hardware timeouts if no finger is detected
        error_msg = payload.get("error", "Finger not detected or signal too noisy.")
        message_queue.put(f"⚠️ <b>Vitals Failed:</b> {error_msg}\nPlease adjust finger and try again.")


def start_mqtt():
    global mqtt_client
    mqtt_client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="telegram_bot",
        clean_session=True
    )
    mqtt_client.on_connect = on_mqtt_connect
    mqtt_client.on_message = on_mqtt_message
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
        log.info("MQTT client started")
    except ConnectionRefusedError:
        log.error("MQTT broker not reachable. Start Mosquitto first.")
        sys.exit(1)


# ─── Queue worker ─────────────────────────────────────────────────────────────

async def queue_worker(app: Application):
    while True:
        await asyncio.sleep(0.2)
        while not message_queue.empty():
            try:
                text = message_queue.get_nowait()
                try:
                    await app.bot.send_message(
                        chat_id    = TELEGRAM_CHAT_ID,
                        text       = text,
                        parse_mode = "HTML"
                    )
                except (TimedOut, NetworkError) as e:
                    log.warning("Telegram send failed (retrying): %s", e)
                    message_queue.put(text)
                    await asyncio.sleep(2)
            except queue.Empty:
                break


# ─── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>MedCart Control Panel</b>\n\n"
        "Commands:\n"
        "/patient  — active patient info\n"
        "/dispense — dispense medication tray\n"
        "/vitals   — latest vitals\n"
        "/scan     — scan bed number (CV)\n"
        "/move     — movement controls\n"
        "/status   — system status",
        parse_mode="HTML"
    )


async def cmd_patient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_patient:
        await update.message.reply_text("⚠️ No patient identified yet. Scan a wristband.")
        return

    p         = active_patient
    allergies = ", ".join(p.get("allergies", [])) or "None"
    slot      = p.get("slot_number")
    slot_text = f"Slot {slot}" if slot else "⚠️ No slot assigned"

    vitals = p.get("latest_vitals")
    vitals_text = (
        f"  💓 HR: {vitals.get('heart_rate_bpm', '—')} bpm\n"
        f"  🩸 SpO2: {vitals.get('spo2_pct', '—')}%"
    ) if vitals else "  No vitals recorded yet"

    await update.message.reply_text(
        f"👤 <b>{p['full_name']}</b>\n"
        f"🏥 {p['ward']} — Bed {p['bed_number']}\n"
        f"💊 Tray: {slot_text}\n"
        f"⚠️ Allergies: {allergies}\n\n"
        f"📊 <b>Latest Vitals:</b>\n{vitals_text}",
        parse_mode="HTML"
    )

async def cmd_vitals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_patient:
        await update.message.reply_text("⚠️ No patient identified yet.")
        return

    vitals = active_patient.get("latest_vitals")
    if not vitals:
        await update.message.reply_text("No vitals recorded yet for this patient.")
        return

    # Convert ISO time to 12-Hour AM/PM format
    recorded = vitals.get('recorded_at', '—')
    if recorded != '—':
        try:
            dt = datetime.fromisoformat(recorded)
            recorded = dt.strftime("%Y-%m-%d %I:%M:%S %p")
        except ValueError:
            pass

    await update.message.reply_text(
        f"📊 <b>Latest Vitals — {active_patient['full_name']}</b>\n"
        f"💓 Heart rate: {vitals.get('heart_rate_bpm', '—')} bpm\n"
        f"🩸 SpO2: {vitals.get('spo2_pct', '—')}%\n"
        f"🕐 Recorded: {recorded}",
        parse_mode="HTML"
    )

async def cmd_vitals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_patient:
        await update.message.reply_text("⚠️ No patient identified yet.")
        return

    vitals = active_patient.get("latest_vitals")
    if not vitals:
        await update.message.reply_text("No vitals recorded yet for this patient.")
        return

    await update.message.reply_text(
        f"📊 <b>Latest Vitals — {active_patient['full_name']}</b>\n"
        f"🌡 Temperature: {vitals.get('temperature_c', '—')}°C\n"
        f"💧 Humidity: {vitals.get('humidity_pct', '—')}%\n"
        f"💓 Heart rate: {vitals.get('heart_rate_bpm', '—')} bpm\n"
        f"🩸 SpO2: {vitals.get('spo2_pct', '—')}%\n"
        f"🕐 Recorded: {vitals.get('recorded_at', '—')}",
        parse_mode="HTML"
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mqtt_publish(PUB_CV_TRIGGER, {"timestamp": datetime.now().isoformat()})
    await update.message.reply_text(
        "📷 Scanning for bed number…\nPoint the camera at the bed number sign."
    )


async def cmd_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⬆️ Forward",  callback_data="move:forward")],
        [
            InlineKeyboardButton("⬅️ Left",  callback_data="move:left"),
            InlineKeyboardButton("⏹ Stop",   callback_data="move:stop"),
            InlineKeyboardButton("➡️ Right", callback_data="move:right"),
        ],
        [InlineKeyboardButton("⬇️ Backward", callback_data="move:backward")],
    ]
    await update.message.reply_text(
        "🕹 <b>Cart Movement Control</b>\nTap a direction:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    patient_info = (
        f"👤 {active_patient['full_name']} — Bed {active_patient['bed_number']}"
        if active_patient else "👤 No patient identified"
    )
    await update.message.reply_text(
        f"📡 <b>System Status</b>\n"
        f"🟢 Bot: online\n"
        f"{patient_info}\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode="HTML"
    )


# ─── Inline button callbacks ──────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # ── Movement ──
    if data.startswith("move:"):
        direction = data.split(":")[1]
        mqtt_publish(PUB_MOVEMENT, {
            "command":   direction,
            "timestamp": datetime.now().isoformat()
        })
        icons = {"forward": "⬆️", "backward": "⬇️",
                 "left": "⬅️", "right": "➡️", "stop": "⏹"}
        await query.edit_message_text(
            f"🕹 <b>Cart Movement</b>\n{icons.get(direction,'')} Sent: <b>{direction}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬆️ Forward",  callback_data="move:forward")],
                [
                    InlineKeyboardButton("⬅️ Left",  callback_data="move:left"),
                    InlineKeyboardButton("⏹ Stop",   callback_data="move:stop"),
                    InlineKeyboardButton("➡️ Right", callback_data="move:right"),
                ],
                [InlineKeyboardButton("⬇️ Backward", callback_data="move:backward")],
            ])
        )

    # ── Dispense cancel ──
    elif data == "dispense:cancel":
        await query.edit_message_text("❌ Dispense cancelled.")

    # ── Dispense confirm ──
    elif data.startswith("dispense:"):
        parts = data.split(":")
        if len(parts) < 3:
            return
        _, patient_id, slot = parts

        if not active_patient:
            await query.edit_message_text("⚠️ No active patient.")
            return

        # Send dispense request to coordinator
        # Coordinator will verify bed match before executing
        mqtt_publish(PUB_DISPENSE, {
            "patient_id":   int(patient_id),
            "slot_number":  int(slot),
            "requested_by": f"telegram:{query.from_user.id}",
            "timestamp":    datetime.now().isoformat()
        })

        await query.edit_message_text(
            f"💊 Dispense request sent for <b>{active_patient['full_name']}</b>\n"
            f"Slot {slot} — awaiting bed verification…",
            parse_mode="HTML"
        )


# ─── Entry point ──────────────────────────────────────────────────────────────

async def post_init(app: Application):
    asyncio.create_task(queue_worker(app))
    log.info("Queue worker started")


def main():
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN not set in .env")
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID not set in .env")
        sys.exit(1)

    start_mqtt()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(HTTPXRequest(connect_timeout=30, read_timeout=30))
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("patient",  cmd_patient))
    app.add_handler(CommandHandler("dispense", cmd_dispense))
    app.add_handler(CommandHandler("vitals",   cmd_vitals))
    app.add_handler(CommandHandler("scan",     cmd_scan))
    app.add_handler(CommandHandler("move",     cmd_move))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback))

    async def error_handler(update, context):
        if isinstance(context.error, (TimedOut, NetworkError)):
            log.warning("Network timeout — retrying automatically.")
        else:
            log.error("Unexpected error: %s", context.error)

    app.add_error_handler(error_handler)

    log.info("Telegram bot running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()