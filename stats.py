"""
Persistent run statistics for the Meesho automation.

Counters survive restarts (stored atomically in stats.json) and are surfaced
through Telegram /status and the per-event notifications.

Parallel runs (one Termux tab per provider) each get their own file: pass
`instance="tempora"` for stats.tempora.json (see runtime.py).
"""

import json
import threading
from pathlib import Path

from runtime import DEFAULT_STATS_FILE, namespaced_name


# Canonical counters with default values.
COUNTERS = {
    "targets_found": 0,        # fresh unregistered numbers found
    "accounts_linked": 0,      # bot confirmed "Account linked!"
    "otp_received": 0,         # OTPs delivered by providers
    "otp_wrong": 0,            # bot rejected code as wrong/incorrect
    "otp_expired": 0,          # bot/code expired
    "user_blocked": 0,         # Meesho blocked / banned the number during signup
    "otp_timeout": 0,          # no OTP arrived within the wait window
    "change_number": 0,        # bot "Change Number" recoveries used
    "bot_menu_resets": 0,      # unrecovered Change Numbers that reset the bot to the menu
    "bot_flow_kept": 0,        # unrecovered Change Numbers where the bot stayed in-flow
    "numbers_cancelled": 0,    # provider activations cancelled
    "numbers_consumed": 0,     # numbers kept/charged (SMS delivered, no refund possible)
    "refunds_verified": 0,     # cancellations where the balance tally matched
    "refunds_missing": 0,      # cancellations where the refund never tallied
    "critical_stops": 0,       # safety-gated full stops
    "offer_rerolls": 0,        # "Try Another Offer" taps
    "late_otp_salvaged": 0,    # OTP found during/after the cancel race
    "referral_pasted": 0,      # referral link pasted at the bot's referral step
    "referral_skipped": 0,     # referral step answered with the bot's skip option
    "checker_api_checks": 0,   # numbers checked through the HTTP checker API
    "checker_bot_checks": 0,   # numbers checked through the PRIMES bot checker
    "checker_dedicated_checks": 0,  # numbers checked through the dedicated checker bot
    "checker_fallbacks": 0,    # checks that fell back from the API to the bot
    "cancel_deferred": 0,      # cancellations the provider refused, postponed to activation expiry
    "cancel_deferred_refunded": 0,  # deferred cancellations refunded after the retry
    "cancel_deferred_otp": 0,  # deferred cancellations where the OTP still arrived
    "offer_prewarmed": 0,      # PRIMES bot parked on an agreed offer before a number existed
    "bot_claims_denied": 0,    # bot checks skipped because a login flow owned the bot
}


class StatsStore:

    def __init__(self, filename=DEFAULT_STATS_FILE, instance=None):
        if instance and filename == DEFAULT_STATS_FILE:
            filename = namespaced_name(filename, instance)
        self.path = Path(filename)
        self._lock = threading.Lock()
        self._counters = dict(COUNTERS)
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in COUNTERS:
                    if key in data and isinstance(data[key], int):
                        self._counters[key] = data[key]
        except Exception:
            pass

    def _save_locked(self):
        temporary = self.path.with_suffix(".tmp")
        payload = json.dumps(self._counters, indent=2)
        try:
            with temporary.open("w", encoding="utf-8") as f:
                f.write(payload)
            temporary.replace(self.path)
        except Exception:
            try:
                with self.path.open("w", encoding="utf-8") as f:
                    f.write(payload)
                if temporary.exists():
                    temporary.unlink()
            except Exception:
                pass

    def increment(self, name, amount=1):
        """Atomically increment a counter and persist. Returns new value."""
        if name not in COUNTERS:
            # Allow ad-hoc counters while still persisting them.
            self._counters.setdefault(name, 0)
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + int(amount)
            self._save_locked()
            return self._counters[name]

    def snapshot(self):
        with self._lock:
            return dict(self._counters)

    def reset(self):
        with self._lock:
            self._counters = dict(COUNTERS)
            self._save_locked()

    def summary(self):
        """One-line-ish human-readable summary for notifications."""
        s = self.snapshot()
        return (
            f"Accounts: {s['accounts_linked']} | "
            f"Targets: {s['targets_found']} | "
            f"Wrong: {s['otp_wrong']} | Expired: {s['otp_expired']} | "
            f"Blocked: {s['user_blocked']} | OTP timeout: {s['otp_timeout']} | "
            f"Consumed: {s['numbers_consumed']} | Refunds missing: {s['refunds_missing']}"
        )
