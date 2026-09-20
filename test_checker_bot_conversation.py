"""
The dedicated checker bot must check in ITS OWN conversation.

checker.telegram_bot.enabled + a username is supposed to keep number checks
away from the PRIMES login chat (no walking the bot out of a waiting OTP, no
tapping over the offer pre-warm). It did not:

  * `_resolve_bot_entity()` called `self._client.get_entity(username)` -
    a COROUTINE - from a coordinator/worker thread and then tried
    `self._loop.run_until_complete(...)`, which raises "This event loop is
    already running" (the loop lives in its own thread). The bare
    `except Exception` swallowed that and returned the PRIMES bot's entity,
    so the "dedicated" check was typed into the LOGIN conversation - and the
    abandoned coroutine produced
    "RuntimeWarning: coroutine 'UserMethods.get_entity' was never awaited".

  * That is also how a check ended up running at the same time as the offer
    pre-warm: the dedicated path deliberately skips the login claim (it has
    its own chat), so a mis-bound check tapped in the PRIMES chat while the
    pre-warm rerolled an offer - "Offer screen has no reroll button".

    python test_checker_bot_conversation.py
"""

import asyncio
import gc
import os
import sys
import tempfile
import threading
import warnings

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

# --- stub the optional runtime dependencies (no network in this check) ------
import types

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

from meesho_bot_client import (  # noqa: E402
    CheckerBotClient,
    MeeshoBotClient,
    MeeshoBotError,
    normalize_username,
)
from checker_client import CheckerUnavailable  # noqa: E402
from checker_router import BotChecker, CheckerBotBusy, CheckerRouter  # noqa: E402

# The fake Telethon bot / screens of the checker-flow replay.
import test_bot_checker_flow as flow  # noqa: E402


LOGIN_BOT = "@primesbot"
CHECKER_BOT = "@checkerbot"

SCRATCH = tempfile.mkdtemp(prefix="checker_bot_conversation_")
SESSION_FILE = os.path.join(SCRATCH, "userbot.session.txt")
with open(SESSION_FILE, "w", encoding="utf-8") as fh:
    fh.write("1BVtsOMZQGEy-fake-session-string")


# ---------------------------------------------------------------------------
# A Telethon stand-in with TWO conversations on ONE session
# ---------------------------------------------------------------------------

class TwoBotTelegramClient(object):
    """
    The real thing: one logged-in account, several chats. Every call is keyed
    by the entity it is given, so a check that carries the wrong entity ends
    up in the wrong conversation - exactly what the bug did.
    """

    def __init__(self, bots):
        self.bots = bots                  # normalized username -> FakeBot
        self.get_entity_calls = []
        self.get_entity_awaited = 0       # counts the coroutines actually awaited
        self.unreachable = set()          # usernames get_entity() cannot resolve

    def _bot_for(self, entity):
        return self.bots[normalize_username(entity)]

    async def get_messages(self, entity, limit=6):
        bot = self._bot_for(entity)
        if bot._pending_result:
            number, remaining = bot._pending_result
            remaining -= 1
            if remaining <= 0:
                bot._pending_result = None
                bot._finish_check(number)
            else:
                bot._pending_result = (number, remaining)
        return list(reversed(bot.messages[-limit:]))

    async def send_message(self, entity, text):
        await self._bot_for(entity).on_message(text)
        return self._bot_for(entity).last

    async def get_entity(self, username):
        self.get_entity_calls.append(username)
        await asyncio.sleep(0)            # a real await: only reachable when awaited
        if normalize_username(username) in self.unreachable:
            raise ValueError(f"No user has the username '{username}'")
        self.get_entity_awaited += 1
        return "@" + normalize_username(username)


