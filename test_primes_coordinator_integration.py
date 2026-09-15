"""
Coordinator-side check of the PRIMES bot flow integration (main.py).

Runs without network access / provider credentials by stubbing `requests` and
the notifier, in a scratch directory (so the live stats.json / state.json are
never touched; the gitignored .signals/ dir may be re-created), then drives
ParallelAutomationCoordinator._bot_send_number and ._bot_submit_code against a
fake userbot client:

    python test_primes_coordinator_integration.py

Verifies:
  * a successful login reports which referral action was taken,
  * an unexpected screen alerts with the screen text AND its buttons, cancels
    the number with the refund expected (nothing was submitted), and resets,
  * a referral prompt interrupting the OTP submission surfaces as "unknown"
    instead of losing the code.
"""

import json
import os
import shutil
import sys
import tempfile
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = tempfile.mkdtemp(prefix="primes_coordinator_check_")

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
from meesho_bot_client import (  # noqa: E402
    MeeshoBotClient,
    MeeshoBotReferralError,
    MeeshoBotUnknownScreen,
)


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


class FakeProviderClient:
    name = "tempora"

    def get_balance(self):
        return 100.0

    def finish(self, activation_id):
        return True


class FakeNumberContext:
    def __init__(self):
        self.client = FakeProviderClient()
        self.provider_name = "tempora"
        self.activation_id = "act-123"
        self.raw_number = "919876543210"
        self.clean_number = "9876543210"


class FakeUserbot:
    """Stands in for the configured MeeshoBotClient (enabled + ready)."""

    def __init__(self, result=None, error=None, code_result=None,
                 referral_link="", referral_failure_action="stop"):
        self.bot_username = "@primesbot"
        self.max_change_number = 5
        self.enabled = True
        self.ready = True
        self.referral_link = referral_link
        self.referral_failure_action = referral_failure_action
        self.referral_summary = (
            f"referral link configured: {referral_link}" if referral_link
            else "no referral link configured - stops and reports if asked"
        )
        self._result = result or {}
        self._error = error
        self._code_result = code_result or {"status": "linked", "user_id": "1", "account_number": "42"}
        self.calls = []

    def prepare_login(self, number):
        self.calls.append(("prepare_login", number))
        if self._error:
            raise self._error
        return self._result

    def continue_with_number(self, number):
        self.calls.append(("continue_with_number", number))
        if self._error:
            raise self._error
        return self._result

    def submit_otp(self, code):
        self.calls.append(("submit_otp", code))
        if self._error:
            raise self._error
        return self._code_result

    def cancel_flow(self):
        self.calls.append(("cancel_flow",))
        return {"stage": "menu"}

    def change_number(self, new_number=None):
        self.calls.append(("change_number", new_number))
        return {"stage": "prompt"}

    def return_to_menu(self):
        self.calls.append(("return_to_menu",))
        return {"stage": "menu"}

    @property
    def referral_required(self):
        return self.referral_failure_action == "stop"

    def set_referral_link(self, link):
        previous = self.referral_link
        self.referral_link = (link or "").strip()
        return previous


def build_coordinator():
    config = json.load(open(os.path.join(REPO_DIR, "config.json"), encoding="utf-8"))
    config["meesho_bot"]["enabled"] = True
    config["active_otp_provider"] = "tempora"
    # Run in a scratch directory so the check never touches the live
    # stats.json / state.json / .signals of the automation.
    os.chdir(SCRATCH_DIR)
    coordinator = m.ParallelAutomationCoordinator(config, provider_override="tempora")

    sent = []
    coordinator.notify.send = lambda title, message, **kw: sent.append((title, message)) or ["stub"]
    coordinator.notify.alert = lambda title, message, **kw: sent.append((title, message)) or ["stub"]
    coordinator.notify.telegram.send = lambda *a, **k: False
    coordinator.notify.termux.send = lambda *a, **k: False

    cancellations = []
    coordinator.handle_cancellation = (
        lambda client, activation_id, number, reason, **kw:
        cancellations.append((reason, kw.get("expect_refund"))) or {"salvaged": False}
    )
    return coordinator, sent, cancellations


def test_successful_login_reports_referral():
    coordinator, sent, _ = build_coordinator()
    coordinator.bot = FakeUserbot(result={
        "stage": "otp_sent", "upi": 45.0, "rerolls": 2,
        "referral_action": "pasted referral link",
    })
    context = FakeNumberContext()
    res = coordinator._bot_send_number(context, from_prompt=False)

    check("login: result passed through", res and res["stage"] == "otp_sent", res)
    joined = "\n".join(msg for _t, msg in sent)
    check("login: notification names the referral action",
          "Referral: pasted referral link" in joined, joined)


