"""Compatibility launcher for the Flask app in backend/src."""

from backend.src.app import app, create_app


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