def _loop_runner(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def build(login_state="menu", checker_results=None, login_results=None,
          login_screen=None):
    """
    A MeeshoBotClient whose loop runs in its OWN thread - production mode.
    The real `_run()` (run_coroutine_threadsafe) is kept on purpose: this is
    the setup in which `loop.run_until_complete()` from a worker thread
    raised "This event loop is already running".
    """
    primes = flow.FakeBot(results=dict(login_results or {}),
                          start_state=login_state)
    checker = flow.FakeBot(results=dict(checker_results or {}),
                           start_state="menu")
    if login_screen is not None:
        primes._push(login_screen)

    telegram = TwoBotTelegramClient({
        normalize_username(LOGIN_BOT): primes,
        normalize_username(CHECKER_BOT): checker,
    })

    config = {
        "meesho_bot": {
            "enabled": True,
            "api_id": 1,
            "api_hash": "x",
            "session_file": SESSION_FILE,
            "bot_username": LOGIN_BOT,
            "target_upi_price": 47,
            "step_timeout_seconds": 5,
            "poll_interval_seconds": 0.01,
            "human_delay_seconds": [0, 0],
        },
        "checker": {"bot": {"step_timeout_seconds": 3,
                            "button_hints": ["Check Number"]}},
    }
    client = MeeshoBotClient(config, log_fn=lambda *_: None)
    client._client = telegram
    client._bot_entity = LOGIN_BOT

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=_loop_runner, args=(loop,),
                              name="userbot-loop", daemon=True)
    thread.start()
    client._loop = loop
    client._thread = thread
    return client, primes, checker, telegram, loop


def stop(client, loop):
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def expect_error(name, fn, error_class=MeeshoBotError, contains=None):
    try:
        fn()
    except error_class as exc:
        ok = contains is None or contains in str(exc)
        check(name, ok, str(exc))
        return exc
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"{type(exc).__name__}: {exc}")
        return None
    check(name, False, "no exception")
    return None


# ---------------------------------------------------------------------------
# 1. The regression: the check must land in the CHECKER conversation
# ---------------------------------------------------------------------------

def scenario_check_goes_to_the_dedicated_bot():
    client, primes, checker, telegram, loop = build(
        login_state="offer", login_screen=flow.OFFER,
        checker_results={"9876543210": True},
    )
    try:
        checker_client = CheckerBotClient(client, CHECKER_BOT,
                                          conf={"button_hints": ["Check Number"]})
        check("dedicated: the checker client is ready", checker_client.ready)
        check("dedicated: it is not the login conversation",
              checker_client.shares_login_conversation is False)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = checker_client.check_registration("9876543210")
            gc.collect()
        runtime_warnings = [str(w.message) for w in caught
                            if issubclass(w.category, RuntimeWarning)]

        check("dedicated: verdict comes back",
              result.get("is_registered") is True, result)
        check("dedicated: the number went to the CHECKER bot",
              checker.sent_numbers == ["9876543210"],
              (checker.sent_numbers, checker.sent_texts))
        check("dedicated: the PRIMES login chat was never typed into",
              primes.sent_texts == [], primes.sent_texts)
        check("dedicated: the PRIMES login chat was never tapped",
              primes.taps == [], primes.taps)
        check("dedicated: the pre-warmed offer screen survived",
              primes.state == "offer", primes.state)
        check("dedicated: get_entity was awaited on the loop",
              telegram.get_entity_calls == [CHECKER_BOT]
              and telegram.get_entity_awaited == 1,
              (telegram.get_entity_calls, telegram.get_entity_awaited))
        check("dedicated: no 'coroutine was never awaited' warning",
              not [w for w in runtime_warnings if "never awaited" in w],
              runtime_warnings)
    finally:
        stop(client, loop)


def scenario_entity_is_cached_and_never_borrowed():
    client, primes, checker, telegram, loop = build(
        checker_results={"9876543210": False, "9876543211": True})
    try:
        # Before the entity is known there is nothing safe to return - and
        # never the PRIMES bot's entity (that was the bug).
        check("resolve: an unknown username resolves to None",
              client._resolve_bot_entity(CHECKER_BOT) is None)
        check("resolve: the login bot still resolves",
              client._resolve_bot_entity(LOGIN_BOT) == LOGIN_BOT)

        checker_client = CheckerBotClient(client, CHECKER_BOT)
        checker_client.check_registration("9876543210")
        calls_after_first = len(telegram.get_entity_calls)
        check("resolve: after the first bind the entity is cached",
              client._resolve_bot_entity(CHECKER_BOT) == "@checkerbot",
              client._resolve_bot_entity(CHECKER_BOT))

        checker_client.check_registration("9876543211")
        check("resolve: a second check does not re-resolve the entity",
              len(telegram.get_entity_calls) == calls_after_first,
              telegram.get_entity_calls)
        check("resolve: both numbers went to the checker bot",
              checker.sent_numbers == ["9876543210", "9876543211"],
              checker.sent_numbers)
        check("resolve: the login bot stayed untouched",
              primes.sent_texts == [] and primes.taps == [])
    finally:
        stop(client, loop)


