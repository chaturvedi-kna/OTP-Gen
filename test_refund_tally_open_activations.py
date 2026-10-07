"""
Refund tally vs. numbers that are still open (the "460 vs 430" bug).

The refund tally answers one question: "did the money for THIS activation come
back?". It compares the live balance against the balance the provider should be
at once the activation is refunded.

That expected balance used to subtract only the money held by DEFERRED
cancellations. It ignored the money held by numbers that were bought and were
still open - being checked, waiting for the OTP, or waiting out the provider's
cancel window. So a cancel taken while another number was still in play was
always compared against a balance that was one (or several) prices too high,
and the run critical-stopped over money that was simply still in use:

    Expected ~460.0, actual 430.0
    ... still it sits at 460 which should be 440 and minus if any active
    numbers waiting for cancellation in this case 1 active number so 430

These checks drive the real coordinator with a scripted provider:

  * the price of every bought number is measured and booked as a hold,
  * a cancel taken while another number is still open tallies correctly
    (no critical stop),
  * a refund that lands out-of-band (expiry / a manual cancel in the provider
    panel) does not corrupt the hold arithmetic,
  * a refund that genuinely never lands still stops the run,
  * a stop never leaves a paid number open: it is cancelled (or recorded as a
    deferred cancel the next run finishes).

    python test_refund_tally_open_activations.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = tempfile.mkdtemp(prefix="refund_tally_check_")

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


FAILURES = []
PRICE = 10.0


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


class MultiProvider(object):
    """Provider that keeps every order's price and refunds on cancel."""

    def __init__(self, name="otpsell", balance=460.0, price=PRICE,
                 cancel_script=None, max_price=None):
        self.name = name
        self.price = price
        self.balance = balance
        self.max_price = max_price
        self.orders = {}
        self.cancel_script = list(cancel_script or [])
        self.cancel_calls = []
        self.cancel_answers = []
        self.status_calls = []
        self.refunded = []
        self.no_refund = set()
        self._seq = [0]

    def _next_id(self):
        self._seq[0] += 1
        return f"ORD-{self._seq[0]:04d}"

    def get_balance(self):
        return self.balance

    def get_number(self, **kwargs):
        activation_id = self._next_id()
        self.balance = round(self.balance - self.price, 6)
        self.orders[activation_id] = {"price": self.price, "open": True}
        return {"type": "ACCESS_NUMBER", "activation_id": activation_id,
                "number": f"9{self._seq[0]:09d}"}

    def get_status(self, activation_id):
        self.status_calls.append(activation_id)
        return {"type": "STATUS_WAIT_CODE"}

    def cancel(self, activation_id):
        self.cancel_calls.append(activation_id)
        answer = (self.cancel_script.pop(0) if self.cancel_script
                  else {"type": "ACCESS_CANCEL"})
        answer = dict(answer)
        self.cancel_answers.append(answer)
        if answer.get("type") in ("ACCESS_CANCEL", "ACCESS_CANCEL_ALREADY",
                                  "STATUS_CANCEL"):
            order = self.orders.get(activation_id)
            if order and order["open"] and activation_id not in self.no_refund:
                order["open"] = False
                self.balance = round(self.balance + order["price"], 6)
                self.refunded.append(activation_id)
        return answer

    def finish(self, activation_id):
        order = self.orders.get(activation_id)
        if order:
            order["open"] = False
        return {"type": "ACCESS_ACTIVATION"}

    # -- helpers used by the checks ----------------------------------------

    def open_ids(self):
        return [aid for aid, o in self.orders.items() if o["open"]]

    def refund_out_of_band(self, activation_id):
        """The provider refunded it itself (expiry / a manual panel cancel)."""
        order = self.orders.get(activation_id)
        if order and order["open"]:
            order["open"] = False
            self.balance = round(self.balance + order["price"], 6)
            self.refunded.append(activation_id)


SCENARIO_SEQ = [0]


def build_coordinator(**automation):
    SCENARIO_SEQ[0] += 1
    case_dir = os.path.join(SCRATCH_DIR, f"case_{SCENARIO_SEQ[0]}")
    os.makedirs(case_dir, exist_ok=True)
    for stale in os.listdir(case_dir):
        if stale.endswith(".json") or stale.endswith(".tmp"):
            try:
                os.remove(os.path.join(case_dir))
            except OSError:
                pass
    os.chdir(case_dir)
    config = {
        "active_otp_provider": "otpsell",
        "otpsell": {"enabled": True, "api_key": "k", "max_attempts": 3,
                    "service": "meesho", "country": "91",
                    "operator": "server-62", "max_price": PRICE,
                    "cancel_wait_seconds": 120, "timeout": 30},
        "checker": {"mode": "api", "api_keys": ["k"], "service": "meesho"},
        "automation": {
            "max_attempts": 3,
            "refund_check_delay_seconds": 0,
            "cancel_salvage_delay": 0,
            "cancel_salvage_probes": 0,
            "defer_refused_cancels": True,
        },
        "balance_guard": {"enabled": True, "tolerance": 0.5,
                          "refund_wait_seconds": 1,
                          "poll_interval_seconds": 0.1},
        "telegram": {"enabled": False},
        "termux": {"enabled": False},
    }
    config["automation"].update(automation)
    coordinator = m.ParallelAutomationCoordinator(config, provider_override="otpsell")
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


