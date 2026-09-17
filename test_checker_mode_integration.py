"""
Coordinator-side checks of checker.mode (main.py wiring).

Runs without network / provider credentials by stubbing `requests`, the
notifier and the checker API, in a scratch directory (so the live stats.json /
state.json are never touched), then:

  * drives worker_loop with a scripted "API is down" checker and a fake
    PRIMES-bot checker and verifies the number is validated through the bot
    and the verdict reaches the cancellation path,
  * verifies mode "api" still raises on a down API (nothing changes),
  * verifies mode "bot" refuses to start the run when the userbot is missing
    (instead of buying numbers only to cancel every one),
  * verifies the /checker command and set_checker_mode() config round-trip.

    python test_checker_mode_integration.py
"""

import json
import os
import shutil
import sys
import tempfile
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = tempfile.mkdtemp(prefix="checker_mode_check_")

if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)


# --- stub the optional runtime dependencies (no network in this check) ------

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

import checker_router  # noqa: E402
from checker_client import CheckerServiceDown, CheckerTimeout  # noqa: E402
import main as m  # noqa: E402
from base_otp import OTPError  # noqa: E402


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


class ScriptedAPI(object):
    """Stands in for CheckerClient inside CheckerRouter."""

    def __init__(self):
        self.results = []
        self.calls = []

    def key_count(self):
        return 1

    max_retry_wait_seconds = 45.0

    def check(self, service, number):
        self.calls.append((service, number))
        outcome = self.results.pop(0) if self.results else {"success": True, "is_registered": False}
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)


SCRIPTED_API = ScriptedAPI()


def _api_factory(**kwargs):
    return SCRIPTED_API


checker_router.CheckerClient = _api_factory


class FakeProviderClient(object):
    """N ACCESS_NUMBERs, then NO_BALANCE so the worker exits."""

    name = "tempora"

    def __init__(self, numbers=1):
        self.calls = 0
        self.numbers = numbers

    def get_balance(self):
        return 100.0

    def get_number(self):
        self.calls += 1
        if self.calls <= self.numbers:
            return {"type": "ACCESS_NUMBER", "activation_id": f"act-{self.calls}",
                    "number": "919876543210"}
        return {"type": "NO_BALANCE"}

    def cancel(self, activation_id):
        return {"type": "STATUS_CANCEL"}

    def finish(self, activation_id):
        return True


class FakeUserbot(object):
    """Stands in for MeeshoBotClient for the checker path."""

    def __init__(self, verdict=True, error=None, ready=True, enabled=True):
        self.enabled = enabled
        self.ready = ready
        self.bot_username = "@primesbot"
        self.start_error = None if ready else "userbot not connected"
        self.referral_link = ""
        self.referral_failure_action = "stop"
        self.referral_summary = "referral not used in this check"
        self.max_change_number = 5
        self._verdict = verdict
        self._error = error
        self.check_calls = []

    # checker API used by CheckerRouter
    def check_registration(self, number):
        self.check_calls.append(number)
        if self._error:
            raise self._error
        return {"success": True, "is_registered": self._verdict, "source": "bot"}

    # lifecycle used by run()
    def start(self):
        if self.ready:
            return True
        self.start_error = self.start_error or "not configured"
        return False

    def stop(self):
        return None


def build_coordinator(mode="auto", bot=None, api_results=None):
    config = json.load(open(os.path.join(REPO_DIR, "config.json"), encoding="utf-8"))
    config["checker"]["mode"] = mode
    config["active_otp_provider"] = "tempora"
    config["telegram"]["enabled"] = False
    config["termux"]["enabled"] = False
    os.chdir(SCRATCH_DIR)
    # A private config.json in the scratch dir, so /checker can persist into it
    # without ever touching the live automation's file.
    shutil.copy(os.path.join(REPO_DIR, "config.json"),
                os.path.join(SCRATCH_DIR, "config.json"))
    SCRIPTED_API.results = list(api_results or [])
    SCRIPTED_API.calls = []
    coordinator = m.ParallelAutomationCoordinator(config, provider_override="tempora")
    # Counters live in stats.json, which would otherwise carry over between the
    # scenarios below.
    coordinator.stats.reset()

    sent = []
    coordinator.notify.send = lambda title, message, **kw: sent.append((title, message)) or ["stub"]
    coordinator.notify.alert = lambda title, message, **kw: sent.append((title, message)) or ["stub"]
    coordinator.notify.telegram.send = lambda *a, **k: False
    coordinator.notify.termux.send = lambda *a, **k: False

    cancellations = []
    coordinator.handle_cancellation = (
        lambda client, activation_id, number, reason, **kw:
        cancellations.append((number, reason, kw.get("expect_refund"))) or
        {"salvaged": False, "balance": None}
    )
    if bot is not None:
        coordinator.bot = bot
    return coordinator, sent, cancellations


