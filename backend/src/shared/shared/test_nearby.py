import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nearby  # noqa: E402


class HaversineTest(unittest.TestCase):
    def test_zero_distance(self):
        self.assertAlmostEqual(nearby.haversine_km(13.04, 80.23, 13.04, 80.23), 0.0)

    def test_known_chennai_pair(self):
        # T. Nagar to Tambaram is roughly 18 km as the crow flies.
        d = nearby.haversine_km(*nearby.AREAS["t_nagar"][1:], *nearby.AREAS["tambaram"][1:])
        self.assertGreater(d, 16)
        self.assertLess(d, 21)


class OriginTest(unittest.TestCase):
    def test_coordinates_win_over_area(self):
        self.assertEqual(nearby.resolve_origin("13.0", "80.2", "adyar"), (13.0, 80.2))

    def test_area_fallback(self):
        self.assertEqual(nearby.resolve_origin(None, None, "adyar"), nearby.AREAS["adyar"][1:])

    def test_invalid_inputs(self):
        for args in [("abc", "80", None), ("95", "80", None), ("13", "200", None),
                     ("nan", "80", None), (None, None, "nowhere"), (None, None, None), ("", "", "")]:
            self.assertIsNone(nearby.resolve_origin(*args), args)


class NearestTest(unittest.TestCase):
    def test_sorted_nearest_first(self):
        lat, lng = nearby.AREAS["adyar"][1:]
        res = nearby.nearest(lat, lng)
        self.assertEqual(len(res), 5)
        self.assertEqual([r["distance_km"] for r in res], sorted(r["distance_km"] for r in res))
        self.assertEqual(res[0]["area"], "Adyar")

    def test_specialty_filter(self):
        res = nearby.nearest(13.04, 80.23, "neurologist", limit=10)
        self.assertTrue(res)
        self.assertTrue(all(r["specialty"] == "neurologist" for r in res))

    def test_far_origin_still_returns_nearest(self):
        res = nearby.nearest(28.61, 77.21)  # Delhi
        self.assertTrue(res)
        self.assertGreater(res[0]["distance_km"], 1000)

    def test_limit_is_clamped(self):
        self.assertEqual(len(nearby.nearest(13.04, 80.23, limit=0)), 1)
        self.assertLessEqual(len(nearby.nearest(13.04, 80.23, limit=999)), 20)

    def test_directory_entries_are_valid_and_demo_is_flagged(self):
        entries = nearby.load_directory()
        self.assertTrue(entries)
        for e in entries:
            if e.get("demo"):
                self.assertIsNone(e["phone"], "demo entries must not carry a dialable number")

    def test_invalid_entries_are_dropped(self):
        self.assertFalse(nearby._valid({"name": "x", "specialty": "wizard", "lat": 1, "lng": 1}))
        self.assertFalse(nearby._valid({"name": "x", "specialty": "slp", "lat": 99, "lng": 1}))
        self.assertTrue(nearby._valid({"name": "x", "specialty": "slp", "lat": 13, "lng": 80}))


if __name__ == "__main__":
    unittest.main()
