"""
The _run() watchdog must work on Python < 3.11 and must never leave a flow
running on the userbot loop.

The bug this pins down (seen live, Python 3.10/anaconda):

    [TEMPORA] Offer pre-warm failed:
    ...
    [TEMPORA] Unexpected error while processing 9025653558:
    concurrent.futures._base.TimeoutError
      File ".../meesho_bot_client.py", line 1423, in _run
        return future.result(timeout=wait)

`future.result(timeout=...)` raises `concurrent.futures.TimeoutError`, which is
NOT the builtin `TimeoutError` before Python 3.11 (the docs: "Changed in
version 3.11: This class was made an alias of TimeoutError" - the same change
applies to asyncio.TimeoutError). _run() polls the future in short (~2s)
slices and treats a slice timeout as "the flow is still working, keep
waiting", but with a bare `except TimeoutError:` the FIRST slice escaped the
method as a bare, message-less TimeoutError:

  * every PRIMES flow "failed" after ~2s with an empty reason - the empty
    "Offer pre-warm failed: " / "Unexpected error while processing ...: "
    lines in the live log, a paid number cancelled and refunded;
  * the abandoned coroutine kept running on the loop, tapping the SAME
    Telegram chat while the next flow started. Two flows in one conversation
    is why the bot never reaced a stable offer page ("it went up to Normal
    login mode at most") and why the offer pre-warm appeared to be shared with
    a number check;
  * the watchdog never got to cancel anything (Python 3.11+ hides all of this
    because the classes are aliases there).

Also covered here:
  * a healthy flow that legitimately outlives several watchdog slices still
    completes (the reason slices exist at all);
  * the leak guard: however _run() exits, an unfinished flow is cancelled;
  * a non-timeout error from the flow is still raised as itself;
  * `in_checker_screen()` no longer reports the LOGIN flow's own number prompt
    or "fetching your offer" copy as a bot-checker screen (that false positive
    produced the bogus "a number check is using the PRIMES conversation -
    configure a dedicated checker bot" warning while the checker API was
    answering and a dedicated checker bot was configured);
  * the coordinator only blames a number check when a check can actually drive
    the PRIMES chat.

    python test_run_watchdog_compat.py
"""

import asyncio
import concurrent.futures
import concurrent.futures._base as futures_base
import contextlib
import os
import sys
import threading
import time
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

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
import meesho_bot_client as bot_module  # noqa: E402
from meesho_bot_client import (  # noqa: E402
    MeeshoBotClient,
    MeeshoBotError,
    MeeshoBotTimeout,
    MeeshoBotUnknownScreen,
    Screen,
)
import test_primes_referral_flow as replay  # noqa: E402


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


# ---------------------------------------------------------------------------
# Simulating Python <= 3.10: concurrent.futures.TimeoutError is its own class
# ---------------------------------------------------------------------------

class LegacyFutureTimeout(Exception):
    """`concurrent.futures.TimeoutError` on Python <= 3.10: not a builtin."""


@contextlib.contextmanager
def pre_311_timeout_split():
    """
    Make `future.result(timeout=...)` raise a class of its own, the way it does
    on Python 3.8-3.10 (anaconda's base env), while _is_timeout_error() keeps
    reading the class at call time - which is exactly the difference the fix
    has to survive.
    """
    saved = (concurrent.futures.TimeoutError, futures_base.TimeoutError)
    concurrent.futures.TimeoutError = LegacyFutureTimeout
    futures_base.TimeoutError = LegacyFutureTimeout
    try:
        yield
    finally:
        concurrent.futures.TimeoutError, futures_base.TimeoutError = saved


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _config(**overrides):
    conf = {
        "enabled": True,
        "api_id": 1,
        "api_hash": "x",
        "session_file": "userbot.session.txt",
        "bot_username": "@primesbot",
        "target_upi_price": 47,
        "max_offer_rerolls": 5,
        "step_timeout_seconds": 5,
        "poll_interval_seconds": 0.01,
        "human_delay_seconds": [0, 0],
    }
    conf.update(overrides)
    return {"meesho_bot": conf}


