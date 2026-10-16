"""Container liveness probe: heartbeat must be newer than 120 seconds."""
import os
import sys
import time
from pathlib import Path

path = Path(os.getenv("DATA_DIR", "data")) / "heartbeat"
try:
    age = time.time() - float(path.read_text(encoding="utf-8"))
    sys.exit(0 if 0 <= age < 120 else 1)
except (OSError, ValueError):
    sys.exit(1)
