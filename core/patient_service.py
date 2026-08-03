"""
patient_service.py — Patient identification and context assembly
Called whenever an RFID scan event arrives (from rfid_node via MQTT or direct call).
Returns a PatientContext object the rest of the system acts on.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from database import (
    get_patient_by_rfid,
    get_active_prescriptions,
    get_latest_vitals,
    get_dispense_history,
)

log = logging.getLogger("patient_service")


# ______ Data Structures _____________________________________________
@dataclass
class PatientContext:
    """Everything the coordinator needs after a successful RFID scan."""
    patient_id:      int
    rfid_uid:        str
    full_name:       str
    ward:            str
    bed_number:      str
    allergies:       list[str]          # split from comma string
    prescriptions:   list[dict]         # from get_active_prescriptions()
    latest_vitals:   Optional[dict]
    recent_dispenses: list[dict]
    identified_at:   str = field(default_factory=lambda: datetime.now().isoformat())
 
    def summary(self) -> str:
        """Human-readable one-liner for Telegram / display."""
        rx_names = [p["med_name"] for p in self.prescriptions]
        meds = ", ".join(rx_names) if rx_names else "no active prescriptions"
        return (
            f"👤 {self.full_name} | {self.ward} Bed {self.bed_number}\n"
            f"💊 Medications: {meds}\n"
            f"⚠️  Allergies: {', '.join(self.allergies) if self.allergies else 'none'}"
        )
 
    def has_allergy_to(self, medication_name: str) -> bool:
        return any(
            a.strip().lower() in medication_name.lower()
            for a in self.allergies
        )
 
# ______ Core service function _____________________________________________
def identify_patient(rfid_uid: str) -> PatientContext | None:
    """
    Called with raw RFID UID string (e.g. 'A1B2C3D4').
    Returns PatientContext on success, None if UID not registered.
    """
    rfid_uid = rfid_uid.strip().upper()
    log.info("RFID scan received: %s", rfid_uid)
 
    patient = get_patient_by_rfid(rfid_uid)
    if not patient:
        log.warning("Unknown RFID UID: %s", rfid_uid)
        return None
 
    prescriptions   = get_active_prescriptions(patient["id"])
    latest_vitals   = get_latest_vitals(patient["id"])
    recent_dispenses = get_dispense_history(patient["id"], limit=5)
 
    allergies = (
        [a.strip() for a in patient["allergies"].split(",")]
        if patient["allergies"] else []
    )
 
    ctx = PatientContext(
        patient_id       = patient["id"],
        rfid_uid         = patient["rfid_uid"],
        full_name        = patient["full_name"],
        ward             = patient["ward"] or "Unknown",
        bed_number       = patient["bed_number"] or "—",
        allergies        = allergies,
        prescriptions    = prescriptions,
        latest_vitals    = latest_vitals,
        recent_dispenses = recent_dispenses,
    )
 
    log.info("Patient identified: %s (id=%d, %d prescriptions)",
             ctx.full_name, ctx.patient_id, len(ctx.prescriptions))
    return ctx
 
 
def check_dispense_safety(ctx: PatientContext, medication_name: str) -> tuple[bool, str]:
    """
    Pre-dispense safety check.
    Returns (safe: bool, reason: str).
    """
    if ctx.has_allergy_to(medication_name):
        return False, f"⛔ ALLERGY ALERT: {ctx.full_name} is allergic to {medication_name}"
 
    prescribed_names = [p["med_name"].lower() for p in ctx.prescriptions]
    if medication_name.lower() not in prescribed_names:
        return False, f"⚠️  {medication_name} is not in {ctx.full_name}'s active prescriptions"
 
    return True, "ok"
 
 
# ─── Quick test ───────────────────────────────────────────────────────────────
 
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
 
    # Should succeed (seeded in database.py test run)
    ctx = identify_patient("A1B2C3D4")
    if ctx:
        print("\n" + ctx.summary())
 
        safe, reason = check_dispense_safety(ctx, "Paracetamol 500mg")
        print(f"\nSafety check (Paracetamol): safe={safe} → {reason}")
 
        safe, reason = check_dispense_safety(ctx, "Amoxicillin 250mg")
        print(f"Safety check (Amoxicillin): safe={safe} → {reason}")
 
    # Should return None
    unknown = identify_patient("DEADBEEF")
    print(f"\nUnknown UID result: {unknown}")
 