def _start_client(telegram, **bot_overrides):
    """A real MeeshoBotClient with the real _run() on a real userbot loop."""
    client = MeeshoBotClient(_config(**bot_overrides), log_fn=lambda *_: None)
    client._client = telegram
    client._bot_entity = "@primesbot"
    client._loop = asyncio.new_event_loop()
    thread = threading.Thread(target=client._run_loop, daemon=True)
    thread.start()
    return client, thread


def _stop(client, thread):
    try:
        client.stop()
    except Exception:
        pass
    thread.join(timeout=5)


class HangingTelegram:
    """Every screen fetch hangs forever; `cancelled` records a real stop."""

    def __init__(self):
        self.cancelled = threading.Event()
        self.fetches = 0

    async def get_messages(self, entity, limit=6):
        self.fetches += 1
        try:
            await asyncio.sleep(3600)
        finally:
            # Runs when the task is cancelled (or when the loop is torn down).
            self.cancelled.set()


class _Button:
    def __init__(self, text):
        self.text = text


class _Message:
    def __init__(self, text, buttons=None):
        self.text = text
        self.id = 1
        self.edit_date = None
        self.buttons = [[_Button(label) for label in row] for row in (buttons or [])]


class OneScreenTelegram:
    """Always shows the same screen (for the read-only checker probe)."""

    def __init__(self, text, buttons=None):
        self.message = _Message(text, buttons)
        self.calls = 0

    async def get_messages(self, entity, limit=6):
        self.calls += 1
        return [self.message]


# ---------------------------------------------------------------------------
# 1. The watchdog under both exception worlds
# ---------------------------------------------------------------------------

def _hanging_flow_outcome():
    """Run a hung flow, return (error, elapsed, telegram, client, thread)."""
    telegram = HangingTelegram()
    client, thread = _start_client(telegram, flow_timeout_seconds=1.0)
    started = time.time()
    error = None
    try:
        client.prepare_login("9876543210")
    except Exception as exc:  # noqa: BLE001 - the whole point is its type
        error = exc
    return error, time.time() - started, telegram, client, thread


def scenario_watchdog_both_python_splits():
    for label, ctx in (("native", contextlib.nullcontext()),
                       ("legacy (<3.11) classes", pre_311_timeout_split())):
        with ctx:
            error, elapsed, telegram, client, thread = _hanging_flow_outcome()
            try:
                check(f"{label}: a hung flow aborts with MeeshoBotTimeout",
                      isinstance(error, MeeshoBotTimeout), repr(error))
                check(f"{label}: the abort is a MeeshoBotError the coordinator handles",
                      isinstance(error, MeeshoBotError), type(error).__name__)
                check(f"{label}: the message is not empty (no bare 'Error: ')",
                      bool(str(error).strip()), repr(str(error)))
                check(f"{label}: the message names the aborted flow",
                      "prepare_login" in str(error or ""), str(error))
                check(f"{label}: it aborts near the no-progress budget, not on the first slice",
                      elapsed < 8, f"{elapsed:.1f}s")
                check(f"{label}: the abandoned flow was cancelled (no zombie taps)",
                      telegram.cancelled.wait(timeout=3) is True)
                # The loop stays usable for the next flow.
                try:
                    client.prepare_login("9876543211")
                    check(f"{label}: the loop is still usable after an abort", False,
                          "no exception")
                except MeeshoBotTimeout:
                    check(f"{label}: the loop is still usable after an abort", True)
                except Exception as exc:  # noqa: BLE001
                    check(f"{label}: the loop is still usable after an abort", False,
                          repr(exc))
            finally:
                _stop(client, thread)


def scenario_is_timeout_error_reads_classes_at_call_time():
    check("timeout helper: the builtin TimeoutError is a timeout",
          bot_module._is_timeout_error(TimeoutError()) is True)
    check("timeout helper: other errors are not",
          bot_module._is_timeout_error(ValueError()) is False)
    with pre_311_timeout_split():
        check("timeout helper: the pre-3.11 concurrent.futures class counts",
              bot_module._is_timeout_error(LegacyFutureTimeout()) is True)
        check("timeout helper: a builtin TimeoutError still counts",
              bot_module._is_timeout_error(TimeoutError()) is True)


