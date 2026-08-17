"""
patient_service.py — Patient identification and context assembly
Returns a PatientContext object the rest of the system acts on.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from database import (
    get_patient_by_rfid,
    get_latest_vitals,
    get_dispense_history,
)

log = logging.getLogger("patient_service")


# ─── Data structure ───────────────────────────────────────────────────────────

@dataclass
class PatientContext:
    """Everything the coordinator needs after a successful RFID scan."""
    patient_id:      int
    rfid_uid:        str
    full_name:       str
    ward:            str
    bed_number:      str
    slot_number:     int | None        # physical tray slot on the cart
    allergies:       list[str]
    latest_vitals:   Optional[dict]
    recent_dispenses: list[dict]
    identified_at:   str = field(default_factory=lambda: datetime.now().isoformat())

    def summary(self) -> str:
        slot = f"Slot {self.slot_number}" if self.slot_number else "No slot assigned"
        allergies = ", ".join(self.allergies) if self.allergies else "none"
        return (
            f"👤 {self.full_name} | {self.ward} Bed {self.bed_number}\n"
            f"💊 Tray: {slot}\n"
            f"⚠️ Allergies: {allergies}"
        )

    def has_allergy_to(self, medication_name: str) -> bool:
        return any(
            a.strip().lower() in medication_name.lower()
            for a in self.allergies
        )


# ─── Core service function ────────────────────────────────────────────────────

def identify_patient(rfid_uid: str) -> PatientContext | None:
    """
    Called with raw RFID UID string.
    Returns PatientContext on success, None if UID not registered.
    """
    rfid_uid = rfid_uid.strip().upper()
    log.info("RFID scan received: %s", rfid_uid)

    patient = get_patient_by_rfid(rfid_uid)
    if not patient:
        log.warning("Unknown RFID UID: %s", rfid_uid)
        return None

    latest_vitals    = get_latest_vitals(patient["id"])
    recent_dispenses = get_dispense_history(patient["id"], limit=5)

    allergies = (
        [a.strip() for a in patient["allergies"].split(",")]
        if patient.get("allergies") else []
    )

    ctx = PatientContext(
        patient_id       = patient["id"],
        rfid_uid         = patient["rfid_uid"],
        full_name        = patient["full_name"],
        ward             = patient.get("ward") or "Unknown",
        bed_number       = patient.get("bed_number") or "—",
        slot_number      = patient.get("slot_number"),
        allergies        = allergies,
        latest_vitals    = latest_vitals,
        recent_dispenses = recent_dispenses,
    )

    log.info("Patient identified: %s (id=%d slot=%s)",
             ctx.full_name, ctx.patient_id, ctx.slot_number)
    return ctx


# ─── Quick test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    ctx = identify_patient("E389A60D")
    if ctx:
        print("\n" + ctx.summary())
    else:
        print("Patient not found — run database.py first to seed data")