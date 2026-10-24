"""Run the dashboard on the in-memory ledger. No network, no keys.

  python scripts/offline_demo.py        then open http://localhost:8403
"""
import os

import _boot  # noqa: F401

os.environ["LOCKBOX_MODE"] = "sim"
import uvicorn  # noqa: E402

print("open http://localhost:8403  (simulated ledger, nothing leaves this machine)")
uvicorn.run("api.app:app", host="127.0.0.1", port=8403, log_level="warning")
