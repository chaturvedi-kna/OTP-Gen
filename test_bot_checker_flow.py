"""
Replay of the PRIMES bot NUMBER CHECKER against the real MeeshoBotClient, using
a fake Telethon client - no Telegram connection, no network, no telethon
install needed:

    python test_bot_checker_flow.py

Covers the checker entry (menu button / command), the number prompt, the
registered / not-registered wording (incl. variants and negations), the
"checking..." transient, timeouts, custom hint configuration, and the
guarantee that a number is never typed into a non-prompt screen.
"""

import asyncio
import threading
import time

from meesho_bot_client import (
    MeeshoBotClient,
    MeeshoBotError,
    MeeshoBotUnknownScreen,
    Screen,
    S_CHECK_PROMPT,
    S_CHECK_RESULT,
    S_CHECKING,
)


# ---------------------------------------------------------------------------
# Screens (copy variants of the bot's checker)
# ---------------------------------------------------------------------------

MAIN_MENU = Screen(
    "👋 Welcome to Meesho Primes Bot\n\nWhat would you like to do?",
    [["➕ Add Account"], ["🏠 Open Shop"], ["🔍 Check Number"]],
)

MAIN_MENU_NO_CHECKER = Screen(
    "👋 Welcome to Meesho Primes Bot\n\nWhat would you like to do?",
    [["➕ Add Account"], ["🏠 Open Shop"]],
)

MAIN_MENU_WITH_BALANCE = Screen(
    "👋 Welcome to Meesho Primes Bot\n\nWhat would you like to do?",
    [["➕ Add Account"], ["💵 Check Balance"], ["🏠 Open Shop"]],
)

CHECK_PROMPT = Screen(
    "🔍 Check Number\n\nSend the 10-digit mobile number you want to check.",
    [["🏠 Main Menu"]],
)

CHECK_PROMPT_VARIANT = Screen(
    "🔎 Number Status\n\nEnter the mobile number below and I will tell you "
    "whether it is registered.",
    [["🏠 Main Menu"], ["❌ Cancel"]],
)

CHECKING = Screen("🔍 Checking the number, please wait...")

RESULT_REGISTERED = Screen(
    "🔍 Number Check\n\n📱 9876543210\n\n"
    "✅ This number is already registered on Meesho.",
    [["🔄 Check Another Number"], ["🏠 Main Menu"]],
)

RESULT_NOT_REGISTERED = Screen(
    "🔍 Number Check\n\n📱 9876543211\n\n"
    "❌ This number is not registered on Meesho. It is available for a new account.",
    [["🔄 Check Another Number"], ["🏠 Main Menu"]],
)

RESULT_UNREADABLE = Screen(
    "Hmm, something went wrong while looking that up.",
    [["🏠 Main Menu"]],
)

# A leftover login screen: the checker must reset out of it and must never
# type the number here.
OFFER = Screen(
    "🎁 Special offer for you!\n\nUPI · ₹60\n\nEnter your 10-digit mobile number.",
    [["🔄 Try Another Offer"], ["❌ Cancel"]],
)

OTP_WAIT = Screen("📩 OTP on its way to your number.", [["🔄 Change Number"]])


# ---------------------------------------------------------------------------
# Fake Telethon
# ---------------------------------------------------------------------------

class FakeButton(object):
    def __init__(self, text):
        self.text = text


class FakeMessage(object):
    def __init__(self, fake, text, buttons):
        self._fake = fake
        self.id = fake.next_id
        fake.next_id += 1
        self.text = text
        self.edit_date = None
        self.buttons = [[FakeButton(label) for label in row] for row in (buttons or [])]

    async def click(self, i=0, j=0):
        label = self.buttons[i][j].text
        self._fake.taps.append(label)
        await self._fake.on_tap(label)