# ---------------------------------------------------------------------------
# 2. A healthy flow must survive the slice timeouts (the reason they exist)
# ---------------------------------------------------------------------------

def scenario_slow_healthy_flow_completes():
    client, bot = replay.build_client(referral_script="absent", referral_link=None,
                                      flow_timeout_seconds=1.0, step_timeout_seconds=5)
    # Use the REAL _run() (the replay harness swaps it for run_until_complete,
    # which would bypass the watchdog entirely).
    client._run = types.MethodType(MeeshoBotClient._run, client)
    slow = client._client
    original = slow.get_messages

    async def delayed_get_messages(entity, limit=6):
        await asyncio.sleep(0.05)          # every poll outlives a 0.25s slice soon enough
        return await original(entity, limit)

    slow.get_messages = delayed_get_messages
    client._loop = asyncio.new_event_loop()
    thread = threading.Thread(target=client._run_loop, daemon=True)
    thread.start()
    try:
        started = time.time()
        res = client.prepare_login("9876543210")
        elapsed = time.time() - started
        check("slice: a healthy (slow) flow outliving watchdog slices still completes",
              isinstance(res, dict) and res.get("stage") == "otp_sent", res)
        check("slice: it really took longer than the watchdog slice",
              elapsed > 0.25, f"{elapsed:.2f}s")
        check("slice: the number reached the bot (OTP screen)",
              bot.state == "otp_wait", getattr(bot, "state", None))
    finally:
        _stop(client, thread)


# ---------------------------------------------------------------------------
# 3. Leak guard: an unclassified escape must still not leave a flow running
# ---------------------------------------------------------------------------

def scenario_escaped_timeout_still_cancels_the_flow():
    telegram = HangingTelegram()
    client, thread = _start_client(telegram, flow_timeout_seconds=1.0)
    saved = bot_module._is_timeout_error
    bot_module._is_timeout_error = lambda exc: False
    try:
        error = None
        try:
            client.prepare_login("9876543210")
        except Exception as exc:  # noqa: BLE001
            error = exc
        check("leak guard: the escape is NOT silently reported as a bot abort",
              not isinstance(error, MeeshoBotTimeout), type(error).__name__)
        check("leak guard: the abandoned flow was cancelled anyway (no zombie taps)",
              telegram.cancelled.wait(timeout=3) is True)
    finally:
        bot_module._is_timeout_error = saved
        _stop(client, thread)


def scenario_flow_errors_pass_through():
    telegram = OneScreenTelegram("Main Menu", [["Add Account"], ["Open Shop"]])
    client, thread = _start_client(telegram)
    try:
        async def boom():
            raise MeeshoBotUnknownScreen("boom", "some screen", ["A"])
        try:
            client._run(boom())
            check("pass-through: the flow's own error is raised", False, "no exception")
        except MeeshoBotUnknownScreen:
            check("pass-through: the flow's own error is raised", True)
        except Exception as exc:  # noqa: BLE001
            check("pass-through: the flow's own error is raised", False,
                  f"{type(exc).__name__}: {exc}")
        # ...and the loop is not left with a pending task afterwards.
        check("pass-through: the loop has no pending flow afterwards",
              not [t for t in asyncio.all_tasks(client._loop)
                   if not t.done() and t is not asyncio.current_task()])
    finally:
        _stop(client, thread)


# ---------------------------------------------------------------------------
# 4. The checker-screen probe must not read LOGIN screens as checker screens
# ---------------------------------------------------------------------------

LOGIN_CHANGE_NUMBER = (
    "\u270f\ufe0f Change Number\n"
    "Send the 10-digit mobile number you'd like to use instead."
)
LOGIN_FETCHING_OFFER = "\u23f3 Fetching your offer\u2026 Please wait"
CHECKER_PROMPT = (
    "\U0001f50d Number Check\n"
    "Send the 10-digit number to check if it is registered on Meesho"
)
CHECKER_RESULT = "\u2705 +919876543210 \u2014 REGISTERED on Meesho"