def ledger_invariant(coordinator, client):
    """base - open holds - deferred holds must equal the live balance."""
    open_hold, _u = coordinator._open_hold(client.name)
    deferred_hold, _d = coordinator.pending_cancels.hold_total(client.name)
    base = coordinator.ledger.get(client.name, {}).get("expected_balance")
    if base is None:
        return None
    return round(base - open_hold - deferred_hold, 6), client.get_balance()


# ---------------------------------------------------------------------------
# 1. the reported bug: a cancel taken while another number is still open
# ---------------------------------------------------------------------------

def scenario_cancel_with_another_number_open():
    coordinator = build_coordinator(cancel_error_expiry_seconds=600,
                                    cancel_error_poll_interval_seconds=1)
    # The first cancel is refused (OTPSell's two-minute window); the retry
    # after the window succeeds.
    client = MultiProvider(cancel_script=[{"type": "ACCESS_CANCEL_WAIT",
                                           "seconds": 1},
                                          {"type": "ACCESS_CANCEL"}])
    coordinator.clients = [client]
    coordinator._note_balance("otpsell", 460.0)

    # Number A is bought and its cancel is refused (OTPSell's 2-minute window).
    a = client.get_number()
    coordinator._note_activation("otpsell", a["activation_id"], a["number"])
    coordinator._open_activation(client, a["activation_id"], a["number"])
    check("open: A's price is measured and booked",
          coordinator._open_hold("otpsell") == (PRICE, False),
          coordinator._open_hold("otpsell"))
    check("open: the expected balance already deducts A",
          coordinator._expected_balance("otpsell", a["activation_id"]) == 460.0,
          coordinator._expected_balance("otpsell", a["activation_id"]))

    result = coordinator.handle_cancellation(
        client, a["activation_id"], a["number"], "Already registered on Meesho")
    check("open: A's refused cancel is deferred, not tallied",
          result.get("deferred") is True and not coordinator.stopped, result)
    check("open: A is no longer counted as an open activation",
          coordinator._open_hold("otpsell") == (0.0, False),
          coordinator._open_hold("otpsell"))

    # Number B is bought and is still open (checking / waiting for the OTP).
    b = client.get_number()
    coordinator._note_activation("otpsell", b["activation_id"], b["number"])
    coordinator._open_activation(client, b["activation_id"], b["number"])
    check("open: B holds its price while it is in play",
          coordinator._open_hold("otpsell") == (PRICE, False),
          coordinator._open_hold("otpsell"))

    # The window passes and the watcher retries A's cancel: the refund lands.
    coordinator.pending_cancels.store.update(a["activation_id"],
                                             expiry_at=time.time() - 1)

    ok = wait_until(lambda: a["activation_id"] not in
                    [r["activation_id"] for r in coordinator.pending_cancels.pending()],
                    timeout=20)
    check("open: the watcher finished A's cancellation", ok,
          coordinator.pending_cancels.pending())
    check("open: A's refund tallied with B still open - NO critical stop",
          not coordinator.stopped, coordinator.stopped)
    check("open: A's money is back", a["activation_id"] in client.refunded,
          client.refunded)

    expected, live = ledger_invariant(coordinator, client)
    check("open: the ledger still matches the live balance",
          expected is not None and abs(expected - live) < 0.01,
          f"model {expected} vs live {live}")

    coordinator.stop_requested.set()


# ---------------------------------------------------------------------------
# 2. the reported numbers, end to end
# ---------------------------------------------------------------------------

