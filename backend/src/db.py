import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    role          TEXT NOT NULL CHECK (role IN ('doctor', 'patient')),
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    invite_code   TEXT UNIQUE,                       -- doctors only
    doctor_id     INTEGER REFERENCES users(id),      -- patients only
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercise_templates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    instructions TEXT NOT NULL,
    prompt_text  TEXT,                               -- text the patient reads aloud, if any
    min_seconds  INTEGER NOT NULL,
    max_seconds  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id        INTEGER NOT NULL REFERENCES users(id),
    doctor_id         INTEGER NOT NULL REFERENCES users(id),
    template_id       INTEGER NOT NULL REFERENCES exercise_templates(id),
    times_per_week    INTEGER NOT NULL CHECK (times_per_week BETWEEN 1 AND 21),
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    patient_id    INTEGER NOT NULL REFERENCES users(id),
    original_path TEXT NOT NULL,
    clean_path    TEXT NOT NULL,
    duration      REAL NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recordings_patient ON recordings(patient_id, created_at);
CREATE INDEX IF NOT EXISTS idx_assignments_patient ON assignments(patient_id, active);

CREATE TABLE IF NOT EXISTS clinician_directory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    specialty TEXT NOT NULL,
    clinic TEXT NOT NULL,
    address TEXT NOT NULL,
    city TEXT NOT NULL,
    place TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT NOT NULL,
    booking_url TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (verification_status IN ('pending', 'verified')),
    verified_on TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_directory_city_place
    ON clinician_directory(city, place, verification_status);

"""

EXERCISES = [
    (
        "sustained_ahh",
        "Sustained “ahh”",
        "Take a deep breath, then say “ahh” in a steady, comfortable voice for "
        "as long as you can. Keep the loudness and pitch even.",
        None,
        2,
        30,
    ),
    (
        "pataka",
        "Repeat “pa-ta-ka”",
        "Take a deep breath, then repeat “pa-ta-ka, pa-ta-ka, pa-ta-ka…” as "
        "quickly and clearly as you can until you run out of breath.",
        "pa-ta-ka pa-ta-ka pa-ta-ka",
        3,
        20,
    ),
    (
        "reading_sentence",
        "Read a sentence",
        "Read the sentence below aloud at your normal speaking pace and loudness.",
        "When the sunlight strikes raindrops in the air, they act as a prism and form a rainbow.",
        3,
        30,
    ),
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT OR IGNORE INTO exercise_templates "
        "(key, title, instructions, prompt_text, min_seconds, max_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        EXERCISES,
    )
    db.commit()