# ---------------------------------------------------------------------------
# 2. A checker bot that cannot be opened is an error - never a fallback
# ---------------------------------------------------------------------------

def scenario_bind_failure_does_not_hijack_the_login_chat():
    client, primes, checker, telegram, loop = build(
        login_state="offer", login_screen=flow.OFFER,
        checker_results={"9876543210": True})
    telegram.unreachable.add("checkerbot")
    try:
        checker_client = CheckerBotClient(client, CHECKER_BOT)
        error = expect_error(
            "bind failure: the check fails loudly",
            lambda: checker_client.check_registration("9876543210"),
            MeeshoBotError, "Could not open the dedicated checker conversation")
        check("bind failure: the message names the bot",
              error is not None and "checkerbot" in str(error), error)
        check("bind failure: the PRIMES bot was NOT used as a stand-in",
              primes.sent_texts == [] and primes.taps == [],
              (primes.sent_texts, primes.taps))
    finally:
        stop(client, loop)


# ---------------------------------------------------------------------------
# 3. Pre-warm vs. check: the conflict that has to be reported, not hidden
# ---------------------------------------------------------------------------

def scenario_prewarm_owns_the_login_chat():
    client, primes, checker, telegram, loop = build(
        login_state="offer", login_screen=flow.OFFER,
        checker_results={"9876543210": True},
        login_results={"9876543210": True})
    try:
        # (a) A dedicated checker bot runs in its own chat: no conflict, the
        #     parked offer is not even touched.
        checker_client = CheckerBotClient(client, CHECKER_BOT)
        client.hold_conversation("prewarm")
        try:
            result = checker_client.check_registration("9876543210")
        finally:
            client.release_conversation("prewarm")
        check("prewarm: the dedicated bot answers while the pre-warm rerolls",
              result.get("is_registered") is True, result)
        check("prewarm: the parked offer was not typed into",
              primes.sent_texts == [], primes.sent_texts)

        # (b) The same check routed at the PRIMES chat (misconfigured
        #     telegram_bot.username = the login bot) must refuse and say why.
        same_conversation = CheckerBotClient(client, LOGIN_BOT)
        check("misconfig: the 'dedicated' bot is detected as the login bot",
              same_conversation.shares_login_conversation is True)
        client.hold_conversation("prewarm")
        try:
            error = expect_error(
                "conflict: a check on the pre-warm's chat is refused",
                lambda: same_conversation.check_registration("9876543210"),
                MeeshoBotError, "Refusing to run a number check")
        finally:
            client.release_conversation("prewarm")
        check("conflict: the message names the other flow (prewarm)",
              error is not None and "prewarm" in str(error), error)
        check("conflict: the message explains they share one conversation",
              error is not None and "SAME" in str(error), error)
        check("conflict: nothing was sent or tapped",
              primes.sent_texts == [] and primes.taps == [],
              (primes.sent_texts, primes.taps))

        # (c) No lease -> the same check runs (it is the login bot, after all).
        result = same_conversation.check_registration("9876543210")
        check("misconfig: without a lease the check runs in the login chat",
              result.get("is_registered") is True, result)
    finally:
        stop(client, loop)


def scenario_conflict_reason_is_empty_for_own_chat():
    client, primes, checker, telegram, loop = build(
        checker_results={"9876543210": True})
    try:
        client.hold_conversation("login")
        check("lease: the holder is reported",
              client.conversation_holder[0] == "login", client.conversation_holder)
        check("lease: a conflict is described for the shared chat",
              "login" in client.conversation_conflict_reason("check"),
              client.conversation_conflict_reason("check"))
        check("lease: no conflict for the owner itself",
              client.conversation_conflict_reason("login") == "")
        client.release_conversation("login")
        check("lease: released", client.conversation_holder == (None, 0.0),
              client.conversation_holder)
    finally:
        stop(client, loop)