def scenario_checker_screen_probe():
    client = MeeshoBotClient(_config(), log_fn=lambda *_: None)
    cases = (
        # LOGIN screens (the false positives that produced the bogus warning)
        ("login Change Number prompt", LOGIN_CHANGE_NUMBER, [["\u274c Cancel"]], False),
        ("login 'fetching your offer' copy", LOGIN_FETCHING_OFFER, [], False),
        ("login offer screen", "Offer \u00b7 Rs.47\nUPI \u00b7 \u20b947\n"
                               "Send the 10-digit mobile number",
         [["\U0001f504 Try Another Offer"], ["\u274c Cancel"]], False),
        ("login OTP wait screen", "OTP on its way. Type it here when it arrives.",
         [["Change Number"]], False),
        ("main menu", "Main Menu", [["Add Account"], ["Open Shop"]], False),
        # Real checker screens - still recognised
        ("checker prompt", CHECKER_PROMPT, [["\U0001f3e0 Main Menu"]], True),
        ("checker 'checking' transient", "\u23f3 Checking the number\u2026 please wait",
         [], True),
        ("checker result (registered)", CHECKER_RESULT, [], True),
        ("checker result (new user)",
         "\U0001f195 +919876543210 \u2014 NOT REGISTERED (NEW USER)", [], True),
    )
    for name, text, buttons, expected in cases:
        got = client._looks_like_checker_screen(Screen.from_text(text, buttons))
        check(f"probe: {name} -> in_checker_screen={expected}", got is expected, got)

    # ...and through the real read-only probe (flow lock + loop + screen fetch).
    for name, text, buttons, expected in (
            ("login Change Number prompt is not a checker screen",
             LOGIN_CHANGE_NUMBER, [["\u274c Cancel"]], False),
            ("login 'fetching your offer' is not a checker screen",
             LOGIN_FETCHING_OFFER, [], False),
            ("the PRIMES checker prompt IS a checker screen",
             CHECKER_PROMPT, [["\U0001f3e0 Main Menu"]], True)):
        probe_client, thread = _start_client(OneScreenTelegram(text, buttons))
        try:
            check(f"probe (live): {name}", probe_client.in_checker_screen() is expected)
        finally:
            _stop(probe_client, thread)


# ---------------------------------------------------------------------------
# 5. The coordinator only blames a check when a check can use that chat
# ---------------------------------------------------------------------------

def scenario_checks_can_use_primes_chat():
    class _Bot:
        def __init__(self, has_preferred, fallback, shares):
            self.has_preferred = has_preferred
            self.fallback_to_primes = fallback
            self.shares = shares
            self.preferred_client = object() if has_preferred else None

        def preferred_shares_login(self, client):
            return self.shares

    class _Checker:
        def __init__(self, has_preferred, fallback, shares):
            self.mode_wants_bot = True
            self.bot = _Bot(has_preferred, fallback, shares)

    class _Coord:
        pass

    def gate(has_preferred, fallback, shares, mode_wants_bot=True):
        coord = _Coord()
        coord.checker = _Checker(has_preferred, fallback, shares)
        coord.checker.mode_wants_bot = mode_wants_bot
        return m.ParallelAutomationCoordinator._checks_can_use_primes_chat(coord)

    check("gate: no dedicated checker bot -> checks do use this chat",
          gate(False, False, False) is True)
    check("gate: dedicated bot in its own chat + fallback off -> checks never touch it",
          gate(True, False, False) is False)
    check("gate: the 'dedicated' bot IS the login bot -> same conversation",
          gate(True, False, True) is True)
    check("gate: fallback_to_primes=true -> PRIMES may answer a check",
          gate(True, True, False) is True)
    check("gate: checker.mode=api -> no bot conversation is used",
          gate(False, False, False, mode_wants_bot=False) is False)


# ---------------------------------------------------------------------------

def main():
    scenario_watchdog_both_python_splits()
    scenario_is_timeout_error_reads_classes_at_call_time()
    scenario_slow_healthy_flow_completes()
    scenario_escaped_timeout_still_cancels_the_flow()
    scenario_flow_errors_pass_through()
    scenario_checker_screen_probe()
    scenario_checks_can_use_primes_chat()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All _run watchdog / Python-3.11 compatibility checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
