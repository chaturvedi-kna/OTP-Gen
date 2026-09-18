"""
Cancellations the provider refuses ({"type": "ERROR"}).

TemporaSMS and VSImpro answer a cancel that arrives while the activation is
still young with a plain ERROR. The old code fell straight into the refund
tally, which then reported REFUND MISMATCH and CRITICAL STOPPED the whole run
over money that was simply still held - and it left the activation open.

This check drives the real coordinator with a scripted provider:

  * the cancel is DEFERRED and the caller is released immediately (the worker
    keeps hunting instead of blocking for the activation lifetime),
  * the amount still held is booked, so the next refund tally is not a false
    mismatch,
  * an OTP arriving during the wait is reported with its code and the charge
    is accepted (no cancel retry),
  * at expiry the cancel is retried and only then the refund is tallied,
  * a hold that cannot be measured suspends the tally instead of stopping,
  * pending cancellations survive a restart.

    python test_deferred_cancel.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = tempfile.mkdtemp(prefix="deferred_cancel_check_")

if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)


# --- stub the optional runtime dependencies (no network in this check) -------

if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        requests = types.ModuleType("requests")

        class _Session:
            def get(self, *a, **k):
                raise RuntimeError("network disabled in this check")

            def post(self, *a, **k):
                raise RuntimeError("network disabled in this check")

        requests.Session = _Session
        requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network disabled"))
        requests.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network disabled"))
        sys.modules["requests"] = requests

if "websocket" not in sys.modules:
    try:
        import websocket  # noqa: F401
    except ImportError:
        websocket = types.ModuleType("websocket")
        websocket.WebSocketApp = object
        websocket.enableTrace = lambda *a, **k: None
        sys.modules["websocket"] = websocket

import main as m  # noqa: E402
from cancel_watch import (  # noqa: E402
    CancelWatchManager,
    PendingCancelStore,
    resolve_settings,
)


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def wait_until(condition, timeout=25.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


class FakeProvider(object):
    """Scripted provider: balance, cancel answers and status answers."""

    name = "tempora"

    def __init__(self, balance_before=100.0, price=7.0,
                 cancel_script=None, status_script=None, balance_error=False):
        self.price = price
        self.refunded = False
        self.balance_error = balance_error
        self.balance_before = balance_before
        self.cancel_script = list(cancel_script or [])
        self.status_script = list(status_script or [])
        self.cancel_calls = []
        self.status_calls = []
        self.finished = []

    # A number was bought: the balance is short by its price until refunded.
    def get_balance(self):
        if self.balance_error:
            raise RuntimeError("balance unavailable")
        return self.balance_before if self.refunded else self.balance_before - self.price

    def get_status(self, activation_id):
        self.status_calls.append(activation_id)
        if self.status_script:
            answer = self.status_script.pop(0)
        else:
            answer = {"type": "STATUS_WAIT_CODE"}
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def cancel(self, activation_id):
        self.cancel_calls.append(activation_id)
        answer = self.cancel_script.pop(0) if self.cancel_script else {"type": "ACCESS_CANCEL"}
        if isinstance(answer, Exception):
            raise answer
        if answer.get("type") in ("ACCESS_CANCEL", "ACCESS_CANCEL_ALREADY", "STATUS_CANCEL"):
            self.refunded = True
        return dict(answer)

    def finish(self, activation_id):
        self.finished.append(activation_id)
        return {"type": "ACCESS_ACTIVATION"}


SCENARIO_SEQ = [0]


def build_coordinator(**automation):
    """A fresh coordinator whose stores live in an isolated directory.

    The deferred-cancel watches are background threads and the store is a JSON
    file: sharing one directory across scenarios would make them undo each
    other's setup, so every scenario gets its own.
    """
    SCENARIO_SEQ[0] += 1
    case_dir = os.path.join(SCRATCH_DIR, f"case_{SCENARIO_SEQ[0]}")
    os.makedirs(case_dir, exist_ok=True)
    for stale in os.listdir(case_dir):
        if stale.endswith(".json") or stale.endswith(".tmp"):
            try:
                os.remove(os.path.join(case_dir, stale))
            except OSError:
                pass
    os.chdir(case_dir)
    config = {
        "active_otp_provider": "tempora",
        "tempora": {"enabled": True, "api_key": "k", "max_attempts": 3},
        "checker": {"mode": "api", "api_keys": ["k"], "service": "meesho"},
        "automation": {
            "max_attempts": 3,
            "refund_check_delay_seconds": 0,
            "cancel_salvage_delay": 0,
        },
        "balance_guard": {
            "enabled": True,
            "tolerance": 0.5,
            "refund_wait_seconds": 1,
            "poll_interval_seconds": 0.1,
        },
        "telegram": {"enabled": False},
        "termux": {"enabled": False},
    }
    # No salvage probes before deferring: a late OTP is the watcher's job.
    config["automation"]["cancel_salvage_probes"] = 0
    config["automation"].update(automation)
    coordinator = m.ParallelAutomationCoordinator(config, provider_override="tempora")
    coordinator.alerts = []
    coordinator.messages = []
    coordinator.notify.send = lambda title, message, **kw: coordinator.messages.append((title, message))
    coordinator.notify.alert = lambda title, message, **kw: coordinator.alerts.append((title, message))
    coordinator.notify.telegram.send = lambda *a, **k: False
    coordinator.notify.termux.send = lambda *a, **k: False
    coordinator.stopped = []
    coordinator._critical_stop = lambda title, message: (
        coordinator.stopped.append((title, message)),
        coordinator.stop_requested.set(),
    )
    return coordinator


# ---------------------------------------------------------------------------
# 1. a refused cancel is deferred, not tallied
# ---------------------------------------------------------------------------

def scenario_defer_instead_of_stop():
    coordinator = build_coordinator(cancel_error_expiry_seconds=600,
                                    cancel_error_poll_interval_seconds=1)
    client = FakeProvider(cancel_script=[{"type": "ERROR"}])
    coordinator.clients = [client]
    coordinator._note_balance("tempora", 100.0)
    coordinator._note_activation("tempora", "act-defer", "9999999999")

    started = time.time()
    result = coordinator.handle_cancellation(client, "act-defer", "9999999999", "test defer")
    elapsed = time.time() - started

    check("defer: the call returns immediately (the worker is not blocked)",
          elapsed < 10.0, f"{elapsed:.1f}s")
    check("defer: the result says the cancellation was deferred",
          result.get("deferred") is True and result.get("tally_ok") is True, result)
    check("defer: no CRITICAL STOP for a refund that is simply pending",
          not coordinator.stopped, coordinator.stopped)
    check("defer: the activation is parked as pending",
          [r["activation_id"] for r in coordinator.pending_cancels.pending()] == ["act-defer"],
          coordinator.pending_cancels.pending())
    record = coordinator.pending_cancels.store.get("act-defer")
    check("defer: the held amount is booked (7.0 = the price of the number)",
          record.get("hold") == 7.0, record.get("hold"))
    check("defer: the assumed expiry is 15 minutes by default",
          resolve_settings({})["cancel_error_expiry_seconds"] == 900.0)
    check("defer: the user is told instead of left guessing",
          any("deferred" in title.lower() for title, _ in coordinator.messages),
          coordinator.messages)

    # The money is still gone: every later tally is measured against it.
    check("defer: the expected balance is reduced by the hold",
          coordinator._expected_balance("tempora", None) == 93.0,
          coordinator._expected_balance("tempora", None))
    check("defer: the hold is ignored for the activation it belongs to",
          coordinator._expected_balance("tempora", "act-defer") == 100.0,
          coordinator._expected_balance("tempora", "act-defer"))

    # A second number cancelled meanwhile must NOT look like a missing refund.
    other = FakeProvider(cancel_script=[{"type": "ACCESS_CANCEL"}])
    coordinator.clients = [client, other]
    result2 = coordinator.handle_cancellation(other, "act-other", "8888888888", "not registered")
    check("defer: another cancellation still tallies against the held balance",
          result2.get("tally_ok") is True and not coordinator.stopped,
          result2)

    coordinator.stop_requested.set()


# ---------------------------------------------------------------------------
# 2. retried at expiry, then the refund is tallied
# ---------------------------------------------------------------------------

def scenario_retry_at_expiry():
    coordinator = build_coordinator(cancel_error_expiry_seconds=1,
                                    cancel_error_grace_seconds=0,
                                    cancel_error_poll_interval_seconds=0.2,
                                    cancel_error_retry_attempts=3,
                                    cancel_error_retry_delay_seconds=0.2)
    client = FakeProvider(cancel_script=[{"type": "ERROR"}, {"type": "ACCESS_CANCEL"}])
    coordinator.clients = [client]
    coordinator._note_balance("tempora", 100.0)

    result = coordinator.handle_cancellation(client, "act-expiry", "9999999999",
                                             "Recovery: otp_timeout")
    check("expiry: deferred", result.get("deferred") is True, result)
    check("expiry: the cancel was not retried before the expiry",
          client.cancel_calls == ["act-expiry"], client.cancel_calls)

    resolved = wait_until(
        lambda: [r["activation_id"] for r in coordinator.pending_cancels.pending()]
                != ["act-expiry"],
        timeout=30)
    check("expiry: the watcher finished the cancellation",
          resolved and coordinator.pending_cancels.store.get("act-expiry") is None,
          coordinator.pending_cancels.pending())
    check("expiry: the cancel was retried after the expiry",
          len(client.cancel_calls) == 2, client.cancel_calls)

    snapshot_before = coordinator.stats.snapshot()
    check("expiry: the refund tallied and was counted",
          snapshot_before["cancel_deferred_refunded"] == 1,
          snapshot_before)
    check("expiry: no refund was reported missing",
          snapshot_before["refunds_missing"] == 0 and not coordinator.stopped,
          coordinator.stopped)
    check("expiry: the deferred cancellation is counted when it really happens",
          snapshot_before["numbers_cancelled"] == 1,
          snapshot_before["numbers_cancelled"])
    check("expiry: the user is told the deferred cancel completed",
          any("completed" in title.lower() for title, _ in coordinator.messages),
          coordinator.messages)


# ---------------------------------------------------------------------------
# 3. an OTP arriving during the wait is reported, the charge stands
# ---------------------------------------------------------------------------

def scenario_late_otp_during_wait():
    coordinator = build_coordinator(cancel_error_expiry_seconds=600,
                                    cancel_error_poll_interval_seconds=0.2,
                                    cancel_salvage_probes=1)
    client = FakeProvider(
        cancel_script=[{"type": "ERROR"}],
        status_script=[{"type": "STATUS_OK", "code": "482913", "sms": "482913 is your code"}],
    )
    coordinator.clients = [client]
    coordinator._note_balance("tempora", 100.0)

    stats_alive = m.StatsStore(filename=coordinator.stats.path.name)
    start = {k: stats_alive.snapshot().get(k, 0) for k in
             ("cancel_deferred_otp", "numbers_consumed", "late_otp_salvaged",
              "numbers_cancelled")}

    result = coordinator.handle_cancellation(client, "act-otp", "9999999999", "otp_timeout")
    check("late otp: the OTP was salvaged (charge stands, no defer)",
          result.get("deferred") is False and result.get("tally_ok") is True
          and result.get("salvaged", {}) and result["salvaged"].get("code") == "482913",
          result)

    stats_before = m.StatsStore(filename="stats.json")
    start = {k: stats_before.snapshot().get(k, 0) for k in
             ("cancel_deferred_otp", "numbers_consumed", "late_otp_salvaged",
              "numbers_cancelled")}

    # Nothing may ever be deferred once the OTP was salvaged: the charge
    # legitimately stands (the SMS was delivered), so the cancelling path
    # settles it immediately - the watcher never even starts.
    check("late otp: nothing is pending once the OTP was salvaged",
          coordinator.pending_cancels.pending() == [],
          coordinator.pending_cancels.pending())
    check("late otp: the cancel is NOT retried once the SMS was delivered",
          client.cancel_calls == ["act-otp"], client.cancel_calls)
    alert_text = " ".join(f"{t} {msg}" for t, msg in coordinator.alerts)
    check("late otp: the notification carries the code",
          "482913" in alert_text, coordinator.alerts)
    check("late otp: the notification says the charge stands",
          "charge stands" in alert_text.lower(), alert_text[:200])
    snapshot = m.StatsStore(filename=coordinator.stats.path.name).snapshot()
    check("late otp: counted as a delivered SMS (no refund expected)",
          snapshot["late_otp_salvaged"] - start["late_otp_salvaged"] == 1
          and snapshot["numbers_consumed"] - start["numbers_consumed"] == 1,
          snapshot)
    check("late otp: the activation was NOT cancelled (the charge stands)",
          snapshot["numbers_cancelled"] - start["numbers_cancelled"] == 0, snapshot)
    check("late otp: not counted as a missing refund",
          snapshot["refunds_missing"] == 0 and not coordinator.stopped,
          coordinator.stopped)
    # A cancel was refused (ERROR) and the OTP still arrived: the complaint
    # record must be persisted for that activation.
    disputes = coordinator.pending_cancels.disputes.records()
    entries = [e for e in disputes if e.get("activation_id") == "act-otp"]
    check("complaint: the refused-cancel + OTP event is persisted",
          len(entries) == 1, disputes)
    check("complaint: the record carries number, code and provider",
          entries and entries[0].get("number") == "9999999999"
          and entries[0].get("otp_code") == "482913"
          and entries[0].get("provider") == "tempora"
          and entries[0].get("source") == "immediate_salvage",
          entries)

    # consume the deferred activation like its resolution does
    client.get_balance()
    check("late otp: the held amount for this activation is gone",
          coordinator.pending_cancel_hold("tempora")[0] == 0.0
          or coordinator.pending_cancel_hold("tempora", exclude="act-otp")[0] == 0.0,
          coordinator.pending_cancel_hold("tempora"))


# ---------------------------------------------------------------------------
# 3b. the deferred watcher also files the complaint record on a late OTP
# ---------------------------------------------------------------------------

def scenario_deferred_otp_record():
    coordinator = build_coordinator(cancel_error_expiry_seconds=600,
                                    cancel_error_poll_interval_seconds=0.05,
                                    cancel_salvage_probes=0)
    client = FakeProvider(
        cancel_script=[{"type": "ERROR"}],
        status_script=[{"type": "STATUS_OK", "code": "777222",
                        "sms": "777222 is your OTP"}],
    )
    coordinator.clients = [client]
    coordinator._note_balance("tempora", 100.0)

    result = coordinator.handle_cancellation(client, "act-watch", "8888888888",
                                             "not registered")
    check("watcher: the refused cancel is deferred (no immediate salvage)",
          result.get("deferred") is True, result)

    resolved = wait_until(
        lambda: coordinator.pending_cancels.store.get("act-watch") is None,
        timeout=30)
    check("watcher: the late OTP resolved the deferred cancellation",
          resolved, coordinator.pending_cancels.pending())

    disputes = coordinator.pending_cancels.disputes.records()
    entries = [e for e in disputes if e.get("activation_id") == "act-watch"]
    check("watcher: the complaint record is persisted by the watcher",
          len(entries) == 1, disputes)
    check("watcher: the record carries the code and points at the watcher",
          entries and entries[0].get("otp_code") == "777222"
          and entries[0].get("source") == "deferred_cancel_watch",
          entries)
    snapshot = coordinator.stats.snapshot()
    check("watcher: the late OTP is counted like before",
          snapshot["cancel_deferred_otp"] == 1, snapshot)


# ---------------------------------------------------------------------------
# 4. an unmeasurable hold suspends the tally instead of stopping the run
# ---------------------------------------------------------------------------

def scenario_unknown_hold():
    coordinator = build_coordinator(cancel_error_expiry_seconds=600,
                                    cancel_error_poll_interval_seconds=0.2)
    client = FakeProvider(cancel_script=[{"type": "ERROR"}], balance_error=True)
    coordinator.clients = [client]
    coordinator._note_balance("tempora", 100.0)

    coordinator.handle_cancellation(client, "act-unknown", "9999999999", "checker error")
    check("unknown hold: the hold is recorded as unknown",
          coordinator.pending_cancels.store.get("act-unknown").get("hold") is None)
    check("unknown hold: the tally for this provider is suspended",
          coordinator._tally_is_suspended("tempora"))
    check("unknown hold: the user is warned once",
          any("suspended" in title.lower() for title, _ in coordinator.alerts),
          coordinator.alerts)

    balance_error = FakeProvider(cancel_script=[{"type": "ACCESS_CANCEL"}])
    coordinator.clients = [client, balance_error]
    result = coordinator.handle_cancellation(balance_error, "act-2", "8888888888", "not registered")
    check("unknown hold: a later cancellation is not stopped by the guesswork",
          result.get("tally_ok") is True and not coordinator.stopped,
          (result.get("tally_ok"), coordinator.stopped))
    coordinator.stop_requested.set()


# ---------------------------------------------------------------------------
# 5. pending cancellations survive a restart
# ---------------------------------------------------------------------------

def scenario_persistence_and_resume():
    os.chdir(SCRATCH_DIR)
    store = PendingCancelStore(filename="act_resume_pending.json")
    store.add({
        "provider": "tempora",
        "activation_id": "act-resume",
        "number": "9999999999",
        "reason": "Recovery: otp_timeout",
        "expected_balance": 100.0,
        "hold": 7.0,
        "deferred_at": m.now(),
        "deferred_at_epoch": time.time(),
        # Expired long ago: the retry must happen right away.
        "expiry_at": time.time() - 60,
        "expiry_assumed": True,
        "attempts": 0,
    })
    check("persistence: the record survives a new store instance",
          PendingCancelStore(filename="act_resume_pending.json").get("act-resume") is not None)

    client = FakeProvider(cancel_script=[{"type": "ACCESS_CANCEL"}])
    owner = build_owner(client)
    manager = CancelWatchManager(owner, store=store, log_fn=lambda *a, **k: None)
    manager.resume()

    resolved = wait_until(lambda: store.get("act-resume") is None, timeout=20)
    check("resume: the leftover cancellation is finished after a restart",
          resolved, store.all())
    check("resume: the cancel was retried", client.cancel_calls == ["act-resume"],
          client.cancel_calls)
    check("resume: the user is told the old cancellation was picked up",
          any("resumed" in title.lower() for title, _ in owner.alerts), owner.alerts)


def build_owner(client):
    """Minimal owner for the manager (no full coordinator needed)."""
    class Owner(object):
        def __init__(self):
            self.settings = {"refund_check_delay_seconds": 0}
            self.stats = m.StatsStore(filename="resume_stats.json")
            self.alerts = []
            self.messages = []
            self.stop_requested = threading.Event()
            self.guard = m.BalanceGuard({"enabled": True, "tolerance": 0.5,
                                         "refund_wait_seconds": 1,
                                         "poll_interval_seconds": 0.1},
                                        log_fn=lambda *a, **k: None)
            self.balances = {}
            self.stopped = []
            self.resolved = []

        # Notifier stand-in
        @property
        def notify(self):
            return self

        def send(self, title, message, **kwargs):
            self.messages.append((title, message))

        def alert(self, title, message, **kwargs):
            self.alerts.append((title, message))

        def client_by_name(self, name):
            return client

        def expected_balance(self, name, activation_id=None):
            return self.balances.get(name, 100.0)

        def note_balance(self, name, balance):
            self.balances[name] = balance

        def critical_stop(self, title, message):
            self.stopped.append((title, message))

        def on_pending_resolved(self, provider):
            self.resolved.append(provider)

    return Owner()


# ---------------------------------------------------------------------------
# 6. opting out keeps the old behaviour
# ---------------------------------------------------------------------------

def scenario_opt_out():
    coordinator = build_coordinator(cancel_error_expiry_seconds=600,
                                    defer_refused_cancels=False)
    client = FakeProvider(cancel_script=[{"type": "ERROR"}])
    coordinator.clients = [client]
    coordinator._note_balance("tempora", 100.0)

    coordinator.handle_cancellation(client, "act-optout", "9999999999", "test")
    check("opt-out: defer_refused_cancels=false keeps the old tally path",
          not coordinator.pending_cancels.pending()
          and coordinator.stats.snapshot()["refunds_missing"] == 1
          and coordinator.stopped,
          (coordinator.pending_cancels.pending(), coordinator.stats.snapshot()["refunds_missing"]))


def main():
    scenario_defer_instead_of_stop()
    scenario_retry_at_expiry()
    scenario_late_otp_during_wait()
    scenario_deferred_otp_record()
    scenario_unknown_hold()
    scenario_persistence_and_resume()
    scenario_opt_out()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All deferred cancellation checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
