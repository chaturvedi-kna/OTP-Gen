"""
Replay of the PRIMES Meesho bot login flow (incl. the referral screens) against
the real MeeshoBotClient, using a fake Telethon client.

No Telegram connection, no network, no Telethon install needed:

    python test_primes_referral_flow.py

The fake bot speaks the exact screens from the screenshots/recordings, so the
navigation, referral handling and OTP submission paths are exercised end to end.
"""

import asyncio
import sys
import time

from meesho_bot_client import (
    MeeshoBotClient,
    MeeshoBotUnknownScreen,
    Screen,
    S_MENU,
    S_REFERRAL,
)


# ---------------------------------------------------------------------------
# Screens (labels/text as shown by the real bot)
# ---------------------------------------------------------------------------

MAIN_MENU = Screen(
    "👋 Welcome to Meesho Primes Bot\n\nWhat would you like to do?",
    [["➕ Add Account"], ["🏠 Open Shop"]],
)

LINK_CHOICE = Screen(
    "How would you like to link your Meesho account?",
    [["📱 Login with Number"], ["🔗 Login with Link"]],
)

# The screen from the screenshot the user attached.
REFERRAL_SET = Screen(
    "🔗 Set Refer Link\n\n"
    "You haven't saved a referral link yet.\n\n"
    "Paste your Meesho referral link once and I'll use it automatically every "
    "time you add an account — no need to enter it again.\n\n"
    "e.g. https://app.meesho.com/...?via=...",
    [["🏠 Main Menu"]],
)

# The screen from the original Telegram alert (monospace / markdown copy).
REFERRAL_ASK = Screen(
    "🎁 **Referral link?**\n\n"
    "Paste your Meesho **referral link**\n"
    "_(e.g. https://app.meesho.com/...?via=...)_\n\n"
    "Don't have one? Tap below.",
    [["🚫 I don't have a refer code"], ["❌ Cancel"]],
)

REFERRAL_ACCEPTED = Screen(
    "✅ Referral link saved!\n\nNow choose how you want to log in.",
    [["Normal"], ["Auto"]],
)

REFERRAL_REJECTED = Screen(
    "❌ That referral link looks invalid or has expired.\n\n"
    "Paste a valid Meesho referral link, or skip.",
    [["🚫 I don't have a refer code"]],
)

REFERRAL_ACK = Screen(
    "✅ Referral link saved! Continuing with your login...",
    [["▶️ Continue"]],
)

LOGIN_MODE = Screen(
    "Choose login mode",
    [["Normal"], ["Auto"]],
)

OFFER_60 = Screen(
    "🎁 Special offer for you!\n\nUPI · ₹60\n\nEnter your 10-digit mobile number.",
    [["🔄 Try Another Offer"]],
)

OFFER_45 = Screen(
    "🎁 Special offer for you!\n\nUPI · ₹45\n\nEnter your 10-digit mobile number.",
    [["🔄 Try Another Offer"]],
)

OTP_WAIT = Screen(
    "✅ OTP on its way!\n\nWe've sent a code to 9xxxxxxxxx. Type it here when it arrives.",
)

LINKED = Screen(
    "🎉 Account linked!\n\nUser ID · 123456789\nAccount # 4242\nMobile · 9876543210",
)

BLOCKED = Screen(
    "🚫 This number is blocked on Meesho. Please use another number.",
)


# ---------------------------------------------------------------------------
# Fake Telethon
# ---------------------------------------------------------------------------

class FakeButton:
    def __init__(self, text, msg, index):
        self.text = text
        self._msg = msg
        self._index = index


class FakeMessage:
    def __init__(self, fake, text, buttons):
        self._fake = fake
        self.id = fake.next_id
        fake.next_id += 1
        self.text = text
        self.edit_date = None
        self.buttons = [
            [FakeButton(label, self, (r, c)) for c, label in enumerate(row)]
            for r, row in enumerate(buttons or [])
        ]

    async def click(self, i=0, j=0):
        label = self.buttons[i][j].text
        self._fake.taps.append(label)
        await self._fake.on_tap(label)


