"""
database.py - MedCart SQLite layer
Tables: patients, medications, prescriptions, vitals_log, dispense_log
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data" / "medcart.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("database")


# _______ Connectino helper ______________________________
@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


# _______ Schema _________________________________________

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rfid_uid        TEXT    NOT NULL UNIQUE,   -- hex UID from RC522
    full_name       TEXT    NOT NULL,
    date_of_birth   TEXT,                      -- ISO 8601 e.g. 1990-04-15
    ward            TEXT,
    bed_number      TEXT,
    allergies       TEXT,                      -- comma-separated
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
 
CREATE TABLE IF NOT EXISTS medications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    description     TEXT,
    unit            TEXT    NOT NULL DEFAULT 'tablet',  -- tablet, ml, capsule …
    slot_number     INTEGER UNIQUE                      -- physical dispenser slot 1-N
);
 
CREATE TABLE IF NOT EXISTS prescriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    medication_id   INTEGER NOT NULL REFERENCES medications(id),
    dosage          REAL    NOT NULL,          -- quantity per dose
    frequency       TEXT    NOT NULL,          -- e.g. "08:00,14:00,20:00"
    start_date      TEXT    NOT NULL,
    end_date        TEXT,
    notes           TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
 
CREATE TABLE IF NOT EXISTS vitals_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    temperature_c   REAL,
    spo2_pct        REAL,
    heart_rate_bpm  INTEGER,
    bp_systolic     INTEGER,
    bp_diastolic    INTEGER,
    recorded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
 
CREATE TABLE IF NOT EXISTS dispense_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    medication_id   INTEGER NOT NULL REFERENCES medications(id),
    quantity        REAL    NOT NULL,
    dispensed_by    TEXT    NOT NULL DEFAULT 'auto',   -- 'auto' | 'nurse:<id>'
    dispensed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    status          TEXT    NOT NULL DEFAULT 'success' -- success | failed | skipped
);
"""

def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
    log.info(f"Database initialized at %s", DB_PATH)
        

# ________ Patient CRUD ______________________________________
def add_patient(rfid_uid: str, full_name: str, date_of_birth: str = None,
                ward: str = None, bed_number: str = None, allergies: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO patients (rfid_uid, full_name, date_of_birth, ward, bed_number, allergies)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rfid_uid.upper(), full_name, date_of_birth, ward, bed_number, allergies)
        )
        log.info("Added patient '%s' (RFID: %s)", full_name, rfid_uid)
        return cur.lastrowid

def get_patient_by_rfid(rfid_uid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE rfid_uid = ?", (rfid_uid.upper(),)
        ).fetchone()
        return dict(row) if row else None
        

def get_patient_by_id(patient_id:int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        return dict(row) if row else None
        
def list_patients() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM patients").fetchall()
        return [dict(r) for r in rows]
    

# _______ Medication CRUD __________________________________
def add_medication(name: str, description: str = None,
                   unit: str = "tablet", slot_number: int = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO medications (name, description, unit, slot_number) VALUES (?, ?, ?, ?)",
            (name, description, unit, slot_number)
        )
        log.info("Added medication '%s' in slot %s", name, slot_number)
        return cur.lastrowid
 
 
def list_medications() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM medications ORDER BY slot_number").fetchall()
        return [dict(r) for r in rows]


# ________ Prescription helpers ___________________________________
def add_prescription(patient_id: int, medication_id: int, dosage: float,
                     frequency: str, start_date: str, end_date: str = None,
                     notes: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO prescriptions
               (patient_id, medication_id, dosage, frequency, start_date, end_date, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (patient_id, medication_id, dosage, frequency, start_date, end_date, notes)
        )
        return cur.lastrowid
 

def get_active_prescriptions(patient_id: int) -> list[dict]:
    """Return active prescriptions with medication details joined."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.*, m.name AS med_name, m.unit, m.slot_number
               FROM prescriptions p
               JOIN medications m ON p.medication_id = m.id
               WHERE p.patient_id = ? AND p.active = 1
               ORDER BY m.slot_number""",
            (patient_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ________ Vitals logging ______________________________________
def log_vitals(patient_id: int, temperature_c: float = None, spo2_pct: float = None,
               heart_rate_bpm: int = None, bp_systolic: int = None,
               bp_diastolic: int = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO vitals_log
               (patient_id, temperature_c, spo2_pct, heart_rate_bpm, bp_systolic, bp_diastolic)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (patient_id, temperature_c, spo2_pct, heart_rate_bpm, bp_systolic, bp_diastolic)
        )
        log.info("Vitals logged for patient_id=%d", patient_id)
        return cur.lastrowid

def get_latest_vitals(patient_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM vitals_log WHERE patient_id =?
            ORDER BY recorded_at DESC LIMIT 1""",
            (patient_id,)
        ).fetchone()
        return dict(row) if row else None
    

# ________ Dispense logging ______________________________________
def log_dispense(patient_id: int, medication_id: int, quantity: float, dispensed_by: str = "auto", status: str = "success") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO dispense_log
            (patient_id, medication_id, quantity, dispensed_by, status)
            VALUES (?, ?, ?, ?, ?)""",
            (patient_id, medication_id, quantity, dispensed_by, status)
        )
        log.info("Dispense logged for patient=%d med=%d qty=%s status=%s", patient_id, medication_id, quantity, status)
        return cur.lastrowid
    
 
def get_dispense_history(patient_id: int, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT d.*, m.name AS med_name
               FROM dispense_log d
               JOIN medications m ON d.medication_id = m.id
               WHERE d.patient_id = ?
               ORDER BY d.dispensed_at DESC LIMIT ?""",
            (patient_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

# ─── Entrypoint (quick test) ──────────────────────────────────────────────────
 
if __name__ == "__main__":
    init_db()
 
    # Seed a test patient
    pid = add_patient(
        rfid_uid="A1B2C3D4",
        full_name="Amina Yusuf",
        date_of_birth="1985-06-20",
        ward="Ward B",
        bed_number="12",
        allergies="Penicillin"
    )
 
    # Seed medications
    mid1 = add_medication("Paracetamol 500mg", unit="tablet", slot_number=1)
    mid2 = add_medication("Metformin 850mg", unit="tablet", slot_number=2)
 
    # Prescribe
    add_prescription(pid, mid1, dosage=2, frequency="08:00,14:00,20:00",
                     start_date="2026-06-01")
    add_prescription(pid, mid2, dosage=1, frequency="08:00,20:00",
                     start_date="2026-06-01")
 
    # Simulate RFID scan
    patient = get_patient_by_rfid("A1B2C3D4")
    print("\n=== Patient identified ===")
    for k, v in patient.items():
        print(f"  {k}: {v}")
 
    prescriptions = get_active_prescriptions(pid)
    print(f"\n=== Active prescriptions ({len(prescriptions)}) ===")
    for rx in prescriptions:
        print(f"  {rx['med_name']} — {rx['dosage']} {rx['unit']} @ {rx['frequency']}")
 
    # Log vitals
    log_vitals(pid, temperature_c=36.8, spo2_pct=98.0, heart_rate_bpm=72)
 
    # Log a dispense
    log_dispense(pid, mid1, quantity=2)
 
    print("\nAll done. DB at:", DB_PATH)