class FakeBot(object):
    """Minimal checker state machine (plus a couple of login screens)."""

    def __init__(self, check_button=True, results=None, checking_polls=0,
                 command="/check {number}", checker_copy="standard",
                 start_state="menu", default_result=None):
        self.taps = []
        self.messages = []
        self.next_id = 1000
        self.check_button = check_button
        self.results = dict(results or {})       # number -> bool (verdict)
        self.default_result = default_result     # verdict for unlisted numbers (None = unreadable)
        self.checking_polls = checking_polls     # polls the transient stays
        self.command = command
        self.checker_copy = checker_copy
        self.sent_numbers = []                   # numbers typed into the checker
        self.sent_texts = []                     # every plain message sent
        self.state = start_state
        self.active_checks = 0
        self.max_concurrent_checks = 0
        self._pending_result = None
        self._push(MAIN_MENU if check_button else MAIN_MENU_NO_CHECKER)

    # -- helpers -----------------------------------------------------------

    def _push(self, screen, edited=False):
        message = FakeMessage(self, screen.text, screen.buttons)
        if edited and self.messages:
            message.id = self.messages[-1].id
            message.edit_date = time.time()
            self.messages[-1] = message
        else:
            self.messages.append(message)
        return message

    def _edit_last(self, screen):
        return self._push(screen, edited=True)

    @property
    def last(self):
        return self.messages[-1]

    def _result_screen(self, number, verdict):
        if self.checker_copy == "caps":
            text = ("🔍 NUMBER CHECK 🔍\n\n" + number + "\n\n"
                    + ("✅ ALREADY REGISTERED ON MEESHO" if verdict
                       else "❌ NOT REGISTERED — AVAILABLE"))
            return Screen(text, [["🏠 Main Menu"]])
        if verdict:
            return Screen(RESULT_REGISTERED.text.replace("9876543210", number),
                          RESULT_REGISTERED.buttons)
        return Screen(RESULT_NOT_REGISTERED.text.replace("9876543211", number),
                      RESULT_NOT_REGISTERED.buttons)

    # -- bot behaviour -----------------------------------------------------

    async def on_tap(self, label):
        if "Check Number" in label or "Number Status" in label:
            self.state = "check_prompt"
            self._edit_last(CHECK_PROMPT)
        elif "Main Menu" in label:
            self.state = "menu"
            self._edit_last(MAIN_MENU if self.check_button else MAIN_MENU_NO_CHECKER)
        elif "Cancel" in label:
            self.state = "menu"
            self._edit_last(MAIN_MENU if self.check_button else MAIN_MENU_NO_CHECKER)

    async def on_message(self, text):
        stripped = text.strip()
        self.sent_texts.append(stripped)
        if self.command and stripped.startswith(self.command.split("{")[0].strip()):
            number = "".join(ch for ch in stripped if ch.isdigit())[-10:]
            await self._run_check(number)
            return
        if stripped.isdigit() and len(stripped) == 10 and self.state == "check_prompt":
            self.sent_numbers.append(stripped)
            await self._run_check(stripped)
            return
        if stripped == "/start":
            self.state = "menu"
            self._push(MAIN_MENU if self.check_button else MAIN_MENU_NO_CHECKER)

    async def _run_check(self, number):
        self.active_checks += 1
        self.max_concurrent_checks = max(self.max_concurrent_checks, self.active_checks)
        try:
            if self.checking_polls:
                self.state = "checking"
                self._push(CHECKING)
                self._pending_result = (number, self.checking_polls)
            else:
                self._finish_check(number)
        finally:
            self.active_checks -= 1

    def _finish_check(self, number):
        verdict = self.results.get(number, self.default_result)
        if verdict is None:
            self.state = "unknown"
            self._push(RESULT_UNREADABLE)
            return
        self.state = "result"
        self._push(self._result_screen(number, verdict))


class FakeTelegramClient(object):
    def __init__(self, bot):
        self.bot = bot

    async def get_messages(self, entity, limit=6):
        bot = self.bot
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
        await self.bot.on_message(text)
        return self.bot.last


def build_client(checker_bot_conf=None, meesho_bot_conf=None, **bot_kwargs):
    config = {
        "meesho_bot": {
            "enabled": True,
            "api_id": 1,
            "api_hash": "x",
            "session_file": "userbot.session.txt",
            "bot_username": "@primesbot",
            "target_upi_price": 47,
            "max_offer_rerolls": 5,
            "step_timeout_seconds": 3,
            "poll_interval_seconds": 0.01,
            "human_delay_seconds": [0, 0],
            **(meesho_bot_conf or {}),
        },
        "checker": {"bot": {"step_timeout_seconds": 2, **(checker_bot_conf or {})}},
    }
    bot = FakeBot(**bot_kwargs)
    client = MeeshoBotClient(config, log_fn=lambda *_: None)
    client._client = FakeTelegramClient(bot)
    client._bot_entity = "@primesbot"
    client._loop = asyncio.new_event_loop()

    def _run(coro, timeout=None):
        return client._loop.run_until_complete(coro)

    client._run = _run
    return client, bot


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
# Screen-level (pure) checks
# ---------------------------------------------------------------------------