class FakeBot:
    """
    Minimal state machine that answers taps/messages the way the real bot does.
    `referral_script` decides what the referral step does.
    """

    def __init__(self, referral_link="", referral_script="save",
                 offer_prices=(60, 45), referral_reask=False):
        self.tapped = []
        self.taps = self.tapped  # alias used by FakeMessage
        self.messages = []
        self.next_id = 1000
        self.referral_link = referral_link
        self.referral_script = referral_script
        self.offer_prices = list(offer_prices)
        self.referral_reask = referral_reask
        self.referral_seen = 0
        self.pasted = []
        self.sent_numbers = []
        self.sent_codes = []
        self.state = "menu"
        self._push(MAIN_MENU)

    # -- helpers ----------------------------------------------------------

    def _push(self, screen, edited=False):
        msg = FakeMessage(self, screen.text, screen.buttons)
        if edited and self.messages:
            msg.id = self.messages[-1].id
            msg.edit_date = time.time()
            self.messages[-1] = msg
        else:
            self.messages.append(msg)
        return msg

    def _edit_last(self, screen):
        return self._push(screen, edited=True)

    @property
    def last(self):
        return self.messages[-1]

    # -- bot behaviour ----------------------------------------------------

    async def on_tap(self, label):
        if "Add Account" in label:
            self.state = "link_choice"
            self._edit_last(LINK_CHOICE)
        elif "Login with Number" in label:
            self.state = "referral"
            self.referral_seen += 1
            if getattr(self, "referral_override", None) is not None:
                self._edit_last(self.referral_override)
            else:
                self._edit_last(REFERRAL_ASK if self.referral_link is None else REFERRAL_SET)
        elif label.startswith("🏠 Main Menu"):
            self.state = "menu"
            self._edit_last(MAIN_MENU)
        elif "don't have a refer code" in label or "Don't have one" in label:
            self.state = "login_mode"
            self._edit_last(LOGIN_MODE)
        elif "Continue" in label and self.state == "login_mode":
            self._edit_last(LOGIN_MODE)
        elif label == "Normal" and self.state == "login_mode":
            self.state = "offer"
            self._edit_last(self._offer_screen())
        elif "Try Another Offer" in label:
            if self.offer_prices:
                self.offer_prices.pop(0)
            self._edit_last(self._offer_screen())
        elif label == "Cancel":
            self.state = "menu"
            self._edit_last(MAIN_MENU)

    def _offer_screen(self):
        price = self.offer_prices[0] if self.offer_prices else 45
        base = OFFER_60 if price == 60 else OFFER_45
        return Screen(base.text.replace("₹60", f"₹{price}").replace("₹45", f"₹{price}"),
                      base.buttons)

    async def on_message(self, text):
        stripped = text.strip()
        if "app.meesho.com" in stripped or "?via=" in stripped:
            self.pasted.append(stripped)
            self.state = "login_mode"
            if self.referral_script == "reject":
                self._push(REFERRAL_REJECTED)
            elif self.referral_script == "silent_reask":
                self._push(REFERRAL_ASK)
            elif self.referral_script == "two_screens":
                self._push(REFERRAL_ASK)
            elif self.referral_script == "ack_then_login":
                self._push(REFERRAL_ACK)
            else:
                self._push(REFERRAL_ACCEPTED)
            return
        if stripped.startswith("/start"):
            self.state = "menu"
            self._push(MAIN_MENU)
            return
        if stripped.isdigit() and len(stripped) == 10:
            self.sent_numbers.append(stripped)
            if stripped.startswith("9999"):
                self.state = "blocked"
                self._push(BLOCKED)
            else:
                self.state = "otp_wait"
                self._push(OTP_WAIT)
            return
        if stripped.isdigit() and len(stripped) <= 6:
            self.sent_codes.append(stripped)
            if stripped == "111111":
                self.state = "linked"
                self._push(LINKED)
            else:
                self.state = "otp_wait"
                self._push(Screen("❌ Incorrect code. Please try again."))
            return


