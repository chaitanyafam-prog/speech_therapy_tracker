import functools
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _load_project_env():
    path = os.path.join(PROJECT_ROOT, ".env")
    try:
        with open(path, encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except OSError:
        pass


_load_project_env()

from . import audio  # noqa: E402
from .db import close_db, get_db, init_db  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))  # Chennai; India has no DST
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def create_app(test_config=None):
    app = Flask(
        __name__,
        instance_path=os.path.join(PROJECT_ROOT, "instance"),
        instance_relative_config=True,
        template_folder=os.path.join(PROJECT_ROOT, "frontend", "templates"),
        static_folder=os.path.join(PROJECT_ROOT, "frontend", "static"),
    )
    os.makedirs(app.instance_path, exist_ok=True)

    database_path = os.environ.get(
        "DATABASE", os.path.join(app.instance_path, "tracker.db")
    )
    upload_dir = os.environ.get(
        "UPLOAD_DIR", os.path.join(app.instance_path, "recordings")
    )

    app.config.update(
        DATABASE=database_path,
        UPLOAD_DIR=upload_dir,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        # Doctors must know this code to register, so strangers cannot create
        # clinician accounts. Set DOCTOR_SIGNUP_CODE in the environment for real use.
        DOCTOR_SIGNUP_CODE=os.environ.get("DOCTOR_SIGNUP_CODE", "chennai-pilot"),
        SECRET_KEY=os.environ.get("SECRET_KEY") or _persistent_secret(app.instance_path),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", ""),
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DATABASE"])), exist_ok=True)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()

    register_hooks(app)
    register_routes(app)
    return app


def _persistent_secret(instance_path):
    path = os.path.join(instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


# ---------- helpers ----------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def week_start_iso():
    """Start of the current Monday-based week in IST, as a UTC ISO string."""
    today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    return monday.astimezone(timezone.utc).isoformat(timespec="seconds")


def current_user():
    if "user" not in g:
        uid = session.get("uid")
        g.user = (get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                  if uid else None)
    return g.user


def login_required(role):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login"))
            if user["role"] != role:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def new_invite_code(db):
    while True:
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(6))
        if not db.execute("SELECT 1 FROM users WHERE invite_code = ?", (code,)).fetchone():
            return code


def _doctor_by_invite(db, code):
    return db.execute("SELECT id FROM users WHERE role = 'doctor' AND invite_code = ?",
                      (code.strip().upper(),)).fetchone()


def directory_validation_errors(fields):
    """Catch incomplete or malformed entries; this does not prove a clinician's identity."""
    errors = []
    required = ("name", "specialty", "clinic", "address", "city", "place", "phone", "email", "booking_url")
    if any(not fields.get(key, "").strip() for key in required):
        errors.append("Complete every field before approving this listing.")
    if fields.get("email") and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", fields["email"].strip()):
        errors.append("Enter a valid contact email.")
    if fields.get("phone"):
        phone = fields["phone"].strip()
        digits = re.sub(r"\D", "", phone)
        if not re.fullmatch(r"[+0-9().\s-]+", phone) or not 7 <= len(digits) <= 15:
            errors.append("Enter a valid contact phone number with 7 to 15 digits.")
    if fields.get("booking_url"):
        parsed = urlparse(fields["booking_url"].strip())
        if parsed.scheme != "https" or not parsed.netloc:
            errors.append("The online booking link must be an HTTPS URL.")
    return errors


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin"))
        return view(*args, **kwargs)
    return wrapped


def can_access_recording(user, rec_patient_id, rec_doctor_id):
    return (user["role"] == "patient" and user["id"] == rec_patient_id) or \
           (user["role"] == "doctor" and user["id"] == rec_doctor_id)


def adherence(db, patient_id):
    """Active assignments with this week's recording counts."""
    return db.execute(
        """
        SELECT a.id, a.times_per_week, t.title, t.key,
               (SELECT COUNT(*) FROM recordings r
                 WHERE r.assignment_id = a.id AND r.created_at >= ?) AS done,
               (SELECT MAX(created_at) FROM recordings r WHERE r.assignment_id = a.id) AS last_at
          FROM assignments a JOIN exercise_templates t ON t.id = a.template_id
         WHERE a.patient_id = ? AND a.active = 1
         ORDER BY t.title
        """,
        (week_start_iso(), patient_id),
    ).fetchall()


# ---------- hooks ----------

def register_hooks(app):
    @app.before_request
    def csrf_protect():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST":
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(sent, session["csrf"]):
                abort(400, "Invalid or missing CSRF token.")

    @app.context_processor
    def inject():
        return {"csrf_token": session.get("csrf", ""), "user": current_user()}

    @app.template_filter("ist")
    def ist(value, fmt="%d %b %Y, %I:%M %p"):
        if not value:
            return "never"
        return datetime.fromisoformat(value).astimezone(IST).strftime(fmt)

    @app.after_request
    def no_store(resp):
        # Health data: do not let browsers/proxies cache authenticated pages or audio.
        if current_user() is not None or session.get("is_admin"):
            resp.headers["Cache-Control"] = "private, no-store"
        return resp


# ---------- routes ----------

def register_routes(app):
    @app.route("/")
    def index():
        user = current_user()
        if user is None:
            return redirect(url_for("login"))
        return redirect(url_for("doctor_home" if user["role"] == "doctor" else "patient_home"))

    @app.route("/directory")
    def clinician_directory():
        db = get_db()
        city = request.args.get("city", "").strip()
        place = request.args.get("place", "").strip()
        cities = db.execute(
            "SELECT DISTINCT city FROM clinician_directory WHERE verification_status = 'verified' ORDER BY city"
        ).fetchall()
        places = db.execute(
            "SELECT DISTINCT place FROM clinician_directory WHERE verification_status = 'verified' "
            "AND (? = '' OR lower(city) = lower(?)) ORDER BY place", (city, city)).fetchall()
        query = "SELECT * FROM clinician_directory WHERE verification_status = 'verified'"
        params = []
        if city:
            query += " AND lower(city) = lower(?)"
            params.append(city)
        if place:
            query += " AND lower(place) = lower(?)"
            params.append(place)
        query += " ORDER BY city, place, clinic, name"
        doctors = db.execute(query, params).fetchall()
        return render_template("directory.html", doctors=doctors, cities=cities, places=places,
                               selected_city=city, selected_place=place)

    @app.route("/directory/<int:entry_id>/connect", methods=["POST"])
    @login_required("patient")
    def connect_directory_doctor(entry_id):
        db = get_db()
        entry = db.execute(
            "SELECT doctor_user_id FROM clinician_directory WHERE id = ? AND verification_status = 'verified'",
            (entry_id,)).fetchone()
        if entry is None or entry["doctor_user_id"] is None:
            abort(404)
        user = current_user()
        if user["doctor_id"] is not None:
            flash("Your account is already linked to a clinician.", "error")
        else:
            db.execute("UPDATE users SET doctor_id = ? WHERE id = ?",
                       (entry["doctor_user_id"], user["id"]))
            db.commit()
            flash("You are now linked to this clinician. Their exercise plan will appear in your tracker.", "success")
        return redirect(url_for("patient_home"))

    @app.route("/admin", methods=["GET", "POST"])
    def admin():
        if session.get("is_admin"):
            return redirect(url_for("admin_directory"))
        if request.method == "POST":
            password = request.form.get("password", "")
            expected = app.config.get("ADMIN_PASSWORD", "")
            if not expected:
                flash("Admin access is disabled. Set ADMIN_PASSWORD in the server environment.", "error")
            elif hmac.compare_digest(password, expected):
                session["is_admin"] = True
                return redirect(url_for("admin_directory"))
            else:
                flash("That admin password is not valid.", "error")
        return render_template("admin_login.html", configured=bool(app.config.get("ADMIN_PASSWORD")))

    @app.route("/admin/logout", methods=["POST"])
    @admin_required
    def admin_logout():
        session.pop("is_admin", None)
        return redirect(url_for("admin"))

    @app.route("/admin/doctors", methods=["GET", "POST"])
    @admin_required
    def admin_directory():
        db = get_db()
        form_rows = [{}, {}]
        if request.method == "POST":
            field_names = (
                "name", "specialty", "clinic", "address", "city", "place",
                "phone", "email", "booking_url", "doctor_user_id",
            )
            values = {
                key: [value.strip() for value in request.form.getlist(key)]
                for key in field_names
            }
            row_count = max((len(items) for items in values.values()), default=0)
            form_rows = [
                {
                    key: values[key][index] if index < len(values[key]) else ""
                    for key in field_names
                }
                for index in range(min(row_count, 50))
            ]
            errors = []
            if row_count == 0:
                errors.append("Enter at least one clinician listing.")
            elif row_count > 50:
                errors.append("Add no more than 50 clinician listings at a time.")
            elif any(len(items) != row_count for items in values.values()):
                errors.append("Complete every field in each clinician listing.")
            else:
                listings = []
                for index in range(row_count):
                    fields = {key: values[key][index] for key in field_names[:-1]}
                    doctor_user_id = values["doctor_user_id"][index] or None
                    row_errors = directory_validation_errors(fields)
                    if doctor_user_id and not db.execute(
                            "SELECT 1 FROM users WHERE id = ? AND role = 'doctor'",
                            (doctor_user_id,)).fetchone():
                        row_errors.append(
                            "Select a registered clinician account or leave that field blank."
                        )
                    errors.extend(
                        f"Listing {index + 1}: {error}" for error in row_errors
                    )
                    listings.append((doctor_user_id, fields))

                if not errors:
                    created_at = now_iso()
                    db.executemany(
                        "INSERT INTO clinician_directory "
                        "(doctor_user_id, name, specialty, clinic, address, city, place, phone, email, booking_url, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                doctor_user_id, fields["name"], fields["specialty"],
                                fields["clinic"], fields["address"], fields["city"],
                                fields["place"], fields["phone"], fields["email"],
                                fields["booking_url"], created_at,
                            )
                            for doctor_user_id, fields in listings
                        ],
                    )
                    db.commit()
                    flash(
                        f"{row_count} listing(s) added as pending. Review the details "
                        "and approve each one to show it in the public directory.",
                        "success",
                    )
                    return redirect(url_for("admin_directory"))

            for error in errors:
                flash(error, "error")
        doctors = db.execute("SELECT id, name, email FROM users WHERE role = 'doctor' ORDER BY name").fetchall()
        entries = db.execute("SELECT * FROM clinician_directory ORDER BY verification_status, city, place, name").fetchall()
        return render_template("admin_directory.html", entries=entries, doctor_accounts=doctors,
                               validation_errors=directory_validation_errors,
                               form_rows=form_rows)

    @app.route("/admin/doctors/<int:entry_id>/verify", methods=["POST"])
    @admin_required
    def verify_directory_doctor(entry_id):
        db = get_db()
        entry = db.execute("SELECT * FROM clinician_directory WHERE id = ?", (entry_id,)).fetchone()
        if entry is None:
            abort(404)
        errors = directory_validation_errors(dict(entry))
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            db.execute("UPDATE clinician_directory SET verification_status = 'verified', verified_on = ? WHERE id = ?",
                       (now_iso(), entry_id))
            db.commit()
            flash("Listing approved and published.", "success")
        return redirect(url_for("admin_directory"))

    @app.route("/admin/doctors/<int:entry_id>/remove", methods=["POST"])
    @admin_required
    def remove_directory_doctor(entry_id):
        db = get_db()
        db.execute("DELETE FROM clinician_directory WHERE id = ?", (entry_id,))
        db.commit()
        flash("Directory listing removed.", "success")
        return redirect(url_for("admin_directory"))

    # ----- auth -----

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "GET":
            return render_template("register.html")

        role = request.form.get("role")
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        code = request.form.get("code", "").strip()

        error = None
        if role not in ("doctor", "patient"):
            error = "Choose whether you are a doctor or a patient."
        elif not name or "@" not in email:
            error = "Enter your name and a valid email address."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."

        db = get_db()
        doctor_id = invite = None
        if error is None and role == "doctor":
            if not hmac.compare_digest(code, app.config["DOCTOR_SIGNUP_CODE"]):
                error = "That doctor sign-up code is not valid."
            else:
                invite = new_invite_code(db)
        elif error is None and code:
            # Patients may register without a code and link to a clinician later.
            doctor = _doctor_by_invite(db, code)
            if doctor is None:
                error = "That doctor invite code was not found. Ask your clinic for it."
            else:
                doctor_id = doctor["id"]

        if error is None:
            try:
                cur = db.execute(
                    "INSERT INTO users (role, name, email, password_hash, invite_code, "
                    "doctor_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (role, name, email, generate_password_hash(password), invite,
                     doctor_id, now_iso()))
                db.commit()
            except sqlite3.IntegrityError:
                error = "An account with that email already exists."
            else:
                session.clear()
                session["uid"] = cur.lastrowid
                return redirect(url_for("index"))

        flash(error, "error")
        return render_template("register.html", form=request.form), 400

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            row = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
                session.clear()
                session["uid"] = row["id"]
                return redirect(url_for("index"))
            flash("Incorrect email or password.", "error")
            return render_template("login.html"), 401
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ----- doctor -----

    @app.route("/doctor")
    @login_required("doctor")
    def doctor_home():
        db = get_db()
        patients = db.execute(
            "SELECT id, name, email FROM users WHERE doctor_id = ? ORDER BY name",
            (current_user()["id"],)).fetchall()
        rows = []
        for p in patients:
            plan = adherence(db, p["id"])
            rows.append({
                "patient": p,
                "done": sum(min(a["done"], a["times_per_week"]) for a in plan),
                "target": sum(a["times_per_week"] for a in plan),
                "last_at": max((a["last_at"] for a in plan if a["last_at"]), default=None),
            })
        return render_template("doctor_home.html", rows=rows)

    @app.route("/doctor/patients/<int:patient_id>")
    @login_required("doctor")
    def doctor_patient(patient_id):
        db = get_db()
        patient = db.execute(
            "SELECT id, name, email FROM users WHERE id = ? AND doctor_id = ?",
            (patient_id, current_user()["id"])).fetchone()
        if patient is None:
            abort(404)
        recordings = db.execute(
            """SELECT r.id, r.created_at, r.duration, t.title
                 FROM recordings r JOIN assignments a ON a.id = r.assignment_id
                 JOIN exercise_templates t ON t.id = a.template_id
                WHERE r.patient_id = ? ORDER BY r.created_at DESC LIMIT 100""",
            (patient_id,)).fetchall()
        templates = db.execute("SELECT id, title FROM exercise_templates ORDER BY id").fetchall()
        return render_template("doctor_patient.html", patient=patient, plan=adherence(db, patient_id),
                               recordings=recordings, templates=templates)

    @app.route("/doctor/patients/<int:patient_id>/assign", methods=["POST"])
    @login_required("doctor")
    def doctor_assign(patient_id):
        db = get_db()
        doctor = current_user()
        if not db.execute("SELECT 1 FROM users WHERE id = ? AND doctor_id = ?",
                          (patient_id, doctor["id"])).fetchone():
            abort(404)
        try:
            template_id = int(request.form["template_id"])
            times = int(request.form["times_per_week"])
        except (KeyError, ValueError):
            abort(400)
        if not 1 <= times <= 21 or not db.execute(
                "SELECT 1 FROM exercise_templates WHERE id = ?", (template_id,)).fetchone():
            abort(400)
        existing = db.execute(
            "SELECT id FROM assignments WHERE patient_id = ? AND template_id = ? AND active = 1",
            (patient_id, template_id)).fetchone()
        if existing:
            db.execute("UPDATE assignments SET times_per_week = ? WHERE id = ?",
                       (times, existing["id"]))
        else:
            db.execute(
                "INSERT INTO assignments (patient_id, doctor_id, template_id, times_per_week, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (patient_id, doctor["id"], template_id, times, now_iso()))
        db.commit()
        return redirect(url_for("doctor_patient", patient_id=patient_id))

    @app.route("/doctor/assignments/<int:assignment_id>/remove", methods=["POST"])
    @login_required("doctor")
    def doctor_remove_assignment(assignment_id):
        db = get_db()
        row = db.execute("SELECT patient_id FROM assignments WHERE id = ? AND doctor_id = ?",
                         (assignment_id, current_user()["id"])).fetchone()
        if row is None:
            abort(404)
        db.execute("UPDATE assignments SET active = 0 WHERE id = ?", (assignment_id,))
        db.commit()
        return redirect(url_for("doctor_patient", patient_id=row["patient_id"]))

    @app.route("/doctor/recordings/<int:recording_id>")
    @login_required("doctor")
    def doctor_recording(recording_id):
        rec = get_db().execute(
            """SELECT r.id, r.created_at, r.duration, t.title, t.prompt_text, u.name AS patient_name,
                      u.id AS patient_id
                 FROM recordings r JOIN assignments a ON a.id = r.assignment_id
                 JOIN exercise_templates t ON t.id = a.template_id
                 JOIN users u ON u.id = r.patient_id
                WHERE r.id = ? AND a.doctor_id = ?""",
            (recording_id, current_user()["id"])).fetchone()
        if rec is None:
            abort(404)
        return render_template("doctor_recording.html", rec=rec)

    # ----- patient -----

    @app.route("/patient")
    @login_required("patient")
    def patient_home():
        db = get_db()
        user = current_user()
        doctor = (db.execute("SELECT name FROM users WHERE id = ?", (user["doctor_id"],)).fetchone()
                  if user["doctor_id"] else None)
        return render_template("patient_home.html", doctor=doctor, plan=adherence(db, user["id"]))

    @app.route("/patient/link", methods=["POST"])
    @login_required("patient")
    def patient_link():
        db = get_db()
        user = current_user()
        doctor = _doctor_by_invite(db, request.form.get("code", ""))
        if doctor is None:
            flash("That doctor invite code was not found. Ask your clinic for it.", "error")
        elif user["doctor_id"] is not None:
            flash("You are already linked to a clinician.", "error")
        else:
            db.execute("UPDATE users SET doctor_id = ? WHERE id = ?", (doctor["id"], user["id"]))
            db.commit()
        return redirect(url_for("patient_home"))

    @app.route("/patient/record/<int:assignment_id>")
    @login_required("patient")
    def patient_record(assignment_id):
        row = get_db().execute(
            """SELECT a.id, t.title, t.instructions, t.prompt_text, t.min_seconds, t.max_seconds
                 FROM assignments a JOIN exercise_templates t ON t.id = a.template_id
                WHERE a.id = ? AND a.patient_id = ? AND a.active = 1""",
            (assignment_id, current_user()["id"])).fetchone()
        if row is None:
            abort(404)
        return render_template("patient_record.html", ex=row)

    @app.route("/api/recordings", methods=["POST"])
    @login_required("patient")
    def upload_recording():
        db = get_db()
        user = current_user()
        file = request.files.get("file")
        try:
            assignment_id = int(request.form.get("assignment_id", ""))
        except ValueError:
            return jsonify(error="Missing exercise."), 400
        row = db.execute(
            """SELECT a.id, t.min_seconds, t.max_seconds
                 FROM assignments a JOIN exercise_templates t ON t.id = a.template_id
                WHERE a.id = ? AND a.patient_id = ? AND a.active = 1""",
            (assignment_id, user["id"])).fetchone()
        if row is None or file is None:
            return jsonify(error="Exercise not found."), 404

        # The browser sends 16 kHz mono WAV; the server never trusts the filename.
        token = secrets.token_hex(8)
        base = os.path.join(app.config["UPLOAD_DIR"], f"{user['id']}_{token}")
        original, cleaned = base + "_original.wav", base + "_clean.wav"
        file.save(original)
        try:
            duration = audio.process_upload(original, cleaned, row["min_seconds"], row["max_seconds"])
        except audio.AudioError as exc:
            _remove(original, cleaned)
            return jsonify(error=str(exc)), 422
        except Exception:
            app.logger.exception("audio processing failed")
            _remove(original, cleaned)
            return jsonify(error="We could not process that recording. Please try again."), 500

        cur = db.execute(
            "INSERT INTO recordings (assignment_id, patient_id, original_path, clean_path, "
            "duration, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (assignment_id, user["id"], original, cleaned, duration, now_iso()))
        db.commit()
        return jsonify(id=cur.lastrowid, duration=round(duration, 1))

    # ----- media -----

    @app.route("/media/<int:recording_id>/<version>.wav")
    def media(recording_id, version):
        user = current_user()
        if user is None:
            abort(401)
        if version not in ("original", "clean"):
            abort(404)
        rec = get_db().execute(
            """SELECT r.original_path, r.clean_path, r.patient_id, a.doctor_id
                 FROM recordings r JOIN assignments a ON a.id = r.assignment_id WHERE r.id = ?""",
            (recording_id,)).fetchone()
        if rec is None or not can_access_recording(user, rec["patient_id"], rec["doctor_id"]):
            abort(404)
        path = rec["original_path" if version == "original" else "clean_path"]
        return send_file(path, mimetype="audio/wav", conditional=True)


def _remove(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


app = create_app()

if __name__ == "__main__":
    # Reachable from a phone on the same Wi-Fi. Never enable debug=True with host 0.0.0.0:
    # the Werkzeug debugger allows remote code execution.
    app.run(host="0.0.0.0", port=5001)
