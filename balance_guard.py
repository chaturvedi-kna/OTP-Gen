"""
Balance / refund safety guard.

The automation must never take a new number while money from a previous number
is unaccounted for. After every cancellation the balance is expected to return
to the pre-purchase snapshot (full refund). This module polls the provider
until the refund is reflected in the ledger, or reports a hard mismatch that
the coordinator treats as STOP-EVERYTHING + alert (manual intervention needed).

It also implements the "late OTP" salvage check used both at OTP timeout and
right after a cancel attempt: if the SMS arrived in the cancellation race, the
code is returned so it can be surfaced immediately instead of being lost.
"""

import time


class RefundMismatch(Exception):
    """Raised when a refund never tallied with the expected balance."""

    def __init__(self, message, expected=None, actual=None, activation_id=None, number=None):
        super().__init__(message)
        self.expected = expected
        self.actual = actual
        self.activation_id = activation_id
        self.number = number


class BalanceGuard:

    def __init__(self, config=None, log_fn=print):
        config = config or {}
        self.tolerance = float(config.get("tolerance", 0.5))
        self.refund_wait = float(config.get("refund_wait_seconds", 90))
        self.poll_interval = float(config.get("poll_interval_seconds", 3))
        self.min_balance = float(config.get("min_balance", 5.0))
        self.enabled = bool(config.get("enabled", True))
        self._log = log_fn

    def get_balance(self, client, prefix=""):
        return client.get_balance()

    def verify_refund(self, client, expected_balance, activation_id=None, number=None,
                      stop_event=None, prefix=""):
        """
        Poll the provider balance until it is back at/above expected_balance
        (minus tolerance), or until refund_wait elapses.

        expected_balance may be a number OR a callable returning one. A callable
        is re-evaluated on every poll, because the balance this provider should
        be at keeps moving while we wait: a deferred cancellation is chased in
        the background precisely so the worker can keep hunting, and every
        number it buys meanwhile holds money too. Comparing against the value
        captured when the wait started turns those numbers into a phantom
        "missing refund".

        Returns (ok: bool, actual_balance: float).
        """
        if not self.enabled:
            try:
                return True, float(client.get_balance())
            except Exception:
                return True, None

        def current_expected():
            value = (expected_balance() if callable(expected_balance)
                     else expected_balance)
            try:
                return None if value is None else float(value)
            except (TypeError, ValueError):
                return None

        target = current_expected()
        if target is None:
            # Nothing trustworthy to compare against yet: do not invent one.
            try:
                return True, float(client.get_balance())
            except Exception:
                return True, None

        deadline = time.time() + max(1.0, self.refund_wait)
        last_balance = None
        while True:
            try:
                last_balance = float(client.get_balance())
            except Exception as exc:
                self._log(f"[BALANCE-GUARD] balance fetch failed: {exc}", prefix)
                last_balance = None

            target = current_expected()
            if target is None:
                # The expected balance stopped being computable (a hold became
                # unmeasurable): stop comparing rather than guess.
                self._log("[BALANCE-GUARD] expected balance is no longer "
                          "computable - not tallying this refund.", prefix)
                return True, last_balance

            if last_balance is not None and last_balance >= target - self.tolerance:
                self._log(
                    f"[BALANCE-GUARD] Refund tallied: balance {last_balance:.4f} "
                    f">= expected {target:.4f} (tol {self.tolerance})",
                    prefix
                )
                return True, last_balance

            if stop_event is not None and stop_event.is_set():
                return False, last_balance

            if time.time() >= deadline:
                break
            time.sleep(self.poll_interval)

        self._log(
            f"[BALANCE-GUARD] REFUND MISMATCH after {self.refund_wait:.0f}s: "
            f"expected ~{target:.4f}, actual {last_balance}",
            prefix
        )
        return False, last_balance

    def salvage_late_otp(self, client, activation_id, probes=3, delay=1.5, prefix=""):
        """
        Final check for an OTP that may have landed during the timeout/cancel
        race. Returns the status dict (type STATUS_OK with code/sms) or None.
        """
        for i in range(max(1, probes)):
            try:
                status = client.get_status(activation_id)
            except Exception:
                status = None
            if status and status.get("type") == "STATUS_OK":
                self._log(
                    f"[BALANCE-GUARD] Late OTP salvaged for {activation_id}: "
                    f"{status.get('code')} (probe {i + 1}/{probes})",
                    prefix
                )
                return status
            if i < probes - 1:
                time.sleep(delay)
        return None