class FakeTelegramClient:
    def __init__(self, bot):
        self.bot = bot

    async def get_messages(self, entity, limit=6):
        return list(reversed(self.bot.messages[-limit:]))

    async def send_message(self, entity, text):
        await self.bot.on_message(text)
        return self.bot.last


def build_client(referral_link="", referral_script="save", **kwargs):
    config = {
        "meesho_bot": {
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
            "referral_link": referral_link,
            **kwargs,
        }
    }
    bot = FakeBot(referral_link=referral_link, referral_script=referral_script)
    client = MeeshoBotClient(config, log_fn=lambda *_: None)
    client._client = FakeTelegramClient(bot)
    client._bot_entity = "@primesbot"
    client._loop = asyncio.new_event_loop()

    def _run(coro, timeout=None):
        return client._loop.run_until_complete(coro)

    client._run = _run
    return client, bot


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def scenario_with_link():
    client, bot = build_client(referral_link="https://app.meesho.com/2yoV/r99th0qd?via=852o6g")
    res = client.prepare_login("9876543210")
    check("with link: flow reaches OTP screen", res["stage"] == "otp_sent", res)
    check("with link: link pasted once", bot.pasted == ["https://app.meesho.com/2yoV/r99th0qd?via=852o6g"], bot.pasted)
    check("with link: referral screen was seen", bot.referral_seen == 1, bot.referral_seen)
    check("with link: skip button not used", not any("refer code" in t for t in bot.tapped), bot.tapped)
    check("with link: number sent after referral", bot.sent_numbers == ["9876543210"], bot.sent_numbers)
    check("with link: offer rerolled to target", res["rerolls"] == 1 and res["upi"] == 45.0, res)
    check("with link: tap order logged", res["referral_action"] == "pasted referral link", res["referral_action"])
    return client, bot


def scenario_without_link():
    client, bot = build_client(referral_link=None)
    res = client.prepare_login("9876543211")
    check("without link: flow reaches OTP screen", res["stage"] == "otp_sent", res)
    check("without link: skip option tapped", "🚫 I don't have a refer code" in bot.tapped, bot.tapped)
    check("without link: nothing pasted", bot.pasted == [], bot.pasted)
    check("without link: number sent", bot.sent_numbers == ["9876543211"], bot.sent_numbers)
    check("without link: skip action recorded",
          res["referral_action"] == "tapped '🚫 I don't have a refer code'", res["referral_action"])


def scenario_two_screens_like_screenshot():
    """
    The exact sequence from the screenshot: 🔗 Set Refer Link (only a Main Menu
    button) -> paste the link -> the bot replies 🎁 Referral link? with a skip
    button -> tap it -> Normal -> offer -> number.
    """
    client, bot = build_client(
        referral_link="https://app.meesho.com/2yoV/r99th0qd?via=852o6g",
        referral_script="two_screens",
    )
    res = client.prepare_login("9876543210")
    check("screenshot sequence: flow reaches OTP screen", res["stage"] == "otp_sent", res)
    check("screenshot sequence: link pasted once", len(bot.pasted) == 1, bot.pasted)
    check("screenshot sequence: skip used on the second screen",
          "🚫 I don't have a refer code" in bot.tapped, bot.tapped)
    check("screenshot sequence: number only after both screens",
          bot.sent_numbers == ["9876543210"], bot.sent_numbers)
    check("screenshot sequence: price target respected", res["upi"] == 45.0, res)
    check("screenshot sequence: referral surfaced in the result",
          res["referral_action"] == "tapped '🚫 I don't have a refer code'",
          res["referral_action"])


def scenario_acknowledgement_screen():
    """"✅ Referral link saved! [▶️ Continue]" must be dismissed, not answered."""
    client, bot = build_client(referral_link="https://app.meesho.com/x?via=1",
                               referral_script="ack_then_login")
    res = client.prepare_login("9876543211")
    check("ack screen: confirmation dismissed with its own button",
          "▶️ Continue" in bot.tapped, bot.tapped)
    check("ack screen: skip option not tapped", not any("refer code" in t for t in bot.tapped),
          bot.tapped)
    check("ack screen: flow reaches OTP screen", res["stage"] == "otp_sent", res)
    check("ack screen: link pasted once", len(bot.pasted) == 1, bot.pasted)


