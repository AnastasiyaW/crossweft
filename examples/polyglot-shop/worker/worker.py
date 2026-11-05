"""Marks paid orders as shipped once the warehouse confirms them."""

import os
import time
from pathlib import Path

# Must match the Go constants in server/main.go: the API reads what we write.
STATUSES = {"pending", "paid", "shipped"}

DB_URL = os.environ["SHOP_DB_URL"]
POLL_SECONDS = int(os.getenv("SHOP_WORKER_POLL_SECONDS", "30"))
HEARTBEAT_FILE = Path(os.getenv("SHOP_WORKER_HEARTBEAT_FILE", "/tmp/shop-worker.heartbeat"))


def beat() -> None:
    """Called once per poll: the monitor alarms when this file stops changing."""
    HEARTBEAT_FILE.write_text(str(int(time.time())))


def ship(order_id: str, status: str) -> str:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    return "shipped" if status == "paid" else status
