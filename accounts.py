"""
Linked-account ledger + milestones (per instance).

Every "Account linked!" the PRIMES bot confirms is appended here with its
number, provider and time, so Telegram can answer

    /accounts            - how many accounts were linked, per provider, and
                           what is new since the last milestone
    /accounts list       - the numbers linked since the last milestone
    /milestone <number> <note>
                         - "everything up to and including <number> is dealt
                           with" (used / shared / handed over); the note is
                           free text, e.g. "10 used + 40 shared"
    /milestone           - show the milestones; /milestone remove drops the
                           last one (typo)

Parallel tabs each keep their own file (accounts.<instance>.json, see
runtime.py), like stats / state / pending cancels. Written atomically.

The JSON layout:

    {"accounts":   [{"seq": 1, "number": "9876543210", "provider": "tempora",
                     "meesho_user_id": "...", "bot_account_number": "...",
                     "linked_at": "<iso utc>", "linked_at_epoch": 1.0}, ...],
     "milestones": [{"seq": 1, "through_seq": 50, "last_number": "98...",
                     "note": "10 used + 40 shared", "total": 50,
                     "by_provider": {"tempora": 30, "vsimpro": 20},
                     "created_at": "<iso utc>", "created_at_epoch": 1.0}]}

`through_seq` is the ledger seq of the milestone's last number: everything
with a higher seq is "after the milestone".
"""

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from runtime import DEFAULT_ACCOUNTS_FILE, namespaced_name


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_number(value):
    """Same normalisation as the checker: a bare 10-digit Indian number."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def looks_like_number(value):
    """True for something that can only be a phone number (10-13 digits)."""
    text = str(value or "").strip()
    if text.startswith("+"):
        text = text[1:]
    return text.isdigit() and 10 <= len(text) <= 13


def local_time(epoch, fmt="%d %b %H:%M"):
    """Epoch -> local wall-clock text (the phone's timezone)."""
    try:
        return datetime.fromtimestamp(float(epoch)).strftime(fmt)
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


class LinkedAccountsStore:

    def __init__(self, filename=DEFAULT_ACCOUNTS_FILE, instance=None):
        if instance and filename == DEFAULT_ACCOUNTS_FILE:
            filename = namespaced_name(filename, instance)
        self.path = Path(filename)
        self._lock = threading.Lock()
        self._accounts = []
        self._milestones = []
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self):
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        accounts = data.get("accounts")
        if isinstance(accounts, list):
            self._accounts = [dict(r) for r in accounts if isinstance(r, dict)]
        milestones = data.get("milestones")
        if isinstance(milestones, list):
            self._milestones = [dict(r) for r in milestones if isinstance(r, dict)]
        # Older / hand-edited files: make sure every record has a seq.
        for index, record in enumerate(self._accounts, start=1):
            record.setdefault("seq", index)

    def _save_locked(self):
        payload = json.dumps({"accounts": self._accounts,
                              "milestones": self._milestones},
                             indent=2, ensure_ascii=False)
        temporary = self.path.with_suffix(".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
            temporary.replace(self.path)
        except Exception:
            try:
                with self.path.open("w", encoding="utf-8") as handle:
                    handle.write(payload)
                if temporary.exists():
                    temporary.unlink()
            except Exception:
                pass

    # -- accounts ------------------------------------------------------------

    def add(self, number, provider, user_id=None, account_number=None):
        """Record one linked account; returns the stored record."""
        with self._lock:
            seq = (self._accounts[-1]["seq"] + 1) if self._accounts else 1
            record = {
                "seq": seq,
                "number": clean_number(number),
                "provider": str(provider or "").strip().lower(),
                "meesho_user_id": None if user_id in (None, "") else str(user_id),
                "bot_account_number": (None if account_number in (None, "")
                                       else str(account_number)),
                "linked_at": now_iso(),
                "linked_at_epoch": time.time(),
            }
            self._accounts.append(record)
            self._save_locked()
            return dict(record)

    def accounts(self):
        with self._lock:
            return [dict(r) for r in self._accounts]

    def total(self):
        with self._lock:
            return len(self._accounts)

    @staticmethod
    def count_by_provider(records):
        """{provider: count}, biggest first (ties by name)."""
        counts = {}
        for record in records:
            name = str(record.get("provider") or "unknown")
            counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def find(self, number):
        """The most recent record for a number (None when never linked)."""
        wanted = clean_number(number)
        if not wanted:
            return None
        with self._lock:
            for record in reversed(self._accounts):
                if clean_number(record.get("number")) == wanted:
                    return dict(record)
        return None

    # -- milestones ----------------------------------------------------------

    def milestones(self):
        with self._lock:
            return [dict(m) for m in self._milestones]

    def last_milestone(self):
        with self._lock:
            return dict(self._milestones[-1]) if self._milestones else None

    def add_milestone(self, last_number, note=""):
        """
        Mark everything up to and including `last_number` as dealt with.

        Raises ValueError when that number was never linked here (a typo would
        otherwise silently put the marker in the wrong place).
        """
        record = self.find(last_number)
        if record is None:
            raise ValueError(f"{clean_number(last_number) or last_number} is not in "
                             f"this instance's linked accounts")
        with self._lock:
            through = int(record["seq"])
            covered = [r for r in self._accounts if int(r.get("seq", 0)) <= through]
            milestone = {
                "seq": (self._milestones[-1]["seq"] + 1) if self._milestones else 1,
                "through_seq": through,
                "last_number": record["number"],
                "note": str(note or "").strip(),
                "total": len(covered),
                "by_provider": self.count_by_provider(covered),
                "created_at": now_iso(),
                "created_at_epoch": time.time(),
            }
            self._milestones.append(milestone)
            self._save_locked()
            return dict(milestone)

    def remove_last_milestone(self):
        with self._lock:
            if not self._milestones:
                return None
            removed = self._milestones.pop()
            self._save_locked()
            return dict(removed)

    def since(self, milestone=None):
        """Accounts linked after `milestone` (default: the last one; all when none)."""
        if milestone is None:
            milestone = self.last_milestone()
        through = int(milestone.get("through_seq", 0)) if milestone else 0
        with self._lock:
            return [dict(r) for r in self._accounts if int(r.get("seq", 0)) > through]
