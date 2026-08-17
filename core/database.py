"""
database.py - MedCart SQLite layer
Tables: patients, vitals_log, dispense_log
Simplified: each patient has a dedicated tray slot.
Prescriptions kept for records but don't gate dispensing.
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data" / "medcart.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("database")


# ─── Connection helper ────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Schema ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rfid_uid        TEXT    NOT NULL UNIQUE,
    full_name       TEXT    NOT NULL,
    date_of_birth   TEXT,
    ward            TEXT,
    bed_number      TEXT,
    slot_number     INTEGER UNIQUE,            -- physical tray slot on the cart
    allergies       TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vitals_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    temperature_c   REAL,
    humidity_pct    REAL,
    spo2_pct        REAL,
    heart_rate_bpm  INTEGER,
    bp_systolic     INTEGER,
    bp_diastolic    INTEGER,
    recorded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dispense_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    slot_number     INTEGER NOT NULL,
    dispensed_by    TEXT    NOT NULL DEFAULT 'auto',
    dispensed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    status          TEXT    NOT NULL DEFAULT 'success',
    bed_confirmed   TEXT,                      -- bed number confirmed by CV at time of dispense
    notes           TEXT
);
"""


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
    log.info("Database initialized at %s", DB_PATH)


# ─── Patient CRUD ─────────────────────────────────────────────────────────────

def add_patient(rfid_uid: str, full_name: str, date_of_birth: str = None,
                ward: str = None, bed_number: str = None,
                slot_number: int = None, allergies: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO patients
               (rfid_uid, full_name, date_of_birth, ward, bed_number, slot_number, allergies)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (rfid_uid.upper(), full_name, date_of_birth, ward,
             bed_number, slot_number, allergies)
        )
        log.info("Added patient '%s' (RFID: %s, Slot: %s)", full_name, rfid_uid, slot_number)
        return cur.lastrowid


def get_patient_by_rfid(rfid_uid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE rfid_uid = ?", (rfid_uid.upper(),)
        ).fetchone()
        return dict(row) if row else None


def get_patient_by_id(patient_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        return dict(row) if row else None


def list_patients() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM patients ORDER BY slot_number"
        ).fetchall()
        return [dict(r) for r in rows]


def update_patient(rfid_uid: str, full_name: str = None, date_of_birth: str = None,
                   ward: str = None, bed_number: str = None,
                   slot_number: int = None, allergies: str = None):
    with get_conn() as conn:
        conn.execute(
            """UPDATE patients SET
               full_name=COALESCE(?,full_name),
               date_of_birth=COALESCE(?,date_of_birth),
               ward=COALESCE(?,ward),
               bed_number=COALESCE(?,bed_number),
               slot_number=COALESCE(?,slot_number),
               allergies=COALESCE(?,allergies),
               updated_at=datetime('now')
               WHERE rfid_uid=?""",
            (full_name, date_of_birth, ward, bed_number,
             slot_number, allergies, rfid_uid.upper())
        )
        log.info("Updated patient RFID: %s", rfid_uid)


# ─── Vitals logging ───────────────────────────────────────────────────────────

def log_vitals(patient_id: int, temperature_c: float = None,
               humidity_pct: float = None, spo2_pct: float = None,
               heart_rate_bpm: int = None, bp_systolic: int = None,
               bp_diastolic: int = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO vitals_log
               (patient_id, temperature_c, humidity_pct, spo2_pct,
                heart_rate_bpm, bp_systolic, bp_diastolic)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (patient_id, temperature_c, humidity_pct, spo2_pct,
             heart_rate_bpm, bp_systolic, bp_diastolic)
        )
        log.info("Vitals logged for patient_id=%d", patient_id)
        return cur.lastrowid


def get_latest_vitals(patient_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM vitals_log WHERE patient_id = ?
               ORDER BY recorded_at DESC LIMIT 1""",
            (patient_id,)
        ).fetchone()
        return dict(row) if row else None


# ─── Dispense logging ─────────────────────────────────────────────────────────

def log_dispense(patient_id: int, slot_number: int,
                 dispensed_by: str = "auto", status: str = "success",
                 bed_confirmed: str = None, notes: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO dispense_log
               (patient_id, slot_number, dispensed_by, status, bed_confirmed, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (patient_id, slot_number, dispensed_by, status, bed_confirmed, notes)
        )
        log.info("Dispense logged: patient=%d slot=%d status=%s bed=%s",
                 patient_id, slot_number, status, bed_confirmed)
        return cur.lastrowid


def get_dispense_history(patient_id: int, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM dispense_log WHERE patient_id = ?
               ORDER BY dispensed_at DESC LIMIT ?""",
            (patient_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


# ─── Quick test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    p1 = add_patient(
        rfid_uid="E389A60D", full_name="Amina Yusuf",
        date_of_birth="1985-06-20", ward="Ward B",
        bed_number="12", slot_number=1, allergies="Penicillin"
    )
    p2 = add_patient(
        rfid_uid="23174610", full_name="Samuel Shaibu",
        date_of_birth="2000-01-15", ward="Ward A",
        bed_number="5", slot_number=2, allergies=""
    )

    log_vitals(p1, temperature_c=36.8, humidity_pct=60.0)
    log_dispense(p1, slot_number=1, bed_confirmed="12")

    print("DB ready at:", DB_PATH)
    for p in list_patients():
        print(f"  {p['full_name']} — Bed {p['bed_number']} — Slot {p['slot_number']}")