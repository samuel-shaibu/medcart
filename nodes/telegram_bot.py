"""
telegram_bot.py — MedCart Telegram operator interface
Receives notifications from coordinator via MQTT and displays them to the nurse.
Sends commands back to coordinator via MQTT (dispense, movement, status).

Commands:
    /start      — welcome message
    /patient    — show current active patient info
    /dispense   — show dispense menu for active patient
    /vitals     — show latest vitals for active patient
    /status     — system status
    /move       — movement control pad

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

PUB_DISPENSE     = "medcart/dispense/command"
PUB_MOVEMENT     = "medcart/movement/command"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] telegram_bot — %(message)s"
)
log = logging.getLogger("telegram_bot")

# ─── Shared state ─────────────────────────────────────────────────────────────

active_patient: dict | None = None

# Thread-safe queue: MQTT thread puts messages here, async loop sends them
message_queue: queue.Queue = queue.Queue()

# MQTT client
mqtt_client: mqtt.Client | None = None


# ─── MQTT ─────────────────────────────────────────────────────────────────────

def mqtt_publish(topic: str, payload: dict):
    if mqtt_client:
        mqtt_client.publish(topic, json.dumps(payload, default=str), qos=1)


def on_mqtt_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        log.info("MQTT connected → %s:%d", MQTT_BROKER, MQTT_PORT)
        for topic in [SUB_NOTIFY, SUB_PATIENT, SUB_VITALS_ALERT, SUB_STATUS]:
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


# ─── Queue worker — drains message_queue and sends to Telegram ───────────────

async def queue_worker(app: Application):
    """Runs as an async task, sends queued messages to Telegram."""
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
                    log.warning("Telegram send failed (will retry): %s", e)
                    message_queue.put(text)   # put it back
                    await asyncio.sleep(2)
            except queue.Empty:
                break


# ─── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>MedCart Control Panel</b>\n\n"
        "Available commands:\n"
        "/patient — active patient info\n"
        "/dispense — dispense medication\n"
        "/vitals — latest vitals\n"
        "/move — movement controls\n"
        "/status — system status",
        parse_mode="HTML"
    )


async def cmd_patient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_patient:
        await update.message.reply_text("⚠️ No patient identified yet. Scan a wristband.")
        return

    p = active_patient
    prescriptions = p.get("prescriptions", [])
    rx_lines = "\n".join(
        f"  • {rx['med_name']} — {rx['dosage']} {rx['unit']} @ {rx['frequency']}"
        for rx in prescriptions
    ) or "  None"

    vitals = p.get("latest_vitals")
    vitals_text = (
        f"  🌡 Temp: {vitals.get('temperature_c', '—')}°C\n"
        f"  💓 HR: {vitals.get('heart_rate_bpm', '—')} bpm\n"
        f"  🩸 SpO2: {vitals.get('spo2_pct', '—')}%\n"
        f"  💊 BP: {vitals.get('bp_systolic', '—')}/{vitals.get('bp_diastolic', '—')} mmHg"
    ) if vitals else "  No vitals recorded yet"

    allergies = ", ".join(p.get("allergies", [])) or "None"

    await update.message.reply_text(
        f"👤 <b>{p['full_name']}</b>\n"
        f"🏥 {p['ward']} — Bed {p['bed_number']}\n"
        f"⚠️ Allergies: {allergies}\n\n"
        f"💊 <b>Prescriptions:</b>\n{rx_lines}\n\n"
        f"📊 <b>Latest Vitals:</b>\n{vitals_text}",
        parse_mode="HTML"
    )


async def cmd_dispense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_patient:
        await update.message.reply_text("⚠️ No patient identified yet. Scan a wristband.")
        return

    prescriptions = active_patient.get("prescriptions", [])
    if not prescriptions:
        await update.message.reply_text("⚠️ No active prescriptions for this patient.")
        return

    keyboard = [
        [InlineKeyboardButton(
            f"💊 {rx['med_name']} ({rx['dosage']} {rx['unit']})",
            callback_data=f"dispense:{rx['medication_id']}:{rx['med_name']}:{rx['dosage']}"
        )]
        for rx in prescriptions
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="dispense:cancel")])

    await update.message.reply_text(
        f"Select medication to dispense for <b>{active_patient['full_name']}</b>:",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
        f"💓 Heart rate: {vitals.get('heart_rate_bpm', '—')} bpm\n"
        f"🩸 SpO2: {vitals.get('spo2_pct', '—')}%\n"
        f"💊 BP: {vitals.get('bp_systolic', '—')}/{vitals.get('bp_diastolic', '—')} mmHg\n"
        f"🕐 Recorded: {vitals.get('recorded_at', '—')}",
        parse_mode="HTML"
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
        f"👤 Active patient: {active_patient['full_name']}"
        if active_patient else
        "👤 No patient identified"
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

    elif data == "dispense:cancel":
        await query.edit_message_text("❌ Dispense cancelled.")

    elif data.startswith("dispense:"):
        parts = data.split(":", 3)
        if len(parts) < 4:
            return
        _, med_id, med_name, quantity = parts
        if not active_patient:
            await query.edit_message_text("⚠️ No active patient.")
            return
        mqtt_publish(PUB_DISPENSE, {
            "patient_id":      active_patient["patient_id"],
            "medication_id":   int(med_id),
            "medication_name": med_name,
            "quantity":        float(quantity),
            "requested_by":    f"telegram:{query.from_user.id}",
            "timestamp":       datetime.now().isoformat()
        })
        await query.edit_message_text(
            f"💊 Dispensing <b>{quantity} × {med_name}</b> for "
            f"<b>{active_patient['full_name']}</b>…",
            parse_mode="HTML"
        )


# ─── Entry point ──────────────────────────────────────────────────────────────

async def post_init(app: Application):
    """Start the queue worker after the app initialises."""
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
    app.add_handler(CommandHandler("move",     cmd_move))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("Telegram bot running…")

    async def error_handler(update, context):
        if isinstance(context.error, (TimedOut, NetworkError)):
            log.warning("Network timeout — will retry automatically.")
        else:
            log.error("Unexpected error: %s", context.error)

    # Add this line inside main() before app.run_polling:
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()