def scenario_link_rejected():
    client, bot = build_client(referral_link="https://app.meesho.com/bad?via=000",
                               referral_script="reject")
    res = client.prepare_login("9876543212")
    check("rejected link: flow still reaches OTP screen", res["stage"] == "otp_sent", res)
    check("rejected link: skip used as fallback",
          "🚫 I don't have a refer code" in bot.tapped, bot.tapped)
    check("rejected link: number sent", bot.sent_numbers == ["9876543212"], bot.sent_numbers)


def scenario_link_silently_reasked():
    client, bot = build_client(referral_link="https://app.meesho.com/bad?via=000",
                               referral_script="silent_reask")
    res = client.prepare_login("9876543213")
    check("silent re-ask: flow still reaches OTP screen", res["stage"] == "otp_sent", res)
    check("silent re-ask: link pasted only once", len(bot.pasted) == 1, bot.pasted)
    check("silent re-ask: skip used after re-ask",
          "🚫 I don't have a refer code" in bot.tapped, bot.tapped)


def scenario_unknown_referral_screen():
    """
    Unrecognisable referral copy inside the flow: with no configured link and
    no skip button there is nothing to tap, so the flow must raise a loud,
    informative error instead of cancelling silently or typing a number into
    the referral field.
    """
    client, bot = build_client(referral_link=None)
    mystery = Screen("🎁 Referral link? Paste your Meesho referral link below.",
                     [["✅ Yes, I have a referral link"]])
    client._referral_screen_override = mystery
    bot.referral_override = mystery  # what the bot shows after "Login with Number"
    try:
        client.prepare_login("9876543214")
        check("unknown referral: raises instead of mis-tapping", False, "no exception")
    except MeeshoBotUnknownScreen as exc:
        check("unknown referral: raises with screen text",
              "Referral link" in exc.screen_text, exc.screen_text)
        check("unknown referral: error names the config key",
              "referral_link" in str(exc), str(exc))
        check("unknown referral: buttons reported",
              any("Yes, I have" in b for b in exc.buttons), exc.buttons)
    check("unknown referral: no number typed into the referral field",
          bot.sent_numbers == [], bot.sent_numbers)


def scenario_menu_refer_earn_not_mistaken():
    """A 'Refer & Earn' menu entry must not look like the referral prompt."""
    menu = Screen("Main Menu\n\n➕ Add Account\n🎁 Refer & Earn\n🏠 Open Shop",
                  [["➕ Add Account", "🎁 Refer & Earn"], ["🏠 Open Shop"]])
    check("menu 'Refer & Earn' is not a referral screen",
          menu.classify() == S_MENU and not menu.is_referral,
          f"{menu.classify()} / referral={menu.is_referral}")

    offer_with_referral_line = Screen(
        "🎁 Offer\n\nUPI · ₹45\nRefer a friend and earn!\n\nEnter your 10-digit mobile number.",
        [["🔄 Try Another Offer"]])
    check("offer screen with referral copy stays an offer",
          offer_with_referral_line.classify() == "offer"
          and not offer_with_referral_line.is_referral,
          offer_with_referral_line.classify())