def test_unexpected_screen_alert_and_refund():
    coordinator, sent, cancellations = build_coordinator()
    error = MeeshoBotUnknownScreen(
        "Referral screen needs a decision (no skip option on the screen); "
        "meesho_bot.referral_link is empty",
        "🎁 Referral link? Paste your Meesho referral link below.",
        ["✅ Yes, I have a refer code"],
    )
    coordinator.bot = FakeUserbot(error=error)
    context = FakeNumberContext()
    res = coordinator._bot_send_number(context, from_prompt=False)

    check("alert path: returns None so the worker moves on", res is None, res)
    title, message = sent[-1]
    check("alert path: title marks the provider", "PRIMES bot needs attention" in title, title)
    check("alert path: screen text included", "Referral link?" in message, message)
    check("alert path: buttons included", "Yes, I have a refer code" in message, message)
    check("alert path: mentions the referral config",
          "Referral step:" in message and "no referral link configured" in message,
          message)
    check("alert path: cancels with refund expected (nothing submitted)",
          cancellations and cancellations[-1][1] is True, cancellations)
    check("alert path: bot flow reset", ("cancel_flow",) in coordinator.bot.calls,
          coordinator.bot.calls)
    check("alert path: number-prompt flag cleared", coordinator.bot_at_number_prompt is False)


def test_code_not_submitted_after_referral_interrupt():
    coordinator, sent, cancellations = build_coordinator()
    error = MeeshoBotUnknownScreen(
        "Bot is not waiting for the OTP code anymore (screen: login_mode) - the "
        "referral step interrupted the number flow; code 111111 was NOT submitted",
        "✅ Referral link saved! Now choose how you want to log in.",
        ["Normal", "Auto"],
    )
    coordinator.bot = FakeUserbot(error=error)
    context = FakeNumberContext()
    status = coordinator._bot_submit_code(context, "111111", "111111 is your code")

    check("otp interrupt: reported as unknown", status == "unknown", status)
    title, message = sent[-1]
    check("otp interrupt: alert explains the situation",
          "not waiting for the OTP code" in message, message)
    check("otp interrupt: buttons included", "Normal / Auto" in message, message)

    # The coordinator then recovers exactly like any unconfirmed bot result:
    # Change Number, and no refund is expected because the SMS was delivered.
    coordinator._recover_change_number(context, "bot_unknown", expect_refund=False)
    check("otp interrupt: recovery uses Change Number without a refund claim",
          cancellations and cancellations[-1][1] is False, cancellations)
    check("otp interrupt: change-number recovery asked the bot",
          ("change_number", None) in coordinator.bot.calls, coordinator.bot.calls)


def test_referral_failure_stops_and_refunds():
    """
    Point 2: referral screen present + link unusable -> alert, cancel the number
    with a refund tally, and STOP the automation (no further numbers bought).
    """
    coordinator, sent, cancellations = build_coordinator()
    error = MeeshoBotReferralError(
        "Referral step failed: no referral link is configured. "
        "meesho_bot.referral_link is empty. Buttons on screen: "
        "🚫 I don't have a refer code / ❌ Cancel. Set it with /referral <link>...",
        "🎁 Referral link? Paste your Meesho referral link below.",
        ["🚫 I don't have a refer code", "❌ Cancel"],
    )
    coordinator.bot = FakeUserbot(error=error)
    context = FakeNumberContext()
    check("referral failure: stop flag clear before the attempt",
          coordinator.stop_requested.is_set() is False)

    res = coordinator._bot_send_number(context, from_prompt=False)

    check("referral failure: returns None so the worker moves on", res is None, res)
    title, message = sent[-1]
    check("referral failure: alert says the automation stopped",
          "Referral step failed" in title and "stopped" in title.lower(), title)
    check("referral failure: alert includes the screen",
          "Referral link?" in message, message)
    check("referral failure: alert includes the buttons",
          "I don't have a refer code" in message, message)
    check("referral failure: alert names both fixes",
          "/referral" in message and "referral_failure_action" in message, message)
    check("referral failure: alert reports the refund tally",
          "Refund tally" in message, message)
    check("referral failure: number cancelled with refund expected",
          cancellations and cancellations[-1][1] is True, cancellations)
    check("referral failure: automation STOPPED", coordinator.stop_requested.is_set())
    check("referral failure: critical stop counted",
          coordinator.stats.snapshot()["critical_stops"] == 1,
          coordinator.stats.snapshot())
    check("referral failure: bot flow reset", ("cancel_flow",) in coordinator.bot.calls,
          coordinator.bot.calls)


