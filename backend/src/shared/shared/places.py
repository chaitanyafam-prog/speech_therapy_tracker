"""Live clinician lookup with the Google Places API (New): Text Search.

Server-side only. The API key is read from the GOOGLE_MAPS_API_KEY environment variable (or a private
`.env` file at the project root) and is never sent to the browser or included in any response.

Cost note: asking for phone numbers, ratings and websites makes each search an "Enterprise" Text Search
request. Trim FIELDS below to cut cost. Results are cached in memory for a few minutes only; Google's terms
restrict storing Places content, so nothing here is written to disk or a database.

Self-test (prints no key):  python places.py --check "13.0418,80.2341"
"""
import json
import logging
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ["id", "displayName", "formattedAddress", "location", "nationalPhoneNumber",
          "internationalPhoneNumber", "rating", "userRatingCount", "googleMapsUri", "websiteUri",
          "businessStatus"]
FIELD_MASK = ",".join("places." + f for f in FIELDS)

# One query per specialty keeps cost down; "any" runs two.
QUERIES = {
    "slp": ["speech therapist speech language pathologist"],
    "neurologist": ["neurologist"],
    "ent": ["ENT specialist"],
    "gp": ["general physician doctor"],
    None: ["speech therapist", "neurologist"],
}
SEARCH_RADIUS_M = 20000
RESULTS_PER_QUERY = 10
CACHE_SECONDS = 300
TIMEOUT_SECONDS = 8

_ENV_LOADED = False
_cache = {}
_cache_lock = threading.Lock()


class PlacesError(Exception):
    """The live lookup failed. The message is safe to log; it never contains the key."""


# ----------------------------- key handling -----------------------------

def load_env_file(path=None):
    """Read KEY=VALUE lines from <project root>/.env into os.environ (existing variables win)."""
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except OSError:
        pass


def api_key():
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_env_file()
        _ENV_LOADED = True
    return os.environ.get("GOOGLE_MAPS_API_KEY") or None


# ----------------------------- helpers -----------------------------

def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def digits(number):
    return re.sub(r"\D", "", number or "")


def indian_mobile(number):
    """10-digit Indian mobile (starts 6-9) from any common formatting, else None. Landlines return None."""
    d = digits(number)
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if len(d) == 10 and d[0] in "6789" else None


def whatsapp_url(number, message="Hello, I would like to book an appointment for a speech assessment."):
    """wa.me link with a fixed greeting. Nothing about the user is put in the message."""
    mobile = indian_mobile(number)
    if not mobile:
        return None
    return f"https://wa.me/91{mobile}?text={urllib.parse.quote(message)}"


def _post_json(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            err = json.loads(exc.read().decode("utf-8")).get("error", {})
            detail = f"{err.get('status', '')}: {err.get('message', '')}"
        except Exception:
            pass
        raise PlacesError(f"HTTP {exc.code} {detail}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PlacesError(f"network error: {exc}") from None
    except ValueError:
        raise PlacesError("unreadable response from Google") from None


def _normalise(place, lat, lng):
    loc = place.get("location") or {}
    if "latitude" not in loc or "longitude" not in loc:
        return None
    if place.get("businessStatus") not in (None, "OPERATIONAL"):
        return None                                   # closed or temporarily closed
    phone = place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber")
    return {
        "place_id": place.get("id"),
        "name": (place.get("displayName") or {}).get("text") or "Unnamed clinic",
        "address": place.get("formattedAddress"),
        "lat": loc["latitude"], "lng": loc["longitude"],
        "phone": phone,
        "rating": place.get("rating"), "rating_count": place.get("userRatingCount"),
        "maps_uri": place.get("googleMapsUri"), "website": place.get("websiteUri"),
        "distance_km": round(haversine_km(lat, lng, loc["latitude"], loc["longitude"]), 1),
    }


# ----------------------------- the search -----------------------------

def search(lat, lng, specialty, key, http_post=_post_json, now=time.monotonic):
    """Nearby clinicians from Google, nearest first. Raises PlacesError on any failure."""
    if not key:
        raise PlacesError("no API key configured")
    cache_key = (round(lat, 3), round(lng, 3), specialty)
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and now() - hit[0] < CACHE_SECONDS:
            return list(hit[1])

    found = {}
    for query in QUERIES.get(specialty, QUERIES[None]):
        body = {
            "textQuery": query,
            "maxResultCount": RESULTS_PER_QUERY,
            "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": SEARCH_RADIUS_M}},
        }
        data = http_post(ENDPOINT, body, {"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK})
        for place in data.get("places", []):
            item = _normalise(place, lat, lng)
            if item and item["place_id"] not in found:
                found[item["place_id"]] = item
    results = sorted(found.values(), key=lambda r: r["distance_km"])

    with _cache_lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[cache_key] = (now(), results)
    return list(results)


def clear_cache():
    with _cache_lock:
        _cache.clear()


# ----------------------------- command-line self-test -----------------------------

def _check(argv):
    lat, lng = (float(x) for x in (argv[0] if argv else "13.0418,80.2341").split(","))
    key = api_key()
    if not key:
        print("No key found. Put GOOGLE_MAPS_API_KEY=... in the .env file at the project root.")
        return 1
    print(f"Key found ({len(key)} characters). Searching near {lat}, {lng} ...")
    try:
        results = search(lat, lng, "slp", key)
    except PlacesError as exc:
        print("FAILED:", exc)
        print("Common causes: 'Places API (New)' not enabled, billing not enabled, or the key is restricted "
              "to other APIs / another IP address.")
        return 2
    print(f"OK: {len(results)} result(s)")
    for r in results[:5]:
        print(f"  {r['distance_km']:5.1f} km  {r['name']}  |  phone: {r['phone'] or 'none listed'}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(_check(args[1:] if args[:1] == ["--check"] else args))