def scenario_reported_numbers():
    """460 expected, 440 after two spent numbers, 430 with one still open."""
    coordinator = build_coordinator(cancel_error_expiry_seconds=600,
                                    cancel_error_poll_interval_seconds=1)
    client = MultiProvider(cancel_script=[{"type": "ACCESS_CANCEL_WAIT",
                                           "seconds": 1},
                                          {"type": "ACCESS_CANCEL"}])
    coordinator.clients = [client]
    coordinator._note_balance("otpsell", 460.0)

    # The reported number: bought, its cancel refused (deferred), and refunded
    # by the provider itself before our retry (expiry / a manual panel cancel).
    a = client.get_number()
    coordinator._open_activation(client, a["activation_id"], a["number"])
    result = coordinator.handle_cancellation(
        client, a["activation_id"], a["number"],
        "Already registered on Meesho")
    check("numbers: the reported number's cancel is deferred",
          result.get("deferred") is True, result)
    client.refund_out_of_band(a["activation_id"])
    coordinator.pending_cancels.store.update(a["activation_id"],
                                             expiry_at=time.time() - 1)
    ok = wait_until(lambda: a["activation_id"] not in
                    [r["activation_id"] for r in coordinator.pending_cancels.pending()],
                    timeout=20)
    check("numbers: the watcher closes it without a mismatch", ok and
          not coordinator.stopped, coordinator.stopped)
    check("numbers: the baseline is back at 460",
          coordinator.ledger["otpsell"]["expected_balance"] == 460.0,
          coordinator.ledger["otpsell"]["expected_balance"])

    # Two more numbers are bought and spent for real (SMS delivered: no refund).
    for _ in range(2):
        spent = client.get_number()
        coordinator._open_activation(client, spent["activation_id"], spent["number"])
        client.no_refund.add(spent["activation_id"])
        coordinator.handle_cancellation(
            client, spent["activation_id"], spent["number"],
            "bot_wrong_otp", expect_refund=False)
    check("numbers: two spent numbers re-baseline the ledger to 440",
          coordinator.ledger["otpsell"]["expected_balance"] == 440.0,
          coordinator.ledger["otpsell"]["expected_balance"])

    # One more number is bought and is still open.
    live = client.get_number()
    coordinator._open_activation(client, live["activation_id"], live["number"])
    check("numbers: with one number still open the expected balance is 430",
          coordinator._expected_balance("otpsell", None) == 430.0,
          coordinator._expected_balance("otpsell", None))
    check("numbers: ...and the live balance agrees",
          client.get_balance() == 430.0, client.get_balance())
    check("numbers: no critical stop was raised on the way",
          not coordinator.stopped, coordinator.stopped)

    coordinator.stop_requested.set()


# ---------------------------------------------------------------------------
# 3. an out-of-band refund must not corrupt the hold arithmetic
# ---------------------------------------------------------------------------

def scenario_out_of_band_refund():
    coordinator = build_coordinator(cancel_error_expiry_seconds=600,
                                    cancel_error_poll_interval_seconds=1)
    client = MultiProvider(cancel_script=[{"type": "ACCESS_CANCEL_WAIT",
                                           "seconds": 1},
                                          {"type": "ACCESS_CANCEL"}])
    coordinator.clients = [client]
    coordinator._note_balance("otpsell", 460.0)

    a = client.get_number()
    coordinator._open_activation(client, a["activation_id"], a["number"])
    result = coordinator.handle_cancellation(
        client, a["activation_id"], a["number"], "Already registered on Meesho")
    check("out-of-band: A's cancel is deferred", result.get("deferred") is True, result)

    b = client.get_number()
    coordinator._open_activation(client, b["activation_id"], b["number"])

    # The provider refunds A itself (expiry, or a manual cancel in the panel).
    client.refund_out_of_band(a["activation_id"])
    coordinator.pending_cancels.store.update(a["activation_id"],
                                             expiry_at=time.time() - 1)

    ok = wait_until(lambda: a["activation_id"] not in
                    [r["activation_id"] for r in coordinator.pending_cancels.pending()],
                    timeout=20)
    check("out-of-band: the watcher still closes A", ok,
          coordinator.pending_cancels.pending())
    check("out-of-band: money that came back on its own is not a mismatch",
          not coordinator.stopped, coordinator.stopped)
    check("out-of-band: the ledger re-anchors on the live balance",
          abs(coordinator.ledger["otpsell"]["expected_balance"]
              - client.get_balance() - PRICE) < 0.01,
          f"base {coordinator.ledger['otpsell']['expected_balance']}, "
          f"live {client.get_balance()}")

    coordinator.stop_requested.set()


# ---------------------------------------------------------------------------
# 4. a refund that really never lands must still stop the run
# ---------------------------------------------------------------------------

def scenario_real_missing_refund_still_stops():
    coordinator = build_coordinator(cancel_error_expiry_seconds=600,
                                    cancel_error_poll_interval_seconds=1)
    client = MultiProvider(cancel_script=[{"type": "ACCESS_CANCEL_WAIT",
                                           "seconds": 1}] * 20)
    coordinator.clients = [client]
    coordinator._note_balance("otpsell", 460.0)

    a = client.get_number()
    coordinator._open_activation(client, a["activation_id"], a["number"])
    result = coordinator.handle_cancellation(
        client, a["activation_id"], a["number"], "Already registered on Meesho")
    check("missing refund: the cancel is deferred first", result.get("deferred") is True,
          result)

    # The window passes but the provider never refunds: a real discrepancy.
    coordinator.pending_cancels.store.update(a["activation_id"],
                                             expiry_at=time.time() - 1)
    ok = wait_until(lambda: bool(coordinator.stopped), timeout=25)
    check("missing refund: the run still critical-stops when money is lost", ok,
          coordinator.stopped)
    check("missing refund: the alert names the activation",
          any(a["activation_id"] in msg for _t, msg in coordinator.stopped),
          coordinator.stopped)
    coordinator.stop_requested.set()


# ---------------------------------------------------------------------------
# 5. a stop must never leave a paid number open
# ---------------------------------------------------------------------------

def scenario_stop_closes_open_numbers():
    coordinator = build_coordinator()
    client = MultiProvider()
    coordinator.clients = [client]
    coordinator._note_balance("otpsell", 460.0)

    a = client.get_number()
    coordinator._open_activation(client, a["activation_id"], a["number"])
    b = client.get_number()
    coordinator._open_activation(client, b["activation_id"], b["number"])

    coordinator._drain_open_activations()

    check("drain: every open number was cancelled",
          sorted(client.cancel_calls) == sorted([a["activation_id"],
                                                 b["activation_id"]]),
          client.cancel_calls)
    check("drain: the money came back",
          client.open_ids() == [], client.open_ids())
    check("drain: nothing is left registered as open",
          coordinator._open_hold("otpsell") == (0.0, False),
          coordinator._open_hold("otpsell"))
    check("drain: no critical stop is raised by the drain itself",
          not coordinator.stopped, coordinator.stopped)


def scenario_stop_defers_a_refused_cancel():
    """OTPSell refuses a cancel inside its 2-minute window: park it, don't drop it."""
    coordinator = build_coordinator()
    client = MultiProvider(cancel_script=[{"type": "ACCESS_CANCEL_WAIT",
                                           "seconds": 120}] * 5)
    coordinator.clients = [client]
    coordinator._note_balance("otpsell", 460.0)

    a = client.get_number()
    coordinator._open_activation(client, a["activation_id"], a["number"])

    coordinator._drain_open_activations()

    pending = [r["activation_id"] for r in coordinator.pending_cancels.pending()]
    check("drain: a cancel refused by the window is parked for the next run",
          pending == [a["activation_id"]], pending)
    check("drain: the number is no longer counted as open here",
          coordinator._open_hold("otpsell") == (0.0, False),
          coordinator._open_hold("otpsell"))
    check("drain: the user is told what is still held",
          any("still open" in title.lower() for title, _ in coordinator.alerts),
          coordinator.alerts)
    coordinator.stop_requested.set()


# ---------------------------------------------------------------------------
# 6. an unmeasurable price suspends the tally instead of guessing
# ---------------------------------------------------------------------------

def scenario_unmeasurable_price_suspends_tally():
    coordinator = build_coordinator()
    client = MultiProvider()
    coordinator.clients = [client]
    # No baseline at all: the price of the first number cannot be derived.
    a = client.get_number()
    coordinator._open_activation(client, a["activation_id"], a["number"])

    check("unknown price: the hold is flagged as unknown",
          coordinator._open_hold("otpsell") == (0.0, True),
          coordinator._open_hold("otpsell"))
    check("unknown price: the refund tally is suspended, not guessed",
          coordinator._tally_is_suspended("otpsell"), coordinator._tally_suspended)
    result = coordinator.handle_cancellation(
        client, a["activation_id"], a["number"], "Already registered on Meesho")
    check("unknown price: the cancel still completes without a critical stop",
          not coordinator.stopped, coordinator.stopped)
    check("unknown price: the ledger is re-baselined instead of guessed",
          coordinator.ledger["otpsell"]["expected_balance"] == client.get_balance(),
          f"ledger {coordinator.ledger['otpsell']['expected_balance']}, "
          f"live {client.get_balance()}")
    check("unknown price: the refund itself still landed",
          a["activation_id"] in client.refunded, client.refunded)
    coordinator.stop_requested.set()


def main():
    print("=" * 70)
    print("Refund tally vs. open activations")
    print("=" * 70)
    try:
        scenario_cancel_with_another_number_open()
        scenario_reported_numbers()
        scenario_out_of_band_refund()
        scenario_real_missing_refund_still_stops()
        scenario_stop_closes_open_numbers()
        scenario_stop_defers_a_refused_cancel()
        scenario_unmeasurable_price_suspends_tally()
    finally:
        shutil.rmtree(SCRATCH_DIR, ignore_errors=True)

    print("=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All refund tally checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