# ---------------------------------------------------------------------------
# 4. Router level: which bot answers, and the claim still guards the login bot
# ---------------------------------------------------------------------------

def scenario_router_prefers_the_dedicated_bot():
    client, primes, checker, telegram, loop = build(
        login_state="menu", checker_results={"9876543210": True})
    try:
        dedicated = CheckerBotClient(client, CHECKER_BOT)
        bot_checker = BotChecker(
            lambda: client,
            conf={},
            log_fn=lambda *_: None,
            gate=lambda: (False, "a login flow is using the PRIMES bot"),
            preferred_getter=lambda: dedicated,
            preferred_username=CHECKER_BOT,
            preferred_name="Meesho Xxpress Manish",
        )
        check("router: the dedicated bot is ready", bot_checker.preferred_ready)
        data = bot_checker.check("9876543210")
        check("router: the dedicated bot answers even while the login is busy",
              data.get("is_registered") is True, data)
        check("router: the result names the bot that answered",
              data.get("checker_username") == "checkerbot"
              and "Manish" in str(data.get("checker_name")), data)
        check("router: the login chat was not used",
              primes.sent_texts == [] and primes.taps == [])

        # A "dedicated" bot that IS the login bot goes through the claim:
        # no claim here -> the check is refused instead of tapping over a
        # login / the pre-warm.
        same = CheckerBotClient(client, LOGIN_BOT)
        bot_checker_same = BotChecker(
            lambda: client,
            conf={},
            log_fn=lambda *_: None,
            gate=lambda: (False, "a login flow is using the PRIMES bot"),
            preferred_getter=lambda: same,
            preferred_username=LOGIN_BOT,
        )
        try:
            bot_checker_same.check("9876543210")
            check("router: a login-bot 'dedicated' bot takes the claim", False,
                  "no error raised")
        except CheckerBotBusy as exc:
            check("router: a login-bot 'dedicated' bot takes the claim", True)
            check("router: the busy reason is the login flow",
                  "login" in str(exc), str(exc))
        check("router: still nothing sent to the login chat",
              primes.sent_texts == [] and primes.taps == [], primes.sent_texts)

        # The log line says WHICH bot answers (it used to claim "PRIMES bot"
        # even when the dedicated one was doing the work).
        router = CheckerRouter(
            {"checker": {"mode": "auto",
                         "telegram_bot": {"enabled": True, "username": CHECKER_BOT}}},
            bot_getter=lambda: client,
            log_fn=lambda *_: None,
            checker_bot_getter=lambda: dedicated,
            checker_bot_username=CHECKER_BOT,
        )
        message = router._bot_check_message("9876543210", "API error: network")
        check("router: the log names the dedicated bot",
              "dedicated checker bot" in message and CHECKER_BOT in message,
              message)
        check("router: the startup summary mentions the dedicated bot",
              "dedicated checker bot" in router.describe(), router.describe())

        unavailable = CheckerRouter(
            {"checker": {"mode": "auto",
                         "telegram_bot": {"enabled": True, "username": CHECKER_BOT}}},
            bot_getter=lambda: client,
            log_fn=lambda *_: None,
            checker_bot_getter=lambda: None,
            checker_bot_username=CHECKER_BOT,
        )
        message = unavailable._bot_check_message("9876543210", "API error: network")
        check("router: when the dedicated bot cannot answer, the log says why",
              "PRIMES bot checker" in message and "cannot answer" in message,
              message)
        check("router: the startup summary says it is not usable",
              "NOT ready" in unavailable.describe(), unavailable.describe())
    finally:
        stop(client, loop)


