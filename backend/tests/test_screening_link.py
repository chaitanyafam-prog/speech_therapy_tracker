import json
import os
import tempfile
import unittest
from unittest import mock

from backend.src.app import create_app
from backend.tests.test_flow import Client

from backend.src.shared import linking

KEY = "test-secret-value"


class ScreeningLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.app = create_app({
            "TESTING": True,
            "DATABASE": os.path.join(cls.tmp.name, "t.db"),
            "UPLOAD_DIR": os.path.join(cls.tmp.name, "rec"),
            "DOCTOR_SIGNUP_CODE": "pilot-code",
            "SCREENING_SHARED_SECRET": KEY,
        })

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SCREENER_URL": "http://screener.test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def register(self, **fields):
        c = Client(self.app)
        r = c.post("/register", fields)
        self.assertEqual(r.status_code, 302)
        return c

    def post_result(self, token, auth=None, **payload):
        data = {"token": token, "prediction": 1, "confidence": 80.5}
        data.update(payload)
        headers = {"Authorization": auth if auth is not None else "Bearer " + KEY}
        return self.app.test_client().post("/api/screenings", data=json.dumps(data),
                                           content_type="application/json", headers=headers)

    def test_end_to_end_and_visibility(self):
        doc = self.register(role="doctor", name="Dr S", email="s@x.in",
                            password="longenough", code="pilot-code")
        # Read the invite code from the database rather than scraping HTML.
        with self.app.app_context():
            from backend.src.db import get_db
            code = get_db().execute("SELECT invite_code FROM users WHERE email='s@x.in'").fetchone()[0]
        pat = self.register(role="patient", name="Pat", email="p@x.in",
                            password="longenough", code=code)
        other = self.register(role="patient", name="Other", email="o@x.in", password="longenough")

        # Patient is redirected to the screener with a token that names only their id.
        r = pat.get("/patient/screen")
        self.assertEqual(r.status_code, 302)
        loc = r.headers["Location"]
        self.assertTrue(loc.startswith("http://screener.test/?link="))
        token = loc.split("link=", 1)[1]
        pid = linking.read_token(token, KEY)
        self.assertIsNotNone(pid)

        # Screener posts the result.
        r = self.post_result(token, prediction=1, confidence=80.5)
        self.assertEqual(r.status_code, 201)

        # The patient and their own doctor see it; another patient does not.
        self.assertIn("Patterns detected", pat.get("/patient").get_data(as_text=True))
        self.assertIn("Patterns detected",
                      doc.get(f"/doctor/patients/{pid}").get_data(as_text=True))
        self.assertNotIn("Patterns detected", other.get("/patient").get_data(as_text=True))

    def test_rejects_bad_requests(self):
        pat = self.register(role="patient", name="P2", email="p2@x.in", password="longenough")
        token = linking.make_token(1, KEY)
        self.assertEqual(self.post_result(token, auth="").status_code, 401)
        self.assertEqual(self.post_result(token, auth="Bearer wrong").status_code, 401)
        self.assertEqual(self.post_result("garbage").status_code, 400)
        self.assertEqual(self.post_result(linking.make_token(1, "other-key")).status_code, 400)
        self.assertEqual(self.post_result(token, prediction=2).status_code, 400)
        self.assertEqual(self.post_result(token, prediction=True).status_code, 400)
        self.assertEqual(self.post_result(token, confidence=150).status_code, 400)
        self.assertEqual(self.post_result(token, confidence="high").status_code, 400)
        # A token for a user that does not exist, or a doctor, is refused.
        self.assertEqual(self.post_result(linking.make_token(9999, KEY)).status_code, 400)
        with self.app.app_context():
            from backend.src.db import get_db
            doc_id = get_db().execute("SELECT id FROM users WHERE role='doctor' LIMIT 1").fetchone()
        if doc_id:
            self.assertEqual(self.post_result(linking.make_token(doc_id[0], KEY)).status_code, 400)
        del pat

    def test_token_expires(self):
        token = linking.make_token(1, KEY)
        self.assertEqual(linking.read_token(token, KEY), 1)
        self.assertIsNone(linking.read_token(token, KEY, max_age=-1))

    def test_disabled_without_secret(self):
        app = create_app({
            "TESTING": True,
            "DATABASE": os.path.join(self.tmp.name, "off.db"),
            "UPLOAD_DIR": os.path.join(self.tmp.name, "rec2"),
            "SCREENING_SHARED_SECRET": None,
        })
        r = app.test_client().post("/api/screenings", data="{}", content_type="application/json")
        self.assertEqual(r.status_code, 503)

    def test_only_patients_can_start_a_screening(self):
        r = Client(self.app).get("/patient/screen")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
