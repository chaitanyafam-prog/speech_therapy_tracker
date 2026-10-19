"""Link a screening result to a tracker patient, server to server.

Flow: a logged-in tracker patient is sent to the screener with a short-lived signed token that holds
only their patient id. After predicting, the screener POSTs the result to the tracker, proving itself
with the shared secret. The tracker checks both and stores the result.

Both apps read the same SCREENING_SHARED_SECRET (environment variable or the private root `.env`).
If it is unset the whole feature is switched off; there is no default secret.
"""
import json
import os
import urllib.error
import urllib.request

from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import places

SALT = "screening-link-v1"
TOKEN_MAX_AGE = 30 * 60  # seconds; long enough to pick a recording, short enough to be low risk
TIMEOUT_SECONDS = 5


def secret():
    places.api_key()  # makes sure the root .env has been loaded
    return os.environ.get("SCREENING_SHARED_SECRET", "").strip() or None


def make_token(patient_id, key):
    return URLSafeTimedSerializer(key, salt=SALT).dumps({"pid": int(patient_id)})


def read_token(token, key, max_age=TOKEN_MAX_AGE):
    """Return the patient id from a valid, unexpired token, else None."""
    if not token or not key:
        return None
    try:
        data = URLSafeTimedSerializer(key, salt=SALT).loads(token, max_age=max_age)
        return int(data["pid"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


def post_screening(tracker_url, token, prediction, confidence, key, timeout=TIMEOUT_SECONDS):
    """Send one result to the tracker. Returns True only if the tracker stored it."""
    if not (tracker_url and token and key):
        return False
    body = json.dumps({"token": token, "prediction": int(prediction),
                       "confidence": float(confidence)}).encode()
    req = urllib.request.Request(
        tracker_url.rstrip("/") + "/api/screenings", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 201
    except (urllib.error.URLError, OSError, ValueError):
        return False