def scenario_coordinator_leases_the_conversation():
    """_BotClaim marks the login/pre-warm as the driver of the PRIMES chat."""

    class _Coord(object):
        def __init__(self, bot):
            self.bot = bot
            self._bot_claim_lock = threading.RLock()
            self._bot_claim_owner = None
            self._bot_login_depth = 0
            self.bot_login_active = False

    import main as m  # noqa: E402  (imported late: it stubs its own deps)

    client, primes, checker, telegram, loop = build(
        checker_results={"9876543210": True})
    coord = _Coord(client)
    try:
        with m._BotClaim(coord, "prewarm", timeout=0, wait=False):
            check("claim: the pre-warm holds the conversation lease",
                  client.conversation_holder[0] == "prewarm",
                  client.conversation_holder)
        check("claim: the lease is released on exit",
              client.conversation_holder[0] is None, client.conversation_holder)
        with m._BotClaim(coord, "login", timeout=0, wait=True):
            check("claim: a login holds the conversation lease",
                  client.conversation_holder[0] == "login",
                  client.conversation_holder)
        check("claim: the lease is released after the login",
              client.conversation_holder[0] is None, client.conversation_holder)
    finally:
        stop(client, loop)


def scenario_auto_mode_end_to_end():
    """
    The reported run: the checker API is unreachable (network), the offer
    pre-warm owns the PRIMES chat and rerolls an offer - and the number still
    has to be checked in the dedicated bot's conversation.
    """

    class _FakeApi(object):
        def __init__(self):
            self.calls = 0

        def key_count(self):
            return 1

        def check(self, service, number):
            self.calls += 1
            raise CheckerUnavailable(
                "network: HTTPSConnectionPool(host='tubesave.in'): "
                "Max retries exceeded")

    class _Coord(object):
        def __init__(self, bot):
            self.bot = bot
            self._bot_claim_lock = threading.RLock()
            self._bot_claim_owner = None
            self._bot_login_depth = 0
            self.bot_login_active = False

    import main as m  # noqa: E402  (stubbed at the top of this file)

    client, primes, checker, telegram, loop = build(
        login_state="offer", login_screen=flow.OFFER,
        checker_results={"9789179020": False})
    coord = _Coord(client)
    logs = []
    try:
        router = CheckerRouter(
            {"checker": {"mode": "auto", "service": "meesho",
                         "telegram_bot": {"enabled": True,
                                          "username": CHECKER_BOT,
                                          "name": "Meesho Xxpress Manish",
                                          "button_hints": ["Check Number"]}}},
            bot_getter=lambda: client,
            log_fn=lambda msg: logs.append(str(msg)),
            api_client=_FakeApi(),
            gate=lambda: (not coord.bot_login_active, ""),
            claim=lambda: m._BotClaim(coord, "check", timeout=2, wait=True),
            checker_bot_getter=lambda: CheckerBotClient(
                client, CHECKER_BOT, conf={"button_hints": ["Check Number"]}),
            checker_bot_username=CHECKER_BOT,
        )

        client.hold_conversation("prewarm")     # the pre-warm owns the chat
        try:
            data = router.check("meesho", "9789179020")
        finally:
            client.release_conversation("prewarm")

        check("auto: the number is checked by the dedicated bot",
              data.get("is_registered") is False, data)
        check("auto: the verdict is attributed to the dedicated bot",
              data.get("checker_username") == "checkerbot", data)
        check("auto: the number went to the checker conversation",
              checker.sent_numbers == ["9789179020"], checker.sent_numbers)
        check("auto: the parked offer was never touched",
              primes.sent_texts == [] and primes.taps == []
              and primes.state == "offer",
              (primes.sent_texts, primes.taps, primes.state))
        joined = "\n".join(logs)
        check("auto: the fallback log names the dedicated bot, not PRIMES",
              "dedicated checker bot" in joined and "@checkerbot" in joined,
              joined)
    finally:
        stop(client, loop)


def main():
    print("--- dedicated checker conversation ---")
    scenario_check_goes_to_the_dedicated_bot()
    scenario_entity_is_cached_and_never_borrowed()
    scenario_bind_failure_does_not_hijack_the_login_chat()
    scenario_prewarm_owns_the_login_chat()
    scenario_conflict_reason_is_empty_for_own_chat()
    scenario_router_prefers_the_dedicated_bot()
    scenario_auto_mode_end_to_end()
    scenario_coordinator_leases_the_conversation()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All dedicated checker conversation checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