def test_worker_falls_back_to_bot():
    bot = FakeUserbot(verdict=True)
    coordinator, sent, cancellations = build_coordinator(
        mode="auto", bot=bot, api_results=[CheckerServiceDown("is_down=true")])
    provider = FakeProviderClient()
    coordinator.worker_loop(provider)

    check("worker/auto: api tried once", len(SCRIPTED_API.calls) == 1, SCRIPTED_API.calls)
    check("worker/auto: bot check ran on the acquired number",
          bot.check_calls == ["9876543210"], bot.check_calls)
    check("worker/auto: verdict drove the cancellation",
          cancellations and cancellations[0][1] == "Already registered on Meesho",
          cancellations)
    stats = coordinator.stats.snapshot()
    check("worker/auto: fallback counted", stats["checker_fallbacks"] == 1, stats)
    check("worker/auto: bot check counted", stats["checker_bot_checks"] == 1, stats)
    check("worker/auto: api check not counted as a success",
          stats["checker_api_checks"] == 0, stats)


def test_worker_api_mode_unchanged():
    bot = FakeUserbot(verdict=True)
    coordinator, sent, cancellations = build_coordinator(
        mode="api", bot=bot, api_results=[CheckerServiceDown("is_down=true")])
    provider = FakeProviderClient()
    coordinator.worker_loop(provider)

    check("worker/api: bot never used", bot.check_calls == [], bot.check_calls)
    check("worker/api: number cancelled on the API error",
          cancellations and "Checker error" in cancellations[0][1], cancellations)
    stats = coordinator.stats.snapshot()
    check("worker/api: no fallback counted", stats["checker_fallbacks"] == 0, stats)


def test_worker_auto_read_timeout():
    bot = FakeUserbot(verdict=True)
    coordinator, sent, cancellations = build_coordinator(
        mode="auto", bot=bot, api_results=[CheckerTimeout("Checker timed out: ReadTimeout(15)")])
    provider = FakeProviderClient()
    coordinator.worker_loop(provider)
    check("worker/auto: read timeout falls back to the bot",
          bot.check_calls == ["9876543210"], bot.check_calls)
    check("worker/auto: the bot verdict drives the flow",
          cancellations and cancellations[0][1] == "Already registered on Meesho",
          cancellations)
    stats = coordinator.stats.snapshot()
    check("worker/auto: timeout fallback counted", stats["checker_fallbacks"] == 1, stats)


def test_bot_mode_gives_up_after_repeated_failures():
    from meesho_bot_client import MeeshoBotUnknownScreen

    bot = FakeUserbot(error=MeeshoBotUnknownScreen(
        "The bot checker gave no readable result for this number",
        "Hmm, something went wrong.", ["🏠 Main Menu"]))
    coordinator, sent, cancellations = build_coordinator(mode="bot", bot=bot)
    provider = FakeProviderClient(numbers=5)
    coordinator.worker_loop(provider)

    check("bot mode failures: stops after the configured streak",
          coordinator.stop_requested.is_set() and coordinator.is_running is False,
          coordinator.stop_requested.is_set())
    check("bot mode failures: exactly 3 checks bought 3 numbers",
          len(bot.check_calls) == 3 and len(cancellations) == 3,
          (bot.check_calls, cancellations))
    joined = "\n".join(f"{title}\n{message}" for title, message in sent)
    check("bot mode failures: critical alert explains the stop",
          "checks in a row failed" in joined and "/checker api" in joined, joined)
    check("bot mode failures: counted as a critical stop",
          coordinator.stats.snapshot()["critical_stops"] == 1,
          coordinator.stats.snapshot())

    # A good check resets the streak, so a flaky checker is tolerated.
    bot = FakeUserbot(error=MeeshoBotUnknownScreen("boom", "screen", []))
    coordinator, sent, cancellations = build_coordinator(mode="bot", bot=bot)
    coordinator._note_checker_failure()
    coordinator._note_checker_failure()
    check("bot mode failures: streak accumulates",
          coordinator.checker_failure_streak == 2, coordinator.checker_failure_streak)
    coordinator._reset_checker_failures()
    check("bot mode failures: streak resets after a good check",
          coordinator.checker_failure_streak == 0, coordinator.checker_failure_streak)
    check("bot mode failures: limit comes from the config",
          coordinator.checker.stop_after_failures == 3, coordinator.checker.stop_after_failures)


