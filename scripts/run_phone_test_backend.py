"""Second backend for testing the Android app over the office Wi-Fi.

Listens on all interfaces (port 8020 by default) against the same database,
allows the app's WebView origin, and keeps the nightly attendance sync and
leave credit jobs off so they only run in the main backend.

    python scripts/run_phone_test_backend.py [port]
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

os.environ["CORS_ORIGINS"] = "http://localhost:3000,http://localhost:3001,https://localhost,capacitor://localhost"
os.environ["ATTENDANCE_SYNC_ENABLED"] = "false"
os.environ["LEAVE_CREDIT_ENABLED"] = "false"

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
