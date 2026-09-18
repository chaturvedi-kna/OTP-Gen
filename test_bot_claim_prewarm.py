"""
Two things that must never throw a paid number away:

Change 2 - a number check must never walk the PRIMES bot out of a live login:
  while number A is waiting for its OTP, the check for number B would
  previously walk the bot back to its main menu and type B into the checker
  prompt - the OTP screen was gone, the OTP could not be entered (not even by
  hand) and the money was wasted. The bot conversation is now claimed
  exclusively: a login owns it, so a check that arrives meanwhile is refused
  ("bot busy") and the number is cancelled with a refund instead.

Change 5 - the offer is pre-warmwared while the workers are still hunting:
  the bot is parked on an agreed offer (UPI <= target) BEFORE any number
  exists, so the number that is found is typed into a screen that is already
  waiting for it instead of paying for Add Account -> ... -> offer rerolls
  while its OTP window runs.

    python test_bot_claim_prewarm.py
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
SCRATCH_DIR = tempfile.mkdtemp(prefix="bot_claim_prewarm_check_")

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
from checker_router import (  # noqa: E402
    BotChecker,
    BotBusy,
    CheckerBotBusy,
    CheckerRouter,
)
from checker_client import CheckerUnavailable, CheckerServiceDown  # noqa: E402
# (CheckerServiceDown re-exported for parity with other test suites)
from meesho_bot_client import MeeshoBotError  # noqa: E402

# Reuse the fake Telethon bot / screens from the referral-flow replay so the
# pre-warm is exercised against the real MeeshoBotClient.
import test_primes_referral_flow as replay  # noqa: E402


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


# ---------------------------------------------------------------------------
# Change 5: the bot client parks itself on an agreed offer without a number
# ---------------------------------------------------------------------------

def scenario_prepare_offer():
    # Cold start: full walk to an offer that fits the target.
    client, bot = replay.build_client(referral_link=None, referral_script="absent")
    res = client.prepare_offer()
    check("prewarm: the bot reaches an agreed offer without a number",
          res.get("stage") == "offer", res)
    check("prewarm: the agreed price is at/below the target",
          res.get("upi") is not None and res["upi"] <= client.target_upi_price, res)
    check("prewarm: no number was typed anywhere",
          not bot.sent_numbers, bot.sent_numbers)
    check("prewarm: the bot is parked on the number prompt",
          client.screen_state() in ("offer", "unknown"), client.screen_state())

    # A found number is then typed straight into the parked prompt.
    res2 = client.prepare_login("9876543210")
    check("prewarm: the next login reuses the parked offer",
          res2.get("stage") == "otp_sent" and res2.get("reused_prompt") is True, res2)
    check("prewarm: the number finally goes out",
          bot.sent_numbers == ["9876543210"], bot.sent_numbers)
    check("prewarm: the parked price is carried into the login result",
          res2.get("upi") == res.get("upi"), (res2.get("upi"), res.get("upi")))

    # Pre-warm again on an offer that is already good: a no-op.
    client2, bot2 = replay.build_client(referral_link=None, referral_script="absent")
    res2 = client2.prepare_offer()
    taps_before = list(bot2.tapped)
    res3 = client2.prepare_offer()
    check("prewarm: already parked - no extra menu walk",
          res3.get("stage") == "offer" and not bot2.tapped[len(taps_before):],
          (res3, bot2.tapped, bot2.tapped[len(taps_before):]))
    check("prewarm: still no number typed",
          not bot2.sent_numbers, bot2.sent_numbers)


# ---------------------------------------------------------------------------
# Change 2: a login owns the PRIMES bot; a check is refused meanwhile
# ---------------------------------------------------------------------------

def _make_coordinator(bot_client=None):
    config = {
        "active_otp_provider": "tempora",
        "tempora": {"enabled": True, "api_key": "k", "max_attempts": 3},
        "checker": {"mode": "bot", "service": "meesho", "bot": {"claim_timeout_seconds": 0}},
        "automation": {"max_attempts": 3, "refund_check_delay_seconds": 0,
                       "prewarm_offer": True},
        "balance_guard": {"enabled": True, "tolerance": 0.5,
                          "refund_wait_seconds": 1, "poll_interval_seconds": 0.1},
        "telegram": {"enabled": False},
        "termux": {"enabled": False},
    }
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    coordinator = m.ParallelAutomationCoordinator(config, provider_override="tempora")
    if bot_client is not None:
        coordinator.bot = bot_client
    return coordinator


def _ready_bot_client():
    client, _bot = replay.build_client(referral_link=None, referral_script="absent")
    # The fake client never goes through start(); mark the session as present
    # so .ready is True for the coordinator's gate.
    client._session_string = lambda: "fake-session"
    return client


def scenario_gate_blocks_during_login():
    os.chdir(SCRATCH_DIR)
    coordinator = _make_coordinator(_ready_bot_client())
    check("gate: bot is free for a check when nothing is running",
          coordinator._bot_check_allowed()[0] is True,
          coordinator._bot_check_allowed())

    # A login claims the bot (the coordinator's _process_target does this).
    with coordinator._bot_login_claim():
        allowed, reason = coordinator._bot_check_allowed()
        check("gate: a bot check is refused while a login owns the bot",
              allowed is False, (allowed, reason))
        check("gate: the reason mentions the login",
              "login" in reason.lower(), reason)

    check("gate: the bot is free again once the login is done",
          coordinator._bot_check_allowed()[0] is True)

    # A target waiting to be processed blocks a check too.
    coordinator.target_found_event.set()
    allowed, reason = coordinator._bot_check_allowed()
    check("gate: a queued target blocks the bot check",
          allowed is False, (allowed, reason))
    coordinator.target_found_event.clear()


def scenario_dedicated_check_refused_during_login():
    os.chdir(SCRATCH_DIR)
    coordinator = _make_coordinator(_ready_bot_client())
    bot_checker = coordinator.checker.bot

    # Outside a login: the (stubbed) check runs.
    check("claim: the bot checker exists and is ready",
          bot_checker.ready, bot_checker.unavailable_reason)

    with coordinator._bot_login_claim():
        try:
            bot_checker.check("9876543210")
            raise AssertionError("the check should have been refused")
        except CheckerBotBusy as exc:
            check("claim: a check during a login raises CheckerBotBusy",
                  "login" in str(exc).lower(), exc)
        except Exception as exc:
            check("claim: a check during a login raises CheckerBotBusy", False,
                  f"{type(exc).__name__}: {exc}")

    # The refusal must be a "busy" failure, not a broken checker.
    from checker_client import CheckerClient  # noqa: F401
    check("claim: CheckerBotBusy is still a CheckerUnavailable (refund, not cancel)",
          issubclass(CheckerBotBusy, CheckerUnavailable))


def scenario_worker_cancels_without_counting_failure():
    """A "bot busy" check cancels the number with a refund - not a streak."""
    os.chdir(SCRATCH_DIR)

    config = {
        "active_otp_provider": "tempora",
        "tempora": {"enabled": True, "api_key": "k", "max_attempts": 3},
        "checker": {"mode": "bot", "service": "meesho", "bot": {"claim_timeout_seconds": 0}},
        "automation": {"max_attempts": 3, "refund_check_delay_seconds": 0,
                       "prewarm_offer": False},
        "balance_guard": {"enabled": True, "tolerance": 0.5,
                          "refund_wait_seconds": 1, "poll_interval_seconds": 0.1},
        "telegram": {"enabled": False},
        "termux": {"enabled": False},
    }

    provider = FakeNumberProvider()

    coordinator = m.ParallelAutomationCoordinator(config, provider_override="tempora")
    coordinator.clients = [provider]
    coordinator.checker.mode = "bot"
    coordinator.checker.bot._client = coordinator.bot = _ready_bot_client()
    coordinator.checker.bot._bot_getter = lambda: coordinator.bot
    cancelled = []
    coordinator.notify.send = lambda *a, **k: None
    coordinator.notify.alert = lambda *a, **k: None
    coordinator.notify.telegram.send = lambda *a, **k: False
    coordinator.notify.termux.send = lambda *a, **k: False
    coordinator.stopped = []
    coordinator._critical_stop = lambda t, msg: (
        coordinator.stopped.append((t, msg)), coordinator.stop_requested.set())

    # A login owns the bot: the worker's check is refused and cancelled.
    with coordinator._bot_login_claim():
        # Patch provider so the worker loop runs once then exits.
        provider._numbers = ["9999999999"]
        provider._cancelled = cancelled
        worker_thread = threading.Thread(
            target=coordinator.worker_loop, args=(provider,),
            name="test-worker-tempora", daemon=True)
        worker_thread.start()
        worker_thread.join(timeout=20)

    check("worker: the busy check cancelled the number with a refund",
          cancelled == ["act-9999999999"], cancelled)
    check("worker: it was refunded (not consumed - no SMS was at risk)",
          provider.refunded, "provider.balance restored")
    snapshot = coordinator.stats.snapshot()
    check("worker: a busy check is NOT counted as a broken checker",
          snapshot.get("bot_claims_denied", 0) == 1 and snapshot.get("refunds_missing", 0) == 0,
          {k: snapshot.get(k) for k in ("bot_claims_denied", "checker_fallbacks",
                                        "refunds_missing", "numbers_cancelled")})
    check("worker: the safety critical stop was NOT triggered for a busy bot",
          not coordinator.stopped, coordinator.stopped)


class FakeNumberProvider(object):
    """One number, then NO_BALANCE so the worker exits."""

    name = "tempora"

    def __init__(self):
        self.balance_before = 100.0
        self.price = 7.0
        self.refunded = False
        self._numbers = []
        self._cancelled = []

    def get_balance(self):
        return self.balance_before if self.refunded else self.balance_before - self.price

    def get_number(self, **kwargs):
        if not self._numbers:
            from base_otp import OTPNoBalance
            raise OTPNoBalance("no more numbers (end of test)")
        number = self._numbers.pop(0)
        return {"type": "ACCESS_NUMBER", "activation_id": f"act-{number}", "number": number}

    def get_status(self, activation_id):
        return {"type": "STATUS_WAIT_CODE"}

    def cancel(self, activation_id):
        self._cancelled.append(activation_id)
        self.refunded = True
        return {"type": "ACCESS_CANCEL"}

    def finish(self, activation_id):
        return {"type": "ACCESS_ACTIVATION"}


# ---------------------------------------------------------------------------
# Change 2, belt and braces: the bot client itself also refuses
# ---------------------------------------------------------------------------

def scenario_client_refuses_check_mid_login():
    client, bot = replay.build_client(referral_link=None, referral_script="absent")
    # Drive the bot to the OTP screen: the login is mid-flight.
    res = client.prepare_login("9876543210")
    check("client: the login reached the OTP wait screen",
          res.get("stage") == "otp_sent", res)
    check("client: the bot reports a login in flight",
          client._login_in_progress(bot_latest(bot)) is True)

    try:
        client.check_registration("8888888888")
        raise AssertionError("the check should have been refused")
    except MeeshoBotError as exc:
        check("client: the check is refused instead of walking out of the login",
              "login" in str(exc).lower(),
              f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        check("client: the check is refused instead of walking out of the login",
              False, f"unexpected {type(exc).__name__}: {exc}")
    check("client: the number to check was NOT typed into the OTP screen",
          "8888888888" not in [msg.text for msg in bot.messages],
          [msg.text for msg in bot.messages])


def bot_latest(bot):
    """Rebuild the current screen from the fake bot's last message."""
    from meesho_bot_client import Screen
    msg = bot.messages[-1]
    buttons = [[btn.text for btn in row] for row in (msg.buttons or [])]
    return Screen(str(msg.text or ""), buttons)