def test_bot_mode_without_userbot_refuses_to_start():
    bot = FakeUserbot(ready=False, enabled=True)
    coordinator, sent, cancellations = build_coordinator(mode="bot", bot=bot)
    coordinator.run()
    check("bot mode: run refused", coordinator.is_running is False, coordinator.is_running)
    check("bot mode: nothing bought", cancellations == [], cancellations)
    check("bot mode: stopped instead of hunting",
          coordinator.stop_requested.is_set(), coordinator.stop_requested.is_set())
    joined = "\n".join(f"{title}\n{message}" for title, message in sent)
    check("bot mode: alert explains why", "checker.mode" in joined and "userbot" in joined,
          joined)


def test_auto_mode_warns_but_runs():
    bot = FakeUserbot(ready=False, enabled=True)
    coordinator, sent, cancellations = build_coordinator(mode="auto", bot=bot)
    check("auto mode: still wants the bot", coordinator.checker.mode_wants_bot is True)
    check("auto mode: bot reported not ready", coordinator.checker.bot_ready is False)


def test_checker_command_and_config_round_trip():
    bot = FakeUserbot()
    coordinator, sent, cancellations = build_coordinator(mode="auto", bot=bot)

    status = coordinator.command_checker_mode("")
    check("command: status shows the mode", "AUTO" in status, status)
    check("command: status names the bot fallback", "PRIMES bot" in status, status)

    reply = coordinator.command_checker_mode("api")
    check("command: switching to api", coordinator.checker.mode == "api", coordinator.checker.mode)
    check("command: reply confirms", "API" in reply.upper(), reply)
    check("command: config.json written",
          json.load(open(os.path.join(SCRATCH_DIR, "config.json"),
                         encoding="utf-8"))["checker"]["mode"] == "api",
          open(os.path.join(SCRATCH_DIR, "config.json"), encoding="utf-8").read()[-400:])

    reply = coordinator.command_checker_mode("primes")
    check("command: alias 'primes' -> bot", coordinator.checker.mode == "bot", coordinator.checker.mode)
    check("command: reply mentions the bot", "PRIMES" in reply.upper(), reply)

    reply = coordinator.command_checker_mode("nonsense")
    check("command: unknown mode refused",
          "Unknown checker mode" in reply and coordinator.checker.mode == "bot", reply)

    # set_checker_mode writes the "checker" block without touching the rest.
    path = os.path.join(SCRATCH_DIR, "config.json")
    before = json.load(open(path, encoding="utf-8"))
    m.set_checker_mode(path, "auto")
    after = json.load(open(path, encoding="utf-8"))
    check("config writer: mode updated", after["checker"]["mode"] == "auto")
    check("config writer: service key intact",
          after["checker"]["service"] == before["checker"]["service"])
    check("config writer: api keys intact",
          after["checker"]["api_keys"] == before["checker"]["api_keys"])
    check("config writer: other sections intact", after["tempora"] == before["tempora"])
    text = open(path, encoding="utf-8").read()
    check("config writer: no duplicate mode key", text.count('"mode"') == 1, text.count('"mode"'))

    try:
        m.set_checker_mode(path, "nonsense")
        check("config writer: rejects an unknown mode", False, "no exception")
    except ValueError:
        check("config writer: rejects an unknown mode", True)


def test_status_summary_mentions_checker():
    bot = FakeUserbot()
    coordinator, sent, cancellations = build_coordinator(mode="auto", bot=bot)
    summary = coordinator.get_status_summary()
    check("status: checker line present", "Checker:" in summary, summary)
    check("status: bot checks counter shown", "via PRIMES bot" in summary, summary)


def main():
    test_worker_falls_back_to_bot()
    test_worker_api_mode_unchanged()
    test_worker_auto_read_timeout()
    test_bot_mode_gives_up_after_repeated_failures()
    test_bot_mode_without_userbot_refuses_to_start()
    test_auto_mode_warns_but_runs()
    test_checker_command_and_config_round_trip()
    test_status_summary_mentions_checker()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All checker mode integration checks passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        os.chdir(REPO_DIR)
        shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
