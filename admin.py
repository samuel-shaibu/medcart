"""
admin.py — MedCart admin CLI
Manage patients, medications and prescriptions from the terminal.

Usage:
    python3 admin.py                    # interactive menu
    python3 admin.py list-patients      # quick commands
    python3 admin.py list-meds
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "core"))

from database import (
    init_db,
    add_patient, get_patient_by_rfid, list_patients,
    add_medication, list_medications,
    add_prescription, get_active_prescriptions,
    get_dispense_history, get_latest_vitals,
)

# ─── Display helpers ──────────────────────────────────────────────────────────

def divider(title=""):
    if title:
        print(f"\n{'─'*10} {title} {'─'*10}")
    else:
        print("─" * 40)


def print_patient(p: dict):
    divider()
    print(f"  ID       : {p['id']}")
    print(f"  Name     : {p['full_name']}")
    print(f"  RFID UID : {p['rfid_uid']}")
    print(f"  DOB      : {p.get('date_of_birth') or '—'}")
    print(f"  Ward     : {p.get('ward') or '—'}")
    print(f"  Bed      : {p.get('bed_number') or '—'}")
    print(f"  Allergies: {p.get('allergies') or 'None'}")


def print_medication(m: dict):
    print(f"  [{m['id']}] {m['name']} | unit: {m['unit']} | slot: {m.get('slot_number') or '—'}")


def print_prescription(rx: dict):
    print(f"  [{rx['id']}] {rx['med_name']} — {rx['dosage']} {rx['unit']} @ {rx['frequency']}")
    print(f"       Start: {rx['start_date']} | End: {rx.get('end_date') or 'ongoing'}")
    if rx.get('notes'):
        print(f"       Notes: {rx['notes']}")


# ─── Input helpers ────────────────────────────────────────────────────────────

def prompt(label: str, default: str = "") -> str:
    val = input(f"  {label}{f' [{default}]' if default else ''}: ").strip()
    return val if val else default


def prompt_required(label: str) -> str:
    while True:
        val = input(f"  {label}: ").strip()
        if val:
            return val
        print("  This field is required.")


def confirm(question: str) -> bool:
    return input(f"  {question} (y/n): ").strip().lower() == "y"


# ─── Patient management ───────────────────────────────────────────────────────

def do_list_patients():
    patients = list_patients()
    if not patients:
        print("\n  No patients registered yet.")
        return
    divider("Registered patients")
    for p in patients:
        print_patient(p)
    print()


def do_add_patient():
    divider("Add new patient")
    print("  (Scan RFID card/tag first to get the UID from serial monitor)\n")

    rfid_uid  = prompt_required("RFID UID (e.g. E389A60D)")
    full_name = prompt_required("Full name")
    dob       = prompt("Date of birth (YYYY-MM-DD)")
    ward      = prompt("Ward (e.g. Ward B)")
    bed       = prompt("Bed number (e.g. 12)")
    allergies = prompt("Allergies (comma-separated, or leave blank)")

    # Check if UID already registered
    existing = get_patient_by_rfid(rfid_uid)
    if existing:
        print(f"\n  UID {rfid_uid} is already registered to {existing['full_name']}.")
        if not confirm("Overwrite?"):
            return

    pid = add_patient(
        rfid_uid      = rfid_uid.upper(),
        full_name     = full_name,
        date_of_birth = dob or None,
        ward          = ward or None,
        bed_number    = bed or None,
        allergies     = allergies or None,
    )
    print(f"\n  Patient '{full_name}' registered with ID {pid}.")

    if confirm("Add prescriptions now?"):
        do_add_prescription(patient_id=pid, patient_name=full_name)


def do_edit_patient():
    divider("Edit patient")
    do_list_patients()

    rfid_uid = prompt_required("Enter RFID UID of patient to edit")
    patient  = get_patient_by_rfid(rfid_uid)
    if not patient:
        print(f"\n  No patient found with UID {rfid_uid}.")
        return

    print(f"\n  Editing: {patient['full_name']} (leave blank to keep current value)")

    full_name = prompt("Full name", patient['full_name'])
    dob       = prompt("Date of birth", patient.get('date_of_birth') or "")
    ward      = prompt("Ward", patient.get('ward') or "")
    bed       = prompt("Bed number", patient.get('bed_number') or "")
    allergies = prompt("Allergies", patient.get('allergies') or "")

    from database import get_conn
    with get_conn() as conn:
        conn.execute(
            """UPDATE patients SET full_name=?, date_of_birth=?, ward=?,
               bed_number=?, allergies=?, updated_at=datetime('now')
               WHERE rfid_uid=?""",
            (full_name, dob or None, ward or None,
             bed or None, allergies or None, rfid_uid.upper())
        )
    print(f"\n  Patient '{full_name}' updated.")


def do_view_patient():
    divider("View patient details")
    rfid_uid = prompt_required("RFID UID")
    patient  = get_patient_by_rfid(rfid_uid)
    if not patient:
        print(f"\n  No patient found with UID {rfid_uid}.")
        return

    print_patient(patient)

    prescriptions = get_active_prescriptions(patient['id'])
    print(f"\n  Active prescriptions ({len(prescriptions)}):")
    if prescriptions:
        for rx in prescriptions:
            print_prescription(rx)
    else:
        print("  None")

    vitals = get_latest_vitals(patient['id'])
    if vitals:
        print(f"\n  Latest vitals ({vitals['recorded_at']}):")
        print(f"    Temp: {vitals.get('temperature_c', '—')}°C")
        print(f"    SpO2: {vitals.get('spo2_pct', '—')}%")
        print(f"    HR:   {vitals.get('heart_rate_bpm', '—')} bpm")
        print(f"    BP:   {vitals.get('bp_systolic', '—')}/{vitals.get('bp_diastolic', '—')} mmHg")

    history = get_dispense_history(patient['id'], limit=5)
    if history:
        print(f"\n  Last {len(history)} dispenses:")
        for d in history:
            print(f"    {d['dispensed_at']} — {d['med_name']} x{d['quantity']} [{d['status']}]")
    print()


# ─── Medication management ────────────────────────────────────────────────────

def do_list_meds():
    meds = list_medications()
    if not meds:
        print("\n  No medications registered yet.")
        return
    divider("Medications")
    for m in meds:
        print_medication(m)
    print()


def do_add_med():
    divider("Add medication")
    name   = prompt_required("Medication name (e.g. Paracetamol 500mg)")
    desc   = prompt("Description")
    unit   = prompt("Unit (tablet/ml/capsule)", "tablet")
    slot   = prompt("Dispenser slot number (1, 2, 3...)")

    mid = add_medication(
        name        = name,
        description = desc or None,
        unit        = unit,
        slot_number = int(slot) if slot else None
    )
    print(f"\n  Medication '{name}' added with ID {mid}.")


# ─── Prescription management ──────────────────────────────────────────────────

def do_add_prescription(patient_id: int = None, patient_name: str = ""):
    divider("Add prescription")

    if not patient_id:
        rfid_uid = prompt_required("Patient RFID UID")
        patient  = get_patient_by_rfid(rfid_uid)
        if not patient:
            print(f"\n  No patient found with UID {rfid_uid}.")
            return
        patient_id   = patient['id']
        patient_name = patient['full_name']

    print(f"\n  Adding prescription for: {patient_name}")

    meds = list_medications()
    if not meds:
        print("\n  No medications in system. Add medications first.")
        return

    print("\n  Available medications:")
    for m in meds:
        print_medication(m)

    med_id = prompt_required("\n  Medication ID")
    dosage = prompt_required("Dosage (e.g. 2)")
    unit_label = next((m['unit'] for m in meds if str(m['id']) == med_id), "unit")
    freq   = prompt_required(f"Frequency — times as HH:MM comma-separated (e.g. 08:00,20:00)")
    start  = prompt("Start date (YYYY-MM-DD)", "2026-01-01")
    end    = prompt("End date (YYYY-MM-DD, or blank for ongoing)")
    notes  = prompt("Notes")

    rxid = add_prescription(
        patient_id    = patient_id,
        medication_id = int(med_id),
        dosage        = float(dosage),
        frequency     = freq,
        start_date    = start,
        end_date      = end or None,
        notes         = notes or None
    )
    print(f"\n  Prescription added (ID {rxid}).")


def do_view_prescriptions():
    divider("View prescriptions")
    rfid_uid = prompt_required("Patient RFID UID")
    patient  = get_patient_by_rfid(rfid_uid)
    if not patient:
        print(f"\n  No patient found.")
        return

    rxs = get_active_prescriptions(patient['id'])
    print(f"\n  Active prescriptions for {patient['full_name']}:")
    if rxs:
        for rx in rxs:
            print_prescription(rx)
    else:
        print("  None")
    print()


# ─── Seed data ────────────────────────────────────────────────────────────────

def do_seed():
    divider("Seed test data")
    print("  This will add sample patients and medications.")
    if not confirm("Continue?"):
        return

    tag_uid  = prompt_required("RFID tag UID (e.g. E389A60D)")
    card_uid = prompt_required("RFID card UID (e.g. 23174610)")

    init_db()

    p1 = add_patient(
        rfid_uid      = tag_uid.upper(),
        full_name     = "Amina Yusuf",
        date_of_birth = "1985-06-20",
        ward          = "Ward B",
        bed_number    = "12",
        allergies     = "Penicillin"
    )
    p2 = add_patient(
        rfid_uid      = card_uid.upper(),
        full_name     = "Samuel Shaibu",
        date_of_birth = "2000-01-15",
        ward          = "Ward A",
        bed_number    = "5",
        allergies     = ""
    )
    m1 = add_medication("Paracetamol 500mg", unit="tablet", slot_number=1)
    m2 = add_medication("Metformin 850mg",   unit="tablet", slot_number=2)
    m3 = add_medication("Amoxicillin 250mg", unit="capsule", slot_number=3)

    add_prescription(p1, m1, dosage=2, frequency="08:00,14:00,20:00", start_date="2026-01-01")
    add_prescription(p1, m2, dosage=1, frequency="08:00,20:00",       start_date="2026-01-01")
    add_prescription(p2, m1, dosage=1, frequency="08:00,20:00",       start_date="2026-01-01")
    add_prescription(p2, m3, dosage=1, frequency="08:00",             start_date="2026-01-01")

    print("\n  Seed data added:")
    print(f"    Patient 1: Amina Yusuf  (UID: {tag_uid.upper()}) — Ward B Bed 12")
    print(f"    Patient 2: Samuel Shaibu (UID: {card_uid.upper()}) — Ward A Bed 5")
    print(f"    Medications: Paracetamol, Metformin, Amoxicillin")
    print()


# ─── Main menu ────────────────────────────────────────────────────────────────

MENU = """
  MedCart Admin
  ─────────────────────────────
  Patients:
    1. List all patients
    2. Add patient
    3. Edit patient
    4. View patient details

  Medications:
    5. List medications
    6. Add medication

  Prescriptions:
    7. Add prescription
    8. View prescriptions

  Setup:
    9. Seed test data (first time setup)

    0. Exit
  ─────────────────────────────
"""

ACTIONS = {
    "1": do_list_patients,
    "2": do_add_patient,
    "3": do_edit_patient,
    "4": do_view_patient,
    "5": do_list_meds,
    "6": do_add_med,
    "7": do_add_prescription,
    "8": do_view_prescriptions,
    "9": do_seed,
}


def main():
    init_db()

    # Quick command mode
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "list-patients": do_list_patients()
        elif cmd == "list-meds":   do_list_meds()
        elif cmd == "seed":        do_seed()
        else:
            print(f"Unknown command: {cmd}")
            print("Available: list-patients, list-meds, seed")
        return

    # Interactive menu
    while True:
        print(MENU)
        choice = input("  Choose: ").strip()
        if choice == "0":
            print("\n  Goodbye.\n")
            break
        action = ACTIONS.get(choice)
        if action:
            try:
                action()
            except KeyboardInterrupt:
                print("\n  Cancelled.")
        else:
            print("  Invalid choice.")


if __name__ == "__main__":
    main()