# ---------------------------------------------------------------------------
# Change 5: coordinator drives the warm loop
# ---------------------------------------------------------------------------

def scenario_warm_loop_parks_offer():
    os.chdir(SCRATCH_DIR)
    bot_client = _ready_bot_client()
    coordinator = _make_coordinator(bot_client)
    coordinator.stop_requested = threading.Event()
    coordinator.worker_statuses = {}
    coordinator.notify.send = lambda *a, **k: None
    coordinator.notify.alert = lambda *a, **k: None
    coordinator.notify.telegram.send = lambda *a, **k: False
    coordinator.notify.termux.send = lambda *a, **k: False

    check("warm: prewarm is on by default", coordinator.prewarm_enabled is True)
    coordinator._bot_warm_once()
    check("warm: the bot is parked on an offer that fits the target",
          coordinator.bot_warm.get("ready") is True
          and coordinator.bot_warm.get("upi") is not None
          and coordinator.bot_warm["upi"] <= bot_client.target_upi_price,
          coordinator.bot_warm)
    check("warm: the number prompt flag is set for the ZERO-reroll next login",
          coordinator.bot_at_number_prompt is True)
    check("warm: counted as a pre-warmed offer",
          coordinator.stats.snapshot().get("offer_prewarmed", 0) >= 1,
          coordinator.stats.snapshot().get("offer_prewarmed"))

    consumed = coordinator._release_bot_warm()
    check("warm: releasing the parked offer resets the flags",
          coordinator.bot_warm.get("ready") is False
          and coordinator.bot_at_number_prompt is False, coordinator.bot_warm)


def main():
    scenario_prepare_offer()
    scenario_gate_blocks_during_login()
    scenario_dedicated_check_refused_during_login()
    scenario_client_refuses_check_mid_login()
    scenario_worker_cancels_without_counting_failure()
    scenario_warm_loop_parks_offer()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All bot claim / offer pre-warm checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
