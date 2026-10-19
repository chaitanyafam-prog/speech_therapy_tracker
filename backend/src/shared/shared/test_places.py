import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nearby  # noqa: E402
import places  # noqa: E402

KEY = "AIza-TEST-KEY-1234567890"
ADYAR = (13.0067, 80.2570)


def gplace(pid, name, lat, lng, phone="+91 44 2345 6789", status="OPERATIONAL", **extra):
    p = {"id": pid, "displayName": {"text": name}, "formattedAddress": f"{name}, Chennai",
         "location": {"latitude": lat, "longitude": lng}, "internationalPhoneNumber": phone,
         "googleMapsUri": f"https://maps.google.com/?cid={pid}", "rating": 4.4, "userRatingCount": 120}
    if status:
        p["businessStatus"] = status
    p.update(extra)
    return p


class FakeHttp:
    def __init__(self, places_list):
        self.places_list, self.calls = places_list, []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return {"places": self.places_list}


class WhatsAppTest(unittest.TestCase):
    def test_mobile_formats(self):
        for n in ("98765 43210", "+91 98765 43210", "09876543210", "919876543210", "+91-9876543210"):
            self.assertEqual(places.indian_mobile(n), "9876543210", n)

    def test_landlines_and_junk_have_no_whatsapp(self):
        for n in ("044 2345 6789", "+91 44 2345 6789", "", None, "12345", "5876543210"):
            self.assertIsNone(places.whatsapp_url(n), n)

    def test_link_has_fixed_greeting_only(self):
        url = places.whatsapp_url("9876543210")
        self.assertTrue(url.startswith("https://wa.me/919876543210?text="))
        self.assertIn("appointment", url)


class SearchTest(unittest.TestCase):
    def setUp(self):
        places.clear_cache()

    def run_search(self, plist, specialty="slp"):
        http = FakeHttp(plist)
        return places.search(*ADYAR, specialty, KEY, http_post=http), http

    def test_request_shape_and_key_placement(self):
        _, http = self.run_search([gplace("a", "Near", 13.01, 80.26)])
        url, body, headers = http.calls[0]
        self.assertEqual(url, places.ENDPOINT)
        self.assertEqual(headers["X-Goog-Api-Key"], KEY)
        self.assertNotIn(KEY, url + json.dumps(body))                 # never in the URL or body
        self.assertIn("places.nationalPhoneNumber", headers["X-Goog-FieldMask"])
        self.assertEqual(body["locationBias"]["circle"]["center"], {"latitude": ADYAR[0], "longitude": ADYAR[1]})
        self.assertIn("speech", body["textQuery"])

    def test_sorted_nearest_first_and_bad_entries_dropped(self):
        res, _ = self.run_search([
            gplace("far", "Far", 13.12, 80.30), gplace("near", "Near", 13.008, 80.258),
            gplace("closed", "Closed", 13.0068, 80.2571, status="CLOSED_PERMANENTLY"),
            {"id": "noloc", "displayName": {"text": "No location"}},
            gplace("dup", "Dup", 13.05, 80.25), gplace("dup", "Dup again", 13.05, 80.25)])
        self.assertEqual([r["place_id"] for r in res], ["near", "dup", "far"])
        self.assertEqual([r["distance_km"] for r in res], sorted(r["distance_km"] for r in res))

    def test_cache_avoids_second_request(self):
        http = FakeHttp([gplace("a", "A", 13.01, 80.26)])
        places.search(*ADYAR, "slp", KEY, http_post=http)
        places.search(*ADYAR, "slp", KEY, http_post=http)
        self.assertEqual(len(http.calls), 1)

    def test_any_specialty_runs_two_queries(self):
        http = FakeHttp([gplace("a", "A", 13.01, 80.26)])
        places.search(*ADYAR, None, KEY, http_post=http)
        self.assertEqual(len(http.calls), 2)

    def test_no_key_raises(self):
        with self.assertRaises(places.PlacesError):
            places.search(*ADYAR, "slp", None, http_post=FakeHttp([]))

    def test_http_error_message_is_useful_and_key_free(self):
        body = json.dumps({"error": {"status": "PERMISSION_DENIED",
                                     "message": "Places API (New) has not been used in project 1 before"}})
        err = urllib.error.HTTPError(places.ENDPOINT, 403, "Forbidden", {}, io.BytesIO(body.encode()))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(places.PlacesError) as cm:
                places._post_json(places.ENDPOINT, {}, {"X-Goog-Api-Key": KEY})
        self.assertIn("PERMISSION_DENIED", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))

    def test_network_failure_becomes_places_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            with self.assertRaises(places.PlacesError):
                places._post_json(places.ENDPOINT, {}, {})


class NearbySearchTest(unittest.TestCase):
    def google(self, plist):
        return lambda lat, lng, spec, key: places.search(lat, lng, spec, key, http_post=FakeHttp(plist))

    def setUp(self):
        places.clear_cache()

    def test_no_key_uses_saved_list_silently(self):
        out = nearby.search(*ADYAR, "slp")
        self.assertEqual(out["source"], "directory")
        self.assertIsNone(out["notice"])
        self.assertTrue(out["results"])

    def test_live_results_replace_demo_entries(self):
        out = nearby.search(*ADYAR, "slp", api_key=KEY,
                            google=self.google([gplace("a", "Real Clinic", 13.01, 80.26, phone="98765 43210")]))
        self.assertEqual(out["source"], "google")
        self.assertEqual([r["name"] for r in out["results"]], ["Real Clinic"])
        r = out["results"][0]
        self.assertFalse(r["demo"])
        self.assertEqual(r["source"], "google")
        self.assertTrue(r["whatsapp_url"].startswith("https://wa.me/919876543210"))
        self.assertIn("destination_place_id=a", r["maps_url"])
        self.assertNotIn(KEY, json.dumps(out))

    def test_google_failure_falls_back_with_notice(self):
        def boom(*a):
            raise places.PlacesError("HTTP 403 PERMISSION_DENIED")
        out = nearby.search(*ADYAR, "slp", api_key=KEY, google=boom)
        self.assertEqual(out["source"], "directory")
        self.assertIn("unavailable", out["notice"])
        self.assertTrue(out["results"])
        self.assertNotIn("403", json.dumps(out))                      # internal detail stays in the logs

    def test_no_live_results_falls_back(self):
        out = nearby.search(*ADYAR, "slp", api_key=KEY, google=self.google([]))
        self.assertEqual(out["source"], "directory")
        self.assertTrue(out["notice"])

    def test_verified_saved_entry_is_kept_and_deduplicated(self):
        verified = [{"id": "v1", "name": "Verified Partner", "specialty": "slp", "lat": 13.0070, "lng": 80.2572,
                     "phone": "044 2345 6789", "verified_on": "2026-10-01", "area": "Adyar"},
                    {"id": "demo", "demo": True, "name": "Demo", "specialty": "slp", "lat": 13.0068,
                     "lng": 80.2571, "phone": None}]
        with mock.patch.object(nearby, "load_directory", return_value=tuple(verified)):
            out = nearby.search(*ADYAR, "slp", api_key=KEY, google=self.google([
                gplace("g1", "Same Clinic On Google", 13.0071, 80.2573, phone="+91 44 2345 6789"),
                gplace("g2", "Other Clinic", 13.02, 80.27, phone="+91 44 9999 0000")]))
        names = [r["name"] for r in out["results"]]
        self.assertEqual(names, ["Verified Partner", "Other Clinic"])   # duplicate phone dropped, demo gone
        self.assertEqual(out["source"], "google+directory")

    def test_limit_respected(self):
        many = [gplace(f"p{i}", f"C{i}", 13.0 + i / 1000, 80.25) for i in range(10)]
        self.assertEqual(len(nearby.search(*ADYAR, "slp", limit=3, api_key=KEY, google=self.google(many))["results"]), 3)


if __name__ == "__main__":
    unittest.main()