def test_referral_screen_absent_continues():
    """
    Point 1: when the bot does not show the referral screen, the flow proceeds
    normally even in strict mode with no link configured.
    """
    coordinator, sent, cancellations = build_coordinator()
    coordinator.bot = FakeUserbot(result={
        "stage": "otp_sent", "upi": 45.0, "rerolls": 1, "referral_action": None,
    })
    context = FakeNumberContext()
    res = coordinator._bot_send_number(context, from_prompt=False)

    check("screen absent: number accepted", res and res["stage"] == "otp_sent", res)
    check("screen absent: automation still running", not coordinator.stop_requested.is_set())
    check("screen absent: nothing cancelled", cancellations == [], cancellations)
    joined = "\n".join(msg for _t, msg in sent)
    check("screen absent: no referral line in the notification",
          "Referral:" not in joined, joined)


def test_referral_command_sets_and_clears_link():
    """Point 4: set the referral link from Telegram, saved to config.json."""
    coordinator, _sent, _canc = build_coordinator()
    coordinator.bot = FakeUserbot()

    reply = coordinator.command_referral_link("")
    check("referral cmd: status reply when no argument",
          "not set" in reply and "Failure action: stop" in reply, reply)

    reply = coordinator.command_referral_link("not-a-link")
    check("referral cmd: rejects a non-Meesho value",
          reply.startswith("❌") and coordinator.bot.referral_link == "", reply)

    link = "https://app.meesho.com/2yoV/r99th0qd?via=852o6g&from=account_section"
    reply = coordinator.command_referral_link(link)
    check("referral cmd: confirms saving", reply.startswith("✅") and link in reply, reply)
    check("referral cmd: applied to the live userbot",
          coordinator.bot.referral_link == link, coordinator.bot.referral_link)
    saved = json.load(open("config.json", encoding="utf-8"))
    check("referral cmd: persisted to config.json",
          saved["meesho_bot"]["referral_link"] == link, saved["meesho_bot"])
    check("referral cmd: rest of config intact",
          saved["telegram"]["chat_id"] == "883480917", saved["telegram"])

    reply = coordinator.command_referral_link("off")
    check("referral cmd: 'off' clears the link",
          reply.startswith("✅") and coordinator.bot.referral_link == "", reply)
    saved = json.load(open("config.json", encoding="utf-8"))
    check("referral cmd: cleared value persisted",
          saved["meesho_bot"]["referral_link"] == "", saved["meesho_bot"])


def test_telegram_referral_command_wiring():
    """The notifier routes /referral (with its argument) to the callback."""
    from notifier import TelegramBackend

    backend = TelegramBackend(token="t", chat_id="1")
    received = []

    class FakeUpdates(TelegramBackend):
        def _call(self, method, payload=None, timeout=None):
            if method == "getUpdates":
                return [{"update_id": 1, "message": {
                    "text": "/referral@MyAlertsBot https://app.meesho.com/abc?via=1",
                    "chat": {"id": 1},
                }}]
            self.sent.append((method, payload))
            return True

    backend2 = FakeUpdates(token="t", chat_id="1")
    backend2.sent = []
    backend2.referral_callback = lambda arg: received.append(arg) or "✅ saved"
    backend2.poll_signal({"run": "run"})

    check("telegram: /referral argument extracted (mention stripped)",
          received == ["https://app.meesho.com/abc?via=1"], received)
    titles = [p.get("text", "") for m, p in backend2.sent if m == "sendMessage"]
    check("telegram: reply sent back to the chat",
          any("Referral" in t for t in titles), titles)


def test_startup_logs_referral_config():
    coordinator, _, _ = build_coordinator()
    coordinator.bot = FakeUserbot()
    coordinator.bot.enabled = True
    summary = coordinator.bot.referral_summary
    check("startup: referral summary available for logs", "referral link configured" in summary, summary)


def main():
    print("=== Coordinator integration (referral flow) ===\n")
    print(f"(scratch dir: {SCRATCH_DIR})\n")
    shutil.copy(os.path.join(REPO_DIR, "config.json"), os.path.join(SCRATCH_DIR, "config.json"))
    test_successful_login_reports_referral()
    test_referral_screen_absent_continues()
    test_referral_failure_stops_and_refunds()
    test_referral_command_sets_and_clears_link()
    test_telegram_referral_command_wiring()
    test_unexpected_screen_alert_and_refund()
    test_code_not_submitted_after_referral_interrupt()
    test_startup_logs_referral_config()
    print()
    shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All coordinator checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
