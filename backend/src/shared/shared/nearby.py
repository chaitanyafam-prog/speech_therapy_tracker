"""Nearest-clinician lookup over a curated directory (Chennai).

Pure functions so both Flask apps can share them. The directory is a JSON file of
hand-verified entries; the repo ships only clearly marked demo entries.
"""
import json
import logging
import math
import os
from functools import lru_cache

import places

log = logging.getLogger(__name__)

DIRECTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doctors_chennai.json")

SPECIALTIES = {
    "slp": "Speech-language pathologist",
    "neurologist": "Neurologist",
    "ent": "ENT specialist",
    "gp": "General physician",
}

# Approximate centres of Chennai neighbourhoods, used when the browser location is unavailable.
AREAS = {
    "adyar": ("Adyar", 13.0067, 80.2570),
    "anna_nagar": ("Anna Nagar", 13.0850, 80.2101),
    "egmore": ("Egmore", 13.0732, 80.2609),
    "guindy": ("Guindy", 13.0067, 80.2206),
    "mylapore": ("Mylapore", 13.0368, 80.2676),
    "perambur": ("Perambur", 13.1186, 80.2330),
    "porur": ("Porur", 13.0358, 80.1565),
    "t_nagar": ("T. Nagar", 13.0418, 80.2341),
    "tambaram": ("Tambaram", 12.9249, 80.1000),
    "velachery": ("Velachery", 12.9815, 80.2180),
}


def area_choices():
    """[(key, label)] sorted by label, for a dropdown."""
    return sorted(((k, v[0]) for k, v in AREAS.items()), key=lambda kv: kv[1])


@lru_cache(maxsize=1)
def load_directory(path=DIRECTORY_PATH):
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)["doctors"]
    return tuple(e for e in entries if _valid(e))


def _valid(e):
    return (e.get("specialty") in SPECIALTIES
            and isinstance(e.get("lat"), (int, float)) and -90 <= e["lat"] <= 90
            and isinstance(e.get("lng"), (int, float)) and -180 <= e["lng"] <= 180
            and e.get("name"))


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def resolve_origin(lat=None, lng=None, area=None):
    """Return (lat, lng) from coordinates or an area key, or None if neither is usable."""
    try:
        if lat is not None and lng is not None and lat != "" and lng != "":
            lat, lng = float(lat), float(lng)
            if math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180:
                return lat, lng
    except (TypeError, ValueError):
        pass
    if area in AREAS:
        return AREAS[area][1], AREAS[area][2]
    return None


def maps_url(entry):
    return f"https://www.google.com/maps/dir/?api=1&destination={entry['lat']},{entry['lng']}"


def nearest(lat, lng, specialty=None, limit=5, directory=None):
    """Closest entries to (lat, lng), nearest first. Straight-line distance, not road distance."""
    entries = load_directory() if directory is None else directory
    if specialty:
        entries = [e for e in entries if e["specialty"] == specialty]
    ranked = sorted(entries, key=lambda e: haversine_km(lat, lng, e["lat"], e["lng"]))
    out = []
    for e in ranked[:max(1, min(int(limit), 20))]:
        out.append({
            "id": e.get("id"),
            "name": e["name"],
            "specialty": e["specialty"],
            "specialty_label": SPECIALTIES[e["specialty"]],
            "clinic": e.get("clinic"),
            "address": e.get("address"),
            "area": e.get("area"),
            "phone": e.get("phone"),
            "email": e.get("email"),
            "hours": e.get("hours"),
            "verified_on": e.get("verified_on"),
            "demo": bool(e.get("demo")),
            "distance_km": round(haversine_km(lat, lng, e["lat"], e["lng"]), 1),
            "maps_url": maps_url(e),
        })
    return out


def _from_google(g, specialty):
    return {
        "id": g["place_id"], "name": g["name"], "specialty": specialty,
        "specialty_label": SPECIALTIES.get(specialty, "Clinician"),
        "clinic": None, "address": g["address"], "area": None,
        "phone": g["phone"], "email": None, "hours": None,
        "verified_on": None, "demo": False, "source": "google",
        "distance_km": g["distance_km"],
        "maps_url": (f"https://www.google.com/maps/dir/?api=1&destination={g['lat']},{g['lng']}"
                     f"&destination_place_id={g['place_id']}"),
        "maps_uri": g["maps_uri"], "website": g["website"],
        "rating": g["rating"], "rating_count": g["rating_count"],
        "whatsapp_url": places.whatsapp_url(g["phone"]),
    }


def search(lat, lng, specialty=None, limit=5, api_key=None, google=None):
    """Nearest clinicians to connect with. Never comes back empty-handed:

    * with a Google key: live results (plus any real, verified entries from our own list), nearest first;
    * with no key, or if Google fails or finds nothing: our saved list.
    Returns {"results": [...], "source": "google" | "google+directory" | "directory", "notice": str | None}.
    """
    google = google or places.search
    limit = max(1, min(int(limit), 20))
    saved = nearest(lat, lng, specialty, limit=20)
    for item in saved:
        item.setdefault("source", "directory")
        item["whatsapp_url"] = places.whatsapp_url(item.get("phone"))

    def fallback(notice):
        return {"results": saved[:limit], "source": "directory", "notice": notice}

    if not api_key:
        return fallback(None)
    try:
        live = google(lat, lng, specialty, api_key)
    except places.PlacesError as exc:
        log.warning("Google Places lookup failed: %s", exc)
        return fallback("Live search is unavailable right now, so this is our saved list.")
    if not live:
        return fallback("No clinics were found nearby on the live search, so this is our saved list.")

    def phone_key(number):
        d = places.digits(number)
        return d[-10:] if len(d) >= 10 else d          # same clinic whether written 044 .. or +91 44 ..

    real_saved = [s for s in saved if not s["demo"]]
    seen = {phone_key(s["phone"]) for s in real_saved if s.get("phone")}
    merged = real_saved + [_from_google(g, specialty) for g in live
                           if not (g["phone"] and phone_key(g["phone"]) in seen)]
    merged.sort(key=lambda r: r["distance_km"])
    return {"results": merged[:limit], "source": "google+directory" if real_saved else "google", "notice": None}
