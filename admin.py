"""
admin.py — MedCart admin CLI
Manage patients and vitals from the terminal.

Usage:
    python3 admin.py                 # interactive menu
    python3 admin.py list-patients   # quick commands
    python3 admin.py seed            # seed test data
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "core"))

from database import (
    init_db, get_conn,
    add_patient, get_patient_by_rfid, list_patients,
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
    print(f"  Slot     : {p.get('slot_number') or '—'}")
    print(f"  Allergies: {p.get('allergies') or 'None'}")


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
    print("  Scan RFID card/tag first to get UID from serial monitor.\n")

    rfid_uid  = prompt_required("RFID UID (e.g. E389A60D)")
    full_name = prompt_required("Full name")
    dob       = prompt("Date of birth (YYYY-MM-DD)")
    ward      = prompt("Ward (e.g. Ward B)")
    bed       = prompt("Bed number (e.g. 12)")
    slot      = prompt("Tray slot number (1, 2, 3...)")
    allergies = prompt("Allergies (comma-separated, or blank)")

    existing = get_patient_by_rfid(rfid_uid)
    if existing:
        print(f"\n  UID {rfid_uid} already registered to {existing['full_name']}.")
        if not confirm("Overwrite?"):
            return

    pid = add_patient(
        rfid_uid      = rfid_uid.upper(),
        full_name     = full_name,
        date_of_birth = dob or None,
        ward          = ward or None,
        bed_number    = bed or None,
        slot_number   = int(slot) if slot else None,
        allergies     = allergies or None,
    )
    print(f"\n  Patient '{full_name}' registered (ID {pid}, Slot {slot or '—'}).")


def do_edit_patient():
    divider("Edit patient")
    do_list_patients()

    rfid_uid = prompt_required("RFID UID of patient to edit")
    patient  = get_patient_by_rfid(rfid_uid)
    if not patient:
        print(f"\n  No patient found with UID {rfid_uid}.")
        return

    print(f"\n  Editing: {patient['full_name']} (leave blank to keep current value)")

    full_name = prompt("Full name", patient['full_name'])
    dob       = prompt("Date of birth", patient.get('date_of_birth') or "")
    ward      = prompt("Ward", patient.get('ward') or "")
    bed       = prompt("Bed number", patient.get('bed_number') or "")
    slot      = prompt("Tray slot number", str(patient.get('slot_number') or ""))
    allergies = prompt("Allergies", patient.get('allergies') or "")

    with get_conn() as conn:
        conn.execute(
            """UPDATE patients SET
               full_name=?, date_of_birth=?, ward=?, bed_number=?,
               slot_number=?, allergies=?, updated_at=datetime('now')
               WHERE rfid_uid=?""",
            (full_name, dob or None, ward or None, bed or None,
             int(slot) if slot else None, allergies or None, rfid_uid.upper())
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

    vitals = get_latest_vitals(patient['id'])
    if vitals:
        print(f"\n  Latest vitals ({vitals['recorded_at']}):")
        print(f"    Temp:     {vitals.get('temperature_c', '—')}°C")
        print(f"    Humidity: {vitals.get('humidity_pct', '—')}%")
        print(f"    SpO2:     {vitals.get('spo2_pct', '—')}%")
        print(f"    HR:       {vitals.get('heart_rate_bpm', '—')} bpm")
    else:
        print("\n  No vitals recorded yet.")

    history = get_dispense_history(patient['id'], limit=5)
    if history:
        print(f"\n  Last {len(history)} dispenses:")
        for d in history:
            print(f"    {d['dispensed_at']} — Slot {d['slot_number']} "
                  f"[{d['status']}] bed={d.get('bed_confirmed','—')}")
    print()


# ─── Seed data ────────────────────────────────────────────────────────────────

def do_seed():
    divider("Seed test data")
    print("  Adds 2 test patients with tray slots assigned.")
    if not confirm("Continue?"):
        return

    tag_uid  = prompt_required("RFID tag UID (e.g. E389A60D)")
    card_uid = prompt_required("RFID card UID (e.g. 23174610)")

    init_db()

    p1 = add_patient(
        rfid_uid="E389A60D" if not tag_uid else tag_uid.upper(),
        full_name="Amina Yusuf",
        date_of_birth="1985-06-20",
        ward="Ward B", bed_number="12",
        slot_number=1, allergies="Penicillin"
    )
    p2 = add_patient(
        rfid_uid="23174610" if not card_uid else card_uid.upper(),
        full_name="Samuel Shaibu",
        date_of_birth="2000-01-15",
        ward="Ward A", bed_number="5",
        slot_number=2, allergies=""
    )

    print(f"\n  Seed data added:")
    print(f"    Amina Yusuf  — UID: {tag_uid.upper()}  — Bed 12 — Slot 1")
    print(f"    Samuel Shaibu — UID: {card_uid.upper()} — Bed 5  — Slot 2")
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

  Setup:
    5. Seed test data (first time setup)

    0. Exit
  ─────────────────────────────
"""

ACTIONS = {
    "1": do_list_patients,
    "2": do_add_patient,
    "3": do_edit_patient,
    "4": do_view_patient,
    "5": do_seed,
}


def main():
    init_db()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "list-patients": do_list_patients()
        elif cmd == "seed":        do_seed()
        else:
            print(f"Unknown command: {cmd}")
            print("Available: list-patients, seed")
        return

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