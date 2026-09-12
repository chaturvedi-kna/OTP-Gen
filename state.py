import json
from pathlib import Path


class StateStore:

    def __init__(self, filename="state.json"):
        self.path = Path(filename)

    def load(self):
        if not self.path.exists():
            return None

        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save(self, state):
        temporary = self.path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            temporary.replace(self.path)
        except Exception:
            # Fallback direct write for Windows file lock safety
            try:
                with self.path.open("w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
                if temporary.exists():
                    temporary.unlink()
            except Exception:
                pass

    def clear(self):
        if self.path.exists():
            try:
                self.path.unlink()
            except Exception:
                pass