def test_screen_verdicts():
    cases = [
        ("✅ This number is already registered on Meesho.", True),
        ("❌ This number is not registered on Meesho.", False),
        ("NOT REGISTERED — AVAILABLE", False),
        ("✅ ALREADY REGISTERED ON MEESHO", True),
        ("This number is registered with Meesho since 2021.", True),
        ("This number is not registered.", False),
        ("❌ No account found for this number on Meesho.", False),
        ("Great news! This number has no Meesho account yet.", False),
        ("This number already has an account.", True),
        ("🟢 Number 9876543210 registered ✅", True),
        ("Result: ❌ not linked to any Meesho account", False),
        ("Registration status: not found in our records", False),
        ("🔍 Checking the registration status, please wait...", None),
        ("Send the 10-digit number to check its registration", None),
        ("Choose login mode", None),
    ]
    for text, expected in cases:
        verdict = Screen(text).check_verdict()
        check(f"verdict: {text[:44]!r} -> {expected}", verdict == expected, f"got {verdict}")

    # Configured extra wording resolves a bot revision with new copy.
    free = Screen("Ye number free hai, naya account bana sakte hain")
    check("verdict: unknown copy is None without hints", free.check_verdict() is None,
          free.check_verdict())
    check("verdict: config hints resolve unknown copy",
          free.check_verdict(not_registered_hints=["free hai"]) is False,
          free.check_verdict(not_registered_hints=["free hai"]))
    taken = Screen("Ye number pehle se Meesho par hai")
    check("verdict: config hints for the registered copy",
          taken.check_verdict(registered_hints=["pehle se"]) is True,
          taken.check_verdict(registered_hints=["pehle se"]))

    check("check prompt: detected",
          Screen("Send the 10-digit mobile number you want to check.").looks_like_check_prompt())
    check("check prompt: not a menu", not MAIN_MENU.looks_like_check_prompt())
    check("check prompt: not an offer", not OFFER.looks_like_check_prompt())
    check("checking: transient detected", CHECKING.looks_like_checking())
    check("checking: result is not transient", not RESULT_REGISTERED.looks_like_checking())

    # The exclusion list keeps "Check Balance" out of the checker entries.
    pick = MAIN_MENU_WITH_BALANCE.checker_button()
    check("entry: 'Check Balance' is not the checker", pick is None, pick)
    pick = MAIN_MENU.checker_button()
    check("entry: '🔍 Check Number' picked", pick is not None and "Check Number" in pick[2], pick)
    pick = Screen("Menu", [["🔎 Number Check"], ["🏠 Main Menu"]]).checker_button()
    check("entry: fallback hint picks 'Number Check'", pick is not None and "Number Check" in pick[2], pick)

    check("classify_check: result", RESULT_REGISTERED.classify_check() == S_CHECK_RESULT)
    check("classify_check: prompt", CHECK_PROMPT.classify_check() == S_CHECK_PROMPT)
    check("classify_check: checking", CHECKING.classify_check() == S_CHECKING)


# ---------------------------------------------------------------------------
# Flow-level checks (fake Telethon)
# ---------------------------------------------------------------------------

def test_registered_number():
    client, bot = build_client(results={"9876543210": True})
    result = client.check_registration("9876543210")
    check("registered: verdict True", result["is_registered"] is True, result)
    check("registered: source tagged bot", result.get("source") == "bot", result)
    check("registered: checker button tapped", any("Check Number" in t for t in bot.taps), bot.taps)
    check("registered: number sent once", bot.sent_numbers == ["9876543210"], bot.sent_numbers)
    check("registered: back at the main menu", bot.state == "menu", bot.state)


def test_not_registered_number():
    client, bot = build_client(results={"9876543212": False})
    result = client.check_registration("9876543212")
    check("not registered: verdict False", result["is_registered"] is False, result)
    check("not registered: number sent", bot.sent_numbers == ["9876543212"], bot.sent_numbers)


def test_transient_checking_screen():
    client, bot = build_client(results={"9876543213": True}, checking_polls=3)
    result = client.check_registration("9876543213")
    check("checking transient: waited for the result", result["is_registered"] is True, result)


def test_caps_copy_variant():
    client, bot = build_client(results={"9876543214": True, "9876543215": False},
                               checker_copy="caps")
    check("caps copy: registered", client.check_registration("9876543214")["is_registered"] is True)
    check("caps copy: not registered", client.check_registration("9876543215")["is_registered"] is False)


def test_number_never_leaks_into_login_screens():
    # The bot sits on the offer (login) screen when the check starts: the flow
    # must reset to the menu first, and the number may only ever be typed at
    # the checker prompt.
    client, bot = build_client(results={"9876543216": True}, start_state="offer")
    bot._push(OFFER)
    result = client.check_registration("9876543216")
    check("safety: verdict still read", result["is_registered"] is True, result)
    check("safety: number typed exactly once", bot.sent_numbers == ["9876543216"], bot.sent_numbers)
    check("safety: only the number was typed",
          all(t == "9876543216" for t in bot.sent_numbers), bot.sent_texts)


