import io
import os
import tempfile
import unittest
from unittest import mock

import numpy as np
import pyloudnorm as pyln
import soundfile as sf

from backend.src.app import create_app

SR = 16000


def wav_bytes(y, sr=SR):
    buf = io.BytesIO()
    sf.write(buf, y, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return buf


def body(resp):
    """Read a response fully and release its file handle (matters on Windows)."""
    data = resp.get_data()
    resp.close()
    return data


def noisy_voice(seconds, amp=0.05, noise=0.01, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    voice = amp * np.sin(2 * np.pi * 150 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    hum = 0.01 * np.sin(2 * np.pi * 50 * t)
    return (voice + hum + noise * rng.standard_normal(len(t))).astype("float32")


class Client:
    """Test client that carries the CSRF token like a browser would."""

    def __init__(self, app):
        self.c = app.test_client()
        self.c.get("/login")

    def token(self):
        with self.c.session_transaction() as s:
            return s["csrf"]

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data["csrf_token"] = self.token()
        return self.c.post(url, data=data, **kw)

    def get(self, url, **kw):
        return self.c.get(url, **kw)


class FlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.app = create_app({
            "TESTING": True,
            "DATABASE": os.path.join(cls.tmp.name, "t.db"),
            "UPLOAD_DIR": os.path.join(cls.tmp.name, "rec"),
            "DOCTOR_SIGNUP_CODE": "pilot-code",
        })
        os.makedirs(cls.app.config["UPLOAD_DIR"], exist_ok=True)
        with cls.app.app_context():
            from backend.src.db import init_db
            init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def register(self, **fields):
        c = Client(self.app)
        r = c.post("/register", fields)
        return c, r

    def test_full_flow(self):
        # --- registration rules ---
        _, r = self.register(role="doctor", name="Dr A", email="a@x.in",
                             password="longenough", code="wrong")
        self.assertEqual(r.status_code, 400)

        doc, r = self.register(role="doctor", name="Dr A", email="a@x.in",
                               password="longenough", code="pilot-code")
        self.assertEqual(r.status_code, 302)
        home = doc.get("/doctor").get_data(as_text=True)
        import re
        invite = re.search(r'class="code">([A-Z2-9]{6})<', home).group(1)

        _, r = self.register(role="patient", name="P", email="p@x.in",
                             password="longenough", code="NOPE12")
        self.assertEqual(r.status_code, 400)

        pat, r = self.register(role="patient", name="Pat", email="p@x.in",
                               password="longenough", code=invite.lower())
        self.assertEqual(r.status_code, 302)

        # CSRF is enforced
        self.assertEqual(Client(self.app).c.post("/login", data={"email": "a@x.in"}).status_code, 400)

        # --- doctor assigns pa-ta-ka 3x/week ---
        with self.app.app_context():
            from backend.src.db import get_db
            db = get_db()
            patient_id = db.execute("SELECT id FROM users WHERE email='p@x.in'").fetchone()["id"]
            tpl = db.execute("SELECT id FROM exercise_templates WHERE key='pataka'").fetchone()["id"]
        r = doc.post(f"/doctor/patients/{patient_id}/assign",
                     {"template_id": tpl, "times_per_week": 3})
        self.assertEqual(r.status_code, 302)
        self.assertIn("pa-ta-ka", pat.get("/patient").get_data(as_text=True))

        with self.app.app_context():
            from backend.src.db import get_db
            aid = get_db().execute("SELECT id FROM assignments").fetchone()["id"]
        self.assertEqual(pat.get(f"/patient/record/{aid}").status_code, 200)

        def upload(client, y, assignment=aid, sr=SR):
            return client.post("/api/recordings",
                               {"assignment_id": assignment, "file": (wav_bytes(y, sr), "r.wav")},
                               content_type="multipart/form-data")

        # --- bad uploads are rejected, not stored ---
        self.assertEqual(upload(pat, noisy_voice(1.0)).status_code, 422)            # too short
        self.assertEqual(upload(pat, np.zeros(SR * 5, dtype="float32")).status_code, 422)  # silent
        self.assertEqual(upload(pat, noisy_voice(25)).status_code, 422)             # too long
        r = pat.post("/api/recordings", {"assignment_id": aid,
                     "file": (io.BytesIO(b"not audio"), "r.wav")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 422)

        # --- good upload (44.1 kHz to exercise resampling) ---
        r = upload(pat, np.repeat(noisy_voice(5), 1), sr=SR)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rid = r.get_json()["id"]

        # cleaned copy is loudness-levelled, peak-safe, 16 kHz mono
        clean, sr = sf.read(io.BytesIO(body(doc.get(f"/media/{rid}/clean.wav"))))
        self.assertEqual(sr, SR)
        self.assertEqual(clean.ndim, 1)
        self.assertLess(np.max(np.abs(clean)), 0.95)
        self.assertAlmostEqual(pyln.Meter(sr).integrated_loudness(clean), -16.0, delta=2.0)

        # original is untouched
        orig, _ = sf.read(io.BytesIO(body(doc.get(f"/media/{rid}/original.wav"))))
        self.assertEqual(len(orig), int(5 * SR))

        # doctor pages render and range requests work (needed for audio seeking on phones)
        self.assertEqual(doc.get(f"/doctor/recordings/{rid}").status_code, 200)
        self.assertEqual(doc.get(f"/doctor/patients/{patient_id}").status_code, 200)
        ranged = doc.get(f"/media/{rid}/clean.wav", headers={"Range": "bytes=0-99"})
        self.assertEqual(ranged.status_code, 206)
        ranged.close()

        # --- access control ---
        self.assertEqual(Client(self.app).get(f"/media/{rid}/clean.wav").status_code, 401)
        other_doc, _ = self.register(role="doctor", name="Dr B", email="b@x.in",
                                     password="longenough", code="pilot-code")
        self.assertEqual(other_doc.get(f"/media/{rid}/clean.wav").status_code, 404)
        self.assertEqual(other_doc.get(f"/doctor/recordings/{rid}").status_code, 404)
        self.assertEqual(other_doc.get(f"/doctor/patients/{patient_id}").status_code, 404)
        self.assertEqual(pat.get("/doctor").status_code, 403)
        self.assertEqual(doc.get("/patient").status_code, 403)
        other_pat, _ = self.register(role="patient", name="Q", email="q@x.in",
                                     password="longenough",
                                     code=re.search(r'class="code">([A-Z2-9]{6})<',
                                                    other_doc.get("/doctor").get_data(as_text=True)).group(1))
        self.assertEqual(other_pat.get(f"/media/{rid}/original.wav").status_code, 404)
        self.assertEqual(upload(other_pat, noisy_voice(5)).status_code, 404)  # not their assignment
        self.assertEqual(doc.get(f"/media/{rid}/../../etc").status_code, 404)

        # --- weekly adherence reflects the upload ---
        self.assertIn("1/3 this week", doc.get("/doctor").get_data(as_text=True))

        # --- removing an assignment hides it from the patient ---
        doc.post(f"/doctor/assignments/{aid}/remove")
        self.assertEqual(pat.get(f"/patient/record/{aid}").status_code, 404)


class NearbyDoctorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.app = create_app({
            "TESTING": True,
            "DATABASE": os.path.join(cls.tmp.name, "n.db"),
            "UPLOAD_DIR": os.path.join(cls.tmp.name, "rec"),
            "DOCTOR_SIGNUP_CODE": "pilot-code",
            "GOOGLE_MAPS_API_KEY": None,
        })

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def reg(self, **f):
        c = Client(self.app)
        return c, c.post("/register", f)

    def post_json(self, client, url, payload):
        return client.c.post(url, json=payload, headers={"X-CSRF-Token": client.token()})

    def test_unlinked_patient_flow(self):
        # Patient can register without an invite code.
        pat, r = self.reg(role="patient", name="Solo", email="solo@x.in", password="longenough", code="")
        self.assertEqual(r.status_code, 302)
        home = pat.get("/patient").get_data(as_text=True)
        self.assertIn("Connect with a clinician", home)
        self.assertIn("Find a specialist near you", home)
        self.assertIn("Adyar", home)

        # A wrong code at registration is still rejected.
        _, r = self.reg(role="patient", name="Bad", email="bad@x.in", password="longenough", code="ZZZZZZ")
        self.assertEqual(r.status_code, 400)

        # Nearby search by area and by coordinates; nearest first.
        r = self.post_json(pat, "/doctors", {"area": "adyar", "specialty": "slp"})
        self.assertEqual(r.status_code, 200)
        results = r.get_json()["results"]
        self.assertEqual(results[0]["area"], "Adyar")
        self.assertTrue(all(x["specialty"] == "slp" for x in results))
        r = self.post_json(pat, "/doctors", {"lat": 13.085, "lng": 80.21})
        self.assertEqual(r.get_json()["results"][0]["area"], "Anna Nagar")

        # Bad input is a clean 400, not a crash.
        self.assertEqual(self.post_json(pat, "/doctors", {}).status_code, 400)
        self.assertEqual(self.post_json(pat, "/doctors", {"lat": "x", "lng": 1}).status_code, 400)
        self.assertEqual(self.post_json(pat, "/doctors", {"area": "adyar", "specialty": "wizard"}).status_code, 400)

        # CSRF and auth are enforced.
        self.assertEqual(pat.c.post("/doctors", json={"area": "adyar"}).status_code, 400)
        anon = Client(self.app)
        self.assertEqual(self.post_json(anon, "/doctors", {"area": "adyar"}).status_code, 302)

        # Linking: wrong code fails, right code works, second link refused.
        doc, _ = self.reg(role="doctor", name="Dr Z", email="z@x.in", password="longenough", code="pilot-code")
        import re
        invite = re.search(r'class="code">([A-Z2-9]{6})<', doc.get("/doctor").get_data(as_text=True)).group(1)
        pat.post("/patient/link", {"code": "ZZZZZZ"})
        self.assertIn("Connect with a clinician", pat.get("/patient").get_data(as_text=True))
        pat.post("/patient/link", {"code": invite.lower()})
        linked = pat.get("/patient").get_data(as_text=True)
        self.assertIn("Dr Z", linked)
        self.assertIn("Find another specialist near you", linked)   # search stays available once linked
        pat.post("/patient/link", {"code": invite})
        self.assertIn("Solo", doc.get("/doctor").get_data(as_text=True))

    def test_shared_script_is_served(self):
        r = Client(self.app).get("/shared/doctors.js")
        self.assertEqual(r.status_code, 200)
        r.close()


class LiveSearchTest(unittest.TestCase):
    SECRET = "AIza-SECRET-KEY-XYZ"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.app = create_app({
            "TESTING": True,
            "DATABASE": os.path.join(cls.tmp.name, "l.db"),
            "UPLOAD_DIR": os.path.join(cls.tmp.name, "rec"),
            "GOOGLE_MAPS_API_KEY": cls.SECRET,
            "SEARCHES_PER_HOUR": 3,
        })

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def patient(self):
        c = Client(self.app)
        c.post("/register", {"role": "patient", "name": "P", "email": "p@x.in", "password": "longenough", "code": ""})
        c.get("/patient")          # a browser loads the next page, which issues a fresh CSRF token
        return c

    def search(self, c, **payload):
        return c.c.post("/doctors", json=payload, headers={"X-CSRF-Token": c.token()})

    def test_live_results_key_secrecy_fallback_and_quota(self):
        from backend.src.shared import places
        c = self.patient()
        live = [{"place_id": "g1", "name": "Live Clinic", "address": "Adyar", "lat": 13.01, "lng": 80.26,
                 "phone": "98765 43210", "rating": 4.6, "rating_count": 50, "maps_uri": "https://maps.google.com/?cid=1",
                 "website": None, "distance_km": 0.7}]

        with mock.patch("backend.src.shared.places.search", return_value=live) as g:
            r = self.search(c, area="adyar", specialty="slp")
            self.assertEqual(r.status_code, 200)
            data = r.get_json()
            self.assertEqual(data["source"], "google")
            self.assertEqual(data["results"][0]["name"], "Live Clinic")
            self.assertTrue(data["results"][0]["whatsapp_url"].startswith("https://wa.me/91"))
            self.assertEqual(g.call_args.args[3], self.SECRET)              # the key reaches Google ...
            self.assertNotIn(self.SECRET, r.get_data(as_text=True))          # ... and never the browser
            self.assertNotIn(self.SECRET, c.get("/patient").get_data(as_text=True))

        with mock.patch(
            "backend.src.shared.places.search",
            side_effect=places.PlacesError("HTTP 403"),
        ):
            r = self.search(c, area="adyar", specialty="slp")                # Google down: still answers
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["source"], "directory")
            self.assertTrue(r.get_json()["results"])
            self.assertTrue(r.get_json()["notice"])

        with mock.patch("backend.src.shared.places.search", return_value=live):
            self.assertEqual(self.search(c, area="adyar").status_code, 200)  # 3rd allowed
            r = self.search(c, area="adyar")                                 # 4th: quota guard
            self.assertEqual(r.status_code, 429)
            self.assertIn("Retry-After", r.headers)


if __name__ == "__main__":
    unittest.main()
