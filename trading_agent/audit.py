import json
from pathlib import Path
from datetime import datetime, timezone

class AuditLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event, payload):
        row = {"timestamp": datetime.now(timezone.utc).isoformat(),
               "event": event, "payload": payload}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
