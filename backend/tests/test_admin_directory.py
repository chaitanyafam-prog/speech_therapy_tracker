import os
import tempfile
import unittest

from backend.src.app import create_app
from backend.src.db import get_db
from backend.tests.test_flow import Client


def listing(name, email, phone="98765 43210"):
    return {
        "name": name,
        "specialty": "Speech-language pathologist",
        "clinic": f"{name} Clinic",
        "address": "1 Main Road",
        "city": "Chennai",
        "place": "Adyar",
        "phone": phone,
        "email": email,
        "booking_url": "https://clinic.example/book",
        "doctor_user_id": "",
    }


class AdminDirectoryBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.app = create_app({
            "TESTING": True,
            "DATABASE": os.path.join(self.tmp.name, "tracker.db"),
            "UPLOAD_DIR": os.path.join(self.tmp.name, "recordings"),
            "ADMIN_PASSWORD": "admin-test-password",
        })
        self.client = Client(self.app)
        response = self.client.post("/admin", {"password": "admin-test-password"})
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.tmp.cleanup()

    def test_admin_form_starts_with_multiple_rows_and_add_row_script(self):
        response = self.client.get("/admin/doctors")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("<legend>Clinician 1</legend>", page)
        self.assertIn("<legend>Clinician 2</legend>", page)
        self.assertIn("Add another clinician", page)
        script = self.client.get("/static/admin-directory.js")
        self.assertEqual(script.status_code, 200)
        script.close()

    def test_adds_multiple_pending_listings(self):
        first = listing("Dr One", "one@example.com")
        second = listing("Dr Two", "two@example.com")
        data = {
            key: [first[key], second[key]]
            for key in first
        }
        response = self.client.post("/admin/doctors", data)
        self.assertEqual(response.status_code, 302)

        with self.app.app_context():
            records = get_db().execute(
                "SELECT name, verification_status FROM clinician_directory ORDER BY name"
            ).fetchall()
        self.assertEqual(
            [(row["name"], row["verification_status"]) for row in records],
            [("Dr One", "pending"), ("Dr Two", "pending")],
        )

    def test_invalid_row_rejects_the_entire_batch_and_preserves_input(self):
        first = listing("Dr One", "one@example.com")
        second = listing("Dr Two", "two@example.com", phone="bad")
        data = {
            key: [first[key], second[key]]
            for key in first
        }
        response = self.client.post("/admin/doctors", data)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Listing 2:", page)
        self.assertIn('value="Dr One"', page)

        with self.app.app_context():
            count = get_db().execute(
                "SELECT COUNT(*) FROM clinician_directory"
            ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