def scenario_referral_mid_otp_wait():
    """
    The bot re-asking for a referral link after the number was sent: the code
    must never be typed into the referral field.
    """
    client, bot = build_client(referral_link="https://app.meesho.com/x?via=1")
    client.prepare_login("9876543215")
    bot.messages.append(FakeMessage(bot, REFERRAL_ASK.text, REFERRAL_ASK.buttons))
    try:
        res = client.submit_otp("111111")
        check("mid-flow referral: code not dumped into the referral field",
              bot.sent_codes == [], f"res={res} codes={bot.sent_codes}")
    except MeeshoBotUnknownScreen as exc:
        check("mid-flow referral: raises so the caller can recover",
              "not waiting for the OTP code" in str(exc), str(exc))
    check("mid-flow referral: no code delivered to the bot", bot.sent_codes == [], bot.sent_codes)

    # Overlay case: the bot answers the referral prompt and is still waiting for
    # the code, so the code is submitted normally.
    client2, bot2 = build_client(referral_link="https://app.meesho.com/x?via=1")
    client2.prepare_login("9876543216")
    bot2.messages.append(FakeMessage(bot2, REFERRAL_ACK.text, REFERRAL_ACK.buttons))
    res2 = client2.submit_otp("111111")
    check("mid-flow referral (ack overlay): code submitted and linked",
          res2["status"] == "linked" and bot2.sent_codes == ["111111"], res2)


def scenario_change_number_with_referral():
    """Change Number recovery, including landing on a referral prompt."""
    client, bot = build_client(referral_link="https://app.meesho.com/x?via=1")
    client.prepare_login("9876543216")
    check("change number: number sent by the first login",
          bot.sent_numbers == ["9876543216"], bot.sent_numbers)

    # (a) The bot wandered onto the referral prompt instead of the number
    # prompt: asking for a Change Number must not type a number into it.
    bot.messages.append(FakeMessage(bot, REFERRAL_ASK.text, REFERRAL_ASK.buttons))
    sent_before = list(bot.sent_numbers)
    try:
        res = client.continue_with_number("9876543217")
        check("change number: referral prompt resolved before typing",
              bot.sent_numbers != sent_before or res.get("stage") != "otp_sent",
              f"res={res} sent={bot.sent_numbers}")
    except MeeshoBotUnknownScreen as exc:
        check("change number: unresolved prompt raises instead of typing",
              "number prompt" in str(exc), str(exc))
    check("change number: number not dumped into the referral field",
          bot.sent_numbers == sent_before, bot.sent_numbers)

    # (b) The bot is genuinely at the number prompt again after Change Number:
    # the replacement number is typed and the SMS is requested.
    bot.state = "offer"
    bot.offer_prices = [45]
    bot._edit_last(bot._offer_screen())
    res2 = client.continue_with_number("9876543218")
    check("change number: replacement number sent",
          bot.sent_numbers[-1] == "9876543218", bot.sent_numbers)
    check("change number: flow reaches OTP screen", res2["stage"] == "otp_sent", res2)

    # (c) Already-linked / already-waiting screens are reported, not re-typed.
    bot.state = "otp_wait"
    bot._edit_last(OTP_WAIT)
    sent_before = list(bot.sent_numbers)
    res3 = client.continue_with_number("9876543219")
    check("change number: no duplicate number when OTP already pending",
          res3["stage"] == "otp_sent" and bot.sent_numbers == sent_before,
          f"res={res3} sent={bot.sent_numbers}")


def scenario_screen_parsing():
    s = REFERRAL_SET
    check("screenshot screen classified as referral", s.classify() == S_REFERRAL, s.classify())
    check("screenshot screen needs a decision", s.referral_prompt, s.referral_prompt)
    check("'Main Menu' is not a skip option for the referral screen",
          s.referral_skip_button() is None, s.referral_skip_button())
    check("alert screen offers its own skip option",
          REFERRAL_ASK.referral_skip_button()[2] == "🚫 I don't have a refer code",
          REFERRAL_ASK.referral_skip_button())
    check("'Normal' login mode is not mistaken for 'No'",
          Screen("Choose login mode", [["Normal"], ["Auto"]]).referral_skip_button() is None)


def main():
    print("=== PRIMES referral-flow replay ===\n")
    scenario_screen_parsing()
    scenario_with_link()
    scenario_two_screens_like_screenshot()
    scenario_acknowledgement_screen()
    scenario_without_link()
    scenario_link_rejected()
    scenario_link_silently_reasked()
    scenario_unknown_referral_screen()
    scenario_menu_refer_earn_not_mistaken()
    scenario_referral_mid_otp_wait()
    scenario_change_number_with_referral()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