def test_unreadable_result():
    client, bot = build_client(default_result=None)
    error = expect_error("unreadable: raises with the screen text",
                         lambda: client.check_registration("9876543217"),
                         MeeshoBotUnknownScreen, "no readable result")
    if error is not None:
        check("unreadable: screen text attached", "something went wrong" in error.screen_text.lower(),
              error.screen_text)
        check("unreadable: buttons attached", "Main Menu" in " ".join(error.buttons), error.buttons)


def test_stuck_checking_screen():
    client, bot = build_client(checking_polls=10_000, checker_bot_conf={"step_timeout_seconds": 0.3})
    expect_error("stuck checking: times out with a clear error",
                 lambda: client.check_registration("9876543218"),
                 MeeshoBotUnknownScreen, "checking")


def test_missing_checker_entry():
    client, bot = build_client(check_button=False, command="", results={"9876543219": True})
    error = expect_error("no entry: raises with an actionable message",
                         lambda: client.check_registration("9876543219"),
                         MeeshoBotUnknownScreen, "button_hints")
    if error is not None:
        check("no entry: current buttons listed", "Add Account" in " ".join(error.buttons), error.buttons)
    check("no entry: nothing typed into the bot", bot.sent_texts == [], bot.sent_texts)


def test_command_entry():
    client, bot = build_client(check_button=False, command="/check {number}",
                               checker_bot_conf={"command": "/check {number}"},
                               results={"9876543220": True})
    result = client.check_registration("9876543220")
    check("command: verdict read", result["is_registered"] is True, result)
    check("command: sent as one message", bot.sent_texts == ["/check 9876543220"], bot.sent_texts)
    check("command: no bare number typed", bot.sent_numbers == [], bot.sent_numbers)

    # entry=command forces the command even when a menu button exists.
    client, bot = build_client(check_button=True, command="/status {number}",
                               checker_bot_conf={"entry": "command",
                                                 "command": "/status {number}"},
                               results={"9876543221": False})
    result = client.check_registration("9876543221")
    check("command: entry=command ignores the menu button",
          result["is_registered"] is False and bot.sent_texts == ["/status 9876543221"],
          bot.sent_texts)


def test_custom_hints():
    client, bot = build_client(results={"9876543222": True},
                               checker_bot_conf={"button_hints": ["check number"]})
    check("custom hints: configured button hint works",
          client.check_registration("9876543222")["is_registered"] is True)

    # Extra hints are additive: a stale extra hint cannot break a healthy menu
    # (and must not tap some other button instead).
    client, bot = build_client(results={"9876543223": True},
                               checker_bot_conf={"button_hints": ["open shop (stale)"]})
    result = client.check_registration("9876543223")
    check("custom hints: extra hints are additive", result["is_registered"] is True, result)
    check("custom hints: the checker button was still the one tapped",
          any("Check Number" in t for t in bot.taps) and "Open Shop" not in bot.taps, bot.taps)


def test_prompt_hint_variant():
    client, bot = build_client(results={"9876543224": False})
    # Replace the standard prompt with the variant copy.
    original = FakeBot.on_tap

    async def on_tap(self, label):
        if "Check Number" in label:
            bot.state = "check_prompt"
            bot._edit_last(CHECK_PROMPT_VARIANT)
            return
        await original(self, label)

    FakeBot.on_tap = on_tap
    try:
        result = client.check_registration("9876543224")
        check("prompt variant: detected by wording", result["is_registered"] is False, result)
    finally:
        FakeBot.on_tap = original


def test_reset_after_check_config():
    client, bot = build_client(results={"9876543225": True},
                               checker_bot_conf={"reset_after_check": False})
    client.check_registration("9876543225")
    check("reset off: bot left on the result screen", bot.state == "result", bot.state)


def test_checks_are_serialized():
    # Two workers checking at the same time must not interleave inside the one
    # bot conversation: the flow lock serializes them.
    client, bot = build_client(results={"9876543226": True, "9876543227": False},
                               checking_polls=2)
    results = []
    errors = []

    def run(number):
        try:
            results.append(client.check_registration(number))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(number,))
               for number in ("9876543226", "9876543227")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    check("serialized: both checks succeeded", len(results) == 2 and not errors, errors)
    check("serialized: never two checks in flight", bot.max_concurrent_checks == 1,
          bot.max_concurrent_checks)
    check("serialized: verdicts match the numbers",
          sorted(r["is_registered"] for r in results) == [False, True], results)


def main():
    test_screen_verdicts()
    test_registered_number()
    test_not_registered_number()
    test_transient_checking_screen()
    test_caps_copy_variant()
    test_number_never_leaks_into_login_screens()
    test_unreadable_result()
    test_stuck_checking_screen()
    test_missing_checker_entry()
    test_command_entry()
    test_custom_hints()
    test_prompt_hint_variant()
    test_reset_after_check_config()
    test_checks_are_serialized()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All bot checker flow checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
