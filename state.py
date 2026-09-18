"""
Last-known run state, used to resume/report after a crash or a restart.

Parallel runs (one Termux tab per provider) each get their own file: pass
`instance="tempora"` for state.tempora.json (see runtime.py).
"""

import json
from pathlib import Path

from runtime import DEFAULT_STATE_FILE, namespaced_name


class StateStore:

    def __init__(self, filename=DEFAULT_STATE_FILE, instance=None):
        if instance and filename == DEFAULT_STATE_FILE:
            filename = namespaced_name(filename, instance)
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