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
import threading
import time

from meesho_bot_client import (
    MeeshoBotClient,
    MeeshoBotError,
    MeeshoBotReferralError,
    MeeshoBotTimeout,
    MeeshoBotUnknownScreen,
    Screen,
    S_MENU,
    S_REFERRAL,
    S_OTP_WAIT,
    S_SENDING_OTP,
    S_VERIFYING,
    S_WRONG_OTP,
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

# The three-button variant some bot revisions show instead of an offer
# ("⚠️ Failed to fetch offer"), transcribed exactly from the screenshot.
# NOTE: it still carries price lines (Bucket · ₹135, UPI · ₹83) with
# "Offer · Null" - a decoy price that must NEVER be accepted as an offer.
# The flow must tap "Try Again" exactly like "Try Another Offer", and must
# never tap "Continue without offer" or "Cancel".
OFFER_RETRY = Screen(
    "⚠️ Failed to fetch offer.\n\n"
    "Couldn't load your first-order offer right now. Tap 🔄 Try Again to retry, "
    "or continue without it.\n\n"
    "📊 Offer details\n• Bucket · ₹135\n• Offer · Null\n\n"
    "🛍️ Sattu 1Kg | Sattu Drink Mix (100% Natural)\n• Original · ₹454\n"
    "• Final · ₹118\n• UPI · ₹83",
    [["🔄 Try Again"], ["➡️ Continue without offer"], ["❌ Cancel"]],
)

# Transient screens the bot shows while working:
#   "Setting things up..." - after a tap (Normal / Try Another Offer /
#   Try Again), before the offer appears;
#   "Sending your OTP..." - after the number is sent, before OTP on its way;
#   "Verifying your code..." - after the code is sent, before the linked
#   screen (or a wrong-code error) appears.
SETTING_UP = Screen("⚙️ Setting things up...\n\nFinding the best offer for you.")
SENDING_OTP = Screen("⏳ Sending your OTP…")
VERIFYING = Screen("🔎 Verifying your code...")

OTP_WAIT = Screen(
    "✅ OTP on its way!\n\nWe've sent a code to 9xxxxxxxxx. Type it here when it arrives.",
    [["🔄 Change Number"]],
)

LINKED = Screen(
    "🎉 Account linked!\n\nUser ID · 123456789\nAccount # 4242\nMobile · 9876543210",
)

BLOCKED = Screen(
    "🚫 This number is blocked on Meesho. Please use another number.",
)

# A number prompt whose copy/buttons classify() does NOT recognise as an offer
# (no "Try Another Offer" button, no "10-digit mobile ... continue"): this is
# the screen that used to end the Change Number recovery with
# "Expected number prompt after Change Number, got unknown" and a full restart
# from the main menu.
PROMPT_ALT_COPY = Screen(
    "📱 Enter the mobile number you want to link\n\n"
    "UPI · ₹45\n\n"
    "Make sure the number is active - the OTP is sent right away.",
    [["❌ Cancel"]],
)

# A prompt only recognizable through meesho_bot.number_prompt_hints.
PROMPT_LOCAL_COPY = Screen("📱 Apna number daalein", [["❌ Cancel"]])

# A dead end that is neither a prompt nor a known screen: the recovery must
# give up (bounded) instead of sitting on it forever or silently resetting.
BROKEN_SCREEN = Screen(
    "⚠️ Something went wrong on our side.\n\nPlease try again later.",
    [["🏠 Main Menu"]],
)

# The bot CHECKER's number prompt: same "send the number" wording, no offer
# marker - it must never be mistaken for the login prompt by a cold read.
CHECK_PROMPT_ONLY = Screen(
    "📱 Send the number you want to check",
    [["🏠 Main Menu"]],
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
                 offer_prices=(60, 45), referral_reask=False,
                 retry_after=None, variant_first=False,
                 otp_transition="instant", setup_interstitial=False,
                 code_transition="instant", change_number_screen=None,
                 ignore_change_taps=0):
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
        # "Try Again" variant: shown instead of the offer after the
        # `retry_after`-th "Try Another Offer" tap (1-based), or instead of
        # the first offer right after tapping Normal (variant_first).
        self.retry_after = retry_after
        self.variant_first = variant_first
        self.reroll_count = 0
        # otp_transition: "instant" (directly to OTP on its way), "delayed"
        # (transient "Sending your OTP…" first) or "stuck" (transient forever).
        self.otp_transition = otp_transition
        self._pending_otp = 0  # get_messages polls that still show the transient
        # code_transition: "instant" (code -> directly to the outcome),
        # "verifying" (transient "🔎 Verifying your code…" first, seen for a
        # couple of polls) or "stuck_verify" (transient forever).
        self.code_transition = code_transition
        self._pending_linked = 0  # polls that still show the verifying transient
        self._pending_offer = None  # "setup" = show the offer on the next poll
        self.setup_interstitial = setup_interstitial
        # Change Number scripting: the screen it answers with (default: the
        # regular offer) and how many taps it ignores first.
        self.change_number_screen = change_number_screen
        self.ignore_change_taps = ignore_change_taps
        self.change_taps = 0
        self.ignored_change_taps = 0
        # How many times the flow was hard-reset with /start, and how many full
        # menu walks (Add Account) it paid for.
        self.starts = 0
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
            if self.referral_script == "absent":
                # The referral screen is optional - on many logins it never shows.
                self.state = "login_mode"
                self._edit_last(LOGIN_MODE)
                return
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
            if self.variant_first:
                self.variant_first = False
                self._edit_last(OFFER_RETRY)
            else:
                self._edit_last(self._offer_screen())
        elif "Change Number" in label:
            self.change_taps += 1
            if self.ignore_change_taps > 0:
                # The bot ignores the tap and re-posts its OTP screen: the
                # recovery must retry instead of giving up on the flow.
                self.ignore_change_taps -= 1
                self.ignored_change_taps += 1
                self._edit_last(OTP_WAIT)
                return
            self.state = "offer"
            self._edit_last(self.change_number_screen or self._offer_screen())
        elif "Try Another Offer" in label:
            self.reroll_count += 1
            if self.offer_prices:
                self.offer_prices.pop(0)
            if self.retry_after == self.reroll_count:
                self.retry_after = None
                self._edit_last(OFFER_RETRY)
                return
            if self.setup_interstitial:
                self._pending_offer = "setup"
                self._edit_last(SETTING_UP)
                return
            self._edit_last(self._offer_screen())
        elif "Try Again" in label:
            # The three-button variant: the tap rerolls; the next screen is
            # the offer itself (the price was consumed when the variant was
            # shown / the first offer is just delayed).
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
                # Exactly the screenshot sequence: the first paste is saved, then
                # the bot asks again for this account; the second paste proceeds.
                self._push(REFERRAL_ASK if len(self.pasted) == 1 else LOGIN_MODE)
            elif self.referral_script == "ack_then_login":
                self._push(REFERRAL_ACK)
            else:
                self._push(REFERRAL_ACCEPTED)
            return
        if stripped.startswith("/start"):
            self.starts += 1
            self.state = "menu"
            self._push(MAIN_MENU)
            return
        if stripped.isdigit() and len(stripped) == 10:
            self.sent_numbers.append(stripped)
            if stripped.startswith("9999"):
                self.state = "blocked"
                self._push(BLOCKED)
            elif self.otp_transition == "stuck":
                # Stays on the transient forever: the flow must time out with
                # a clear "stuck on 'Sending your OTP…'" error.
                self.state = "sending_otp"
                self._push(SENDING_OTP)
            elif self.otp_transition == "delayed":
                # A couple of polls see the transient, the next shows
                # "OTP on its way" - the flow must settle through it.
                self.state = "sending_otp"
                self._push(SENDING_OTP)
                self._pending_otp = 2
            else:
                self.state = "otp_wait"
                self._push(OTP_WAIT)
            return
        if stripped.isdigit() and len(stripped) <= 6:
            self.sent_codes.append(stripped)
            if stripped == "111111":
                if self.code_transition == "verifying":
                    # A couple of polls see the transient, the next shows
                    # "Account linked!" - the flow must settle through it.
                    self.state = "verifying"
                    self._edit_last(VERIFYING)
                    self._pending_linked = 2
                    return
                if self.code_transition == "stuck_verify":
                    # Stays on the transient forever: the flow must fail with
                    # a clear "stuck on 'Verifying your code…'" error.
                    self.state = "verifying"
                    self._edit_last(VERIFYING)
                    return
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
        # Simulate the bot's delayed in-place edits between a transient
        # screen and the real one (transient seen for a couple of polls,
        # real screen after).
        b = self.bot
        if getattr(b, "_pending_otp", 0) > 0:
            b._pending_otp -= 1
            if b._pending_otp == 0:
                b.state = "otp_wait"
                b._edit_last(OTP_WAIT)
        if getattr(b, "_pending_linked", 0) > 0:
            b._pending_linked -= 1
            if b._pending_linked == 0:
                b.state = "linked"
                b._edit_last(LINKED)
        if getattr(b, "_pending_offer", None) == "setup":
            b._pending_offer = None
            b._edit_last(b._offer_screen())
        return list(reversed(b.messages[-limit:]))

    async def send_message(self, entity, text):
        await self.bot.on_message(text)
        return self.bot.last


def build_client(referral_link="", referral_script="save",
                 referral_failure_action="stop", retry_after=None,
                 variant_first=False, otp_transition="instant",
                 setup_interstitial=False, code_transition="instant",
                 change_number_screen=None, ignore_change_taps=0, **kwargs):
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
            "referral_failure_action": referral_failure_action,
            **kwargs,
        }
    }
    bot = FakeBot(referral_link=referral_link, referral_script=referral_script,
                  retry_after=retry_after, variant_first=variant_first,
                  otp_transition=otp_transition,
                  setup_interstitial=setup_interstitial,
                  code_transition=code_transition,
                  change_number_screen=change_number_screen,
                  ignore_change_taps=ignore_change_taps)
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
    check("with link: screen_state() reports the OTP-wait screen (read-only)",
          client.screen_state() == S_OTP_WAIT, client.screen_state())
    check("with link: screen_state() sent nothing to the bot",
          bot.sent_numbers == ["9876543210"] and bot.sent_codes == [],
          f"numbers={bot.sent_numbers} codes={bot.sent_codes}")
    return client, bot


def scenario_without_link_skip_mode():
    """referral_failure_action="skip": continue using the bot's own skip button."""
    client, bot = build_client(referral_link=None, referral_failure_action="skip")
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
    check("screenshot sequence: link pasted for both prompts",
          len(bot.pasted) == 2, bot.pasted)
    check("screenshot sequence: skip button never needed",
          not any("refer code" in t for t in bot.tapped), bot.tapped)
    check("screenshot sequence: number only after both screens",
          bot.sent_numbers == ["9876543210"], bot.sent_numbers)
    check("screenshot sequence: price target respected", res["upi"] == 45.0, res)
    check("screenshot sequence: referral action recorded",
          res["referral_action"] == "pasted referral link", res["referral_action"])


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


def scenario_link_rejected_skip_mode():
    client, bot = build_client(referral_link="https://app.meesho.com/bad?via=000",
                               referral_script="reject",
                               referral_failure_action="skip")
    res = client.prepare_login("9876543212")
    check("rejected link: flow still reaches OTP screen", res["stage"] == "otp_sent", res)
    check("rejected link: skip used as fallback",
          "🚫 I don't have a refer code" in bot.tapped, bot.tapped)
    check("rejected link: number sent", bot.sent_numbers == ["9876543212"], bot.sent_numbers)


def scenario_link_silently_reasked():
    client, bot = build_client(referral_link="https://app.meesho.com/bad?via=000",
                               referral_script="silent_reask",
                               referral_failure_action="skip")
    res = client.prepare_login("9876543213")
    check("silent re-ask: flow still reaches OTP screen", res["stage"] == "otp_sent", res)
    check("silent re-ask: pasted only up to the budget",
          len(bot.pasted) == 2, bot.pasted)
    check("silent re-ask: skip used after re-ask",
          "🚫 I don't have a refer code" in bot.tapped, bot.tapped)


def scenario_unknown_referral_screen():
    """
    Unrecognisable referral copy inside the flow: with no configured link and
    no skip button there is nothing to tap, so the flow must raise a loud,
    informative error instead of cancelling silently or typing a number into
    the referral field.
    """
    client, bot = build_client(referral_link=None, referral_failure_action="skip")
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


def scenario_screen_absent_strict_mode():
    """
    Point 1: the referral screen may not appear at all. In "stop" mode (the
    default) with no link configured, a login that never asks for a referral
    must still run normally - no error, no link needed.
    """
    client, bot = build_client(referral_link=None, referral_script="absent",
                               referral_failure_action="stop")
    res = client.prepare_login("9876543210")
    check("no referral screen (strict, no link): flow reaches OTP screen",
          res["stage"] == "otp_sent", res)
    check("no referral screen: nothing pasted and no skip tapped",
          bot.pasted == [] and not any("refer code" in t for t in bot.tapped), bot.tapped)
    check("no referral screen: number sent", bot.sent_numbers == ["9876543210"],
          bot.sent_numbers)
    check("no referral screen: offer reroll still applied",
          res["rerolls"] == 1 and res["upi"] == 45.0, res)
    check("no referral screen: no referral action recorded",
          res["referral_action"] is None, res["referral_action"])


def scenario_screen_present_strict_no_link():
    """
    Point 2: the screen appears but no link is set -> stop, report, cancel the
    number with a refund tally. The flow must NOT continue without the link and
    must NOT type the number into the referral field.
    """
    client, bot = build_client(referral_link=None, referral_failure_action="stop")
    try:
        client.prepare_login("9876543210")
        check("strict, no link: raises MeeshoBotReferralError", False, "no exception")
    except MeeshoBotReferralError as exc:
        check("strict, no link: raises MeeshoBotReferralError", True)
        check("strict, no link: error explains the missing link",
              "no referral link is configured" in str(exc), str(exc))
        check("strict, no link: error tells how to fix it",
              "/referral" in str(exc) and "referral_failure_action" in str(exc), str(exc))
        check("strict, no link: buttons included",
              "Main Menu" in exc.buttons or "refer code" in str(exc.buttons), exc.buttons)
    except MeeshoBotUnknownScreen as exc:
        check("strict, no link: raises MeeshoBotReferralError", False,
              f"raised base class instead: {exc}")
    check("strict, no link: number never sent", bot.sent_numbers == [], bot.sent_numbers)
    check("strict, no link: skip button not tapped",
          not any("refer code" in t for t in bot.tapped), bot.tapped)


def scenario_link_rejected_strict():
    """Point 2: a link the bot refuses must also stop, not silently skip."""
    client, bot = build_client(referral_link="https://app.meesho.com/bad?via=000",
                               referral_script="reject", referral_failure_action="stop")
    try:
        client.prepare_login("9876543210")
        check("strict, rejected link: raises MeeshoBotReferralError", False, "no exception")
    except MeeshoBotReferralError as exc:
        check("strict, rejected link: raises MeeshoBotReferralError", True)
        check("strict, rejected link: error says the link was rejected",
              "rejected" in str(exc) or "invalid" in str(exc), str(exc))
    check("strict, rejected link: skip button NOT used",
          not any("refer code" in t for t in bot.tapped), bot.tapped)
    check("strict, rejected link: number never sent", bot.sent_numbers == [], bot.sent_numbers)
    check("strict, rejected link: exactly one paste attempt", len(bot.pasted) == 1, bot.pasted)


def scenario_change_number_no_referral_screen():
    """
    Point 3: Change Number does not show the referral screen. The recovery path
    must work in strict mode with no link configured (nothing to paste), and the
    replacement number must go out only after the number prompt.
    """
    client, bot = build_client(referral_link=None, referral_script="absent",
                               referral_failure_action="stop")
    client.prepare_login("9876543210")
    check("change number (strict, no link): first number sent",
          bot.sent_numbers == ["9876543210"], bot.sent_numbers)

    res = client.change_number(None)
    check("change number (strict, no link): still reaches the number prompt",
          res["stage"] == "prompt", res)

    res2 = client.continue_with_number("9876543211")
    check("change number (strict, no link): replacement number sent",
          bot.sent_numbers[-1] == "9876543211", bot.sent_numbers)
    check("change number (strict, no link): OTP screen reached",
          res2["stage"] == "otp_sent", res2)
    check("change number (strict, no link): no referral error raised",
          res2.get("referral_action") is None, res2.get("referral_action"))


def scenario_change_number_with_link_no_referral_screen():
    """Point 3 with a link set: recovery must not paste anything it doesn't need."""
    client, bot = build_client(referral_link="https://app.meesho.com/x?via=1",
                               referral_script="absent", referral_failure_action="stop")
    client.prepare_login("9876543210")
    pasted_during_login = len(bot.pasted)
    check("change number (link set, screen absent): nothing pasted during login",
          pasted_during_login == 0, bot.pasted)

    # Tap Change Number (the bot goes back to the number prompt), then send.
    prompt = client.change_number(None)
    check("change number (link set, screen absent): Change Number reaches the prompt",
          prompt["stage"] == "prompt", prompt)
    res = client.continue_with_number("9876543212")
    check("change number (link set, screen absent): replacement number sent",
          bot.sent_numbers[-1] == "9876543212", bot.sent_numbers)
    check("change number (link set, screen absent): OTP screen reached",
          res["stage"] == "otp_sent", res)
    check("change number (link set, screen absent): still nothing pasted",
          len(bot.pasted) == pasted_during_login, bot.pasted)


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


def scenario_reroll_button_parsing():
    s = Screen("UPI \u00b7 \u20b945", [["\U0001f504 Try Another Offer"]])
    check("reroll: 'Try Another Offer' is the reroll button",
          s.reroll_button()[2] == "\U0001f504 Try Another Offer", s.reroll_button())

    variant = Screen("Couldn't find an offer right now.",
                     [["\U0001f504 Try Again", "Main Menu", "Cancel"]])
    check("reroll: variant classifies as unknown (no price on it)",
          variant.classify() == "unknown", variant.classify())
    check("reroll: variant's 'Try Again' is the reroll button",
          variant.reroll_button()[2] == "\U0001f504 Try Again", variant.reroll_button())

    check("reroll: 'Try Again Later' is NOT a reroll button",
          Screen("x", [["Try Again Later"]]).reroll_button() is None)
    check("reroll: 'please try again' prose is NOT a reroll button",
          Screen("Please try again later.", [["Contact Support"]]).reroll_button() is None)
    check("reroll: no buttons -> no reroll button",
          Screen("Something went wrong.").reroll_button() is None)

    check("real variant: never classified as an offer",
          OFFER_RETRY.classify() == "unknown", OFFER_RETRY.classify())
    check("real variant: decoy 'UPI \u00b7 \u20b983' parses but must not be accepted",
          OFFER_RETRY.upi_price == 83.0, OFFER_RETRY.upi_price)
    check("real variant: reroll button is 'Try Again'",
          OFFER_RETRY.reroll_button()[2] == "\U0001f504 Try Again",
          OFFER_RETRY.reroll_button())
    check("real variant: 'Continue without offer' is present but is NOT the reroll",
          OFFER_RETRY.find_button("continue without") is not None
          and OFFER_RETRY.reroll_button()[2] == "\U0001f504 Try Again")
    check("real variant: not mistaken for a referral screen",
          not OFFER_RETRY.is_referral, OFFER_RETRY.is_referral)

    check("transient: 'Sending your OTP\u2026' has its own state",
          SENDING_OTP.classify() == S_SENDING_OTP, SENDING_OTP.classify())
    check("transient: 'Setting things up\u2026' stays unknown",
          SETTING_UP.classify() == "unknown", SETTING_UP.classify())
    check("transient: 'Verifying your code\u2026' has its own state",
          VERIFYING.classify() == S_VERIFYING, VERIFYING.classify())
    check("transient: 'Verifying your OTP' also matches",
          Screen("Please wait, verifying your OTP\u2026").classify() == S_VERIFYING,
          Screen("Please wait, verifying your OTP\u2026").classify())
    check("transient: verification failure copy is NOT the verifying transient",
          Screen("\u274c Verification failed. Wrong code, please try again.").classify()
          == S_WRONG_OTP,
          Screen("\u274c Verification failed. Wrong code, please try again.").classify())


def scenario_try_again_variant():
    """
    The three-button 'Try Again' variant appears mid-reroll: it must be tapped
    like 'Try Another Offer' and the flow continues to the next offer.
    """
    client, bot = build_client(referral_script="absent", retry_after=1)
    res = client.prepare_login("9876543210")
    check("variant: flow reaches OTP screen", res["stage"] == "otp_sent", res)
    check("variant: 'Try Again' was tapped", "\U0001f504 Try Again" in bot.tapped, bot.tapped)
    check("variant: 'Try Another Offer' tapped once",
          bot.tapped.count("\U0001f504 Try Another Offer") == 1, bot.tapped)
    check("variant: landed on the \u20b945 offer", res["upi"] == 45.0, res)
    check("variant: rerolls counted", res["rerolls"] == 1, res)
    check("variant: number sent once", bot.sent_numbers == ["9876543210"], bot.sent_numbers)
    check("variant: 'Continue without offer' never tapped",
          not any("Continue without" in t for t in bot.tapped), bot.tapped)
    check("variant: 'Cancel' never tapped",
          not any(t == "Cancel" or "❌ Cancel" in t for t in bot.tapped), bot.tapped)
    check("variant: decoy ₹83 price never accepted", res["upi"] != 83.0, res)


def scenario_try_again_variant_first():
    """The variant shows up right after tapping Normal, in place of the offer."""
    client, bot = build_client(referral_script="absent", variant_first=True)
    res = client.prepare_login("9876543210")
    check("variant first: flow reaches OTP screen", res["stage"] == "otp_sent", res)
    check("variant first: 'Try Again' tapped before any number",
          "\U0001f504 Try Again" in bot.tapped and bot.sent_numbers == ["9876543210"],
          f"tapped={bot.tapped} sent={bot.sent_numbers}")
    check("variant first: price target still enforced", res["upi"] == 45.0, res)
    check("variant first: 'Continue without offer' never tapped",
          not any("Continue without" in t for t in bot.tapped), bot.tapped)


def scenario_sending_otp_transient():
    """'\u23f3 Sending your OTP\u2026' between the number and 'OTP on its way' is settled."""
    client, bot = build_client(referral_script="absent", otp_transition="delayed")
    res = client.prepare_login("9876543210")
    check("sending-otp transient: flow reaches OTP screen", res["stage"] == "otp_sent", res)
    check("sending-otp transient: number sent exactly once",
          bot.sent_numbers == ["9876543210"], bot.sent_numbers)
    check("sending-otp transient: offer accepted at target", res["upi"] == 45.0, res)


def scenario_sending_otp_stuck():
    """A bot that never leaves 'Sending your OTP\u2026' must fail loudly (bounded)."""
    client, bot = build_client(referral_script="absent", otp_transition="stuck",
                               step_timeout_seconds=1)
    try:
        client.prepare_login("9876543210")
        check("sending-otp stuck: raises", False, "no exception")
    except MeeshoBotUnknownScreen as exc:
        check("sending-otp stuck: raises", True)
        check("sending-otp stuck: error names the stuck screen",
              "Sending your OTP" in str(exc), str(exc))
        check("sending-otp stuck: screen text attached",
              "Sending your OTP" in exc.screen_text, exc.screen_text)
        check("sending-otp stuck: says the number WAS submitted",
              "WAS submitted" in str(exc), str(exc))


def scenario_setup_interstitial():
    """'Setting things up\u2026' between a reroll tap and the offer is waited out."""
    client, bot = build_client(referral_script="absent", setup_interstitial=True)
    res = client.prepare_login("9876543210")
    check("setup interstitial: flow reaches OTP screen", res["stage"] == "otp_sent", res)
    check("setup interstitial: offer rerolled to target",
          res["rerolls"] == 1 and res["upi"] == 45.0, res)


def scenario_verifying_transient():
    """
    '🔎 Verifying your code...' shown after the code is submitted (before
    'Account linked!') must be settled through - reporting it as an unknown
    result made the coordinator throw away successful logins.
    """
    client, bot = build_client(referral_script="absent", code_transition="verifying")
    client.prepare_login("9876543210")
    res = client.submit_otp("111111")
    check("verifying transient: reported as linked, not unknown",
          res["status"] == "linked", res)
    check("verifying transient: user id parsed", res.get("user_id") == "123456789", res)
    check("verifying transient: account number parsed",
          res.get("account_number") == "4242", res)
    check("verifying transient: code sent exactly once",
          bot.sent_codes == ["111111"], bot.sent_codes)


def scenario_verifying_wrong_code_after_transient():
    """The transient must not swallow a wrong-code outcome either."""
    client, bot = build_client(referral_script="absent")
    client.prepare_login("9876543210")
    res = client.submit_otp("222222")
    check("verifying (wrong code): status is wrong_otp, not unknown",
          res["status"] == "wrong_otp", res)


def scenario_verifying_stuck():
    """
    A bot that never leaves 'Verifying your code\u2026' must fail loudly, stating
    that the code WAS submitted (so it can be checked/salvaged manually).
    """
    client, bot = build_client(referral_script="absent", code_transition="stuck_verify",
                               step_timeout_seconds=1)
    client.prepare_login("9876543210")
    try:
        client.submit_otp("111111")
        check("verifying stuck: raises", False, "no exception")
    except MeeshoBotUnknownScreen as exc:
        check("verifying stuck: raises", True)
        check("verifying stuck: error names the stuck screen",
              "Verifying your code" in str(exc), str(exc))
        check("verifying stuck: says the code WAS submitted",
              "WAS submitted" in str(exc), str(exc))
        check("verifying stuck: screen text attached",
              "Verifying your code" in exc.screen_text, exc.screen_text)


def scenario_flow_watchdog():
    """
    A flow whose Telegram calls hang forever must abort with MeeshoBotTimeout
    (a MeeshoBotError, so the coordinator handles it) instead of blocking the
    coordinator thread forever or crashing it with a bare TimeoutError. The
    aborted coroutine is cancelled - no leaked pending task - and the loop
    stays usable for the next flow.
    """
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
            "flow_timeout_seconds": 1.0,
        }
    }
    client = MeeshoBotClient(config, log_fn=lambda *_: None)

    class HangingClient:
        # Every screen fetch hangs: the flow can never make progress.
        async def get_messages(self, entity, limit=6):
            await asyncio.sleep(30)

    client._client = HangingClient()
    client._bot_entity = "@primesbot"
    client._loop = asyncio.new_event_loop()
    thread = threading.Thread(target=client._run_loop, daemon=True)
    thread.start()

    started = time.time()
    try:
        try:
            client.prepare_login("9876543210")
            check("watchdog: raises MeeshoBotTimeout", False, "no exception")
        except MeeshoBotTimeout as exc:
            elapsed = time.time() - started
            check("watchdog: raises MeeshoBotTimeout", True)
            check("watchdog: aborts near the no-progress budget (not step_timeout+30)",
                  elapsed < 8, f"{elapsed:.1f}s")
            check("watchdog: message names the aborted flow",
                  "prepare_login" in str(exc), str(exc))
            check("watchdog: it is a MeeshoBotError the coordinator already handles",
                  isinstance(exc, MeeshoBotError), type(exc).__name__)
        # The loop must still be alive and usable: a second flow starts (and
        # aborts) fine instead of deadlocking after the first cancellation.
        try:
            client.prepare_login("9876543211")
            check("watchdog: loop still usable after an abort", False, "no exception")
        except MeeshoBotTimeout:
            check("watchdog: loop still usable after an abort", True)
        check("watchdog: loop thread alive", thread.is_alive())
    finally:
        client.stop()
        thread.join(timeout=5)


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


# ---------------------------------------------------------------------------
# Change Number recovery: getting back to the number prompt without paying for
# a main-menu restart (Add Account -> Login with Number -> Normal -> rerolls)
# ---------------------------------------------------------------------------

def menu_pushes(bot):
    """How many times the main menu was posted (a /start reset adds one)."""
    return sum(1 for m in bot.messages if m.text == MAIN_MENU.text)


def scenario_number_prompt_recognition():
    """
    The recognition behind the recovery: a regular offer, a revision copy
    classify() reports as "unknown", and the screens that must never be typed
    into.
    """
    check("prompt: a regular offer is the number prompt",
          OFFER_45.classify() == "offer" and OFFER_45.looks_like_number_prompt(),
          OFFER_45.classify())
    check("prompt: revision copy classify() does not know is still the prompt",
          PROMPT_ALT_COPY.classify() == "unknown"
          and PROMPT_ALT_COPY.looks_like_number_prompt(),
          PROMPT_ALT_COPY.classify())
    check("prompt: revision copy also passes the strict (offer-marker) read",
          PROMPT_ALT_COPY.looks_like_number_prompt(require_offer_marker=True))
    check("prompt: a configured hint recognizes an otherwise unknown copy",
          not PROMPT_LOCAL_COPY.looks_like_number_prompt()
          and PROMPT_LOCAL_COPY.looks_like_number_prompt(extra_hints=("daalein",)),
          PROMPT_LOCAL_COPY.classify())
    check("prompt: the 'Failed to fetch offer' variant is NOT a prompt",
          not OFFER_RETRY.looks_like_number_prompt()
          and not OFFER_RETRY.looks_like_number_prompt(require_offer_marker=True),
          OFFER_RETRY.classify())
    check("prompt: the bot checker's prompt is NOT a prompt on a cold read",
          not CHECK_PROMPT_ONLY.looks_like_number_prompt(require_offer_marker=True))
    check("prompt: OTP / linked / menu / blocked / transient screens are not prompts",
          not any(screen.looks_like_number_prompt() for screen in
                  (OTP_WAIT, LINKED, MAIN_MENU, BLOCKED, SENDING_OTP,
                   VERIFYING, SETTING_UP, LINK_CHOICE, LOGIN_MODE)))
    check("prompt: the referral screens are not number prompts",
          not REFERRAL_ASK.looks_like_number_prompt()
          and not REFERRAL_SET.looks_like_number_prompt())


def scenario_change_number_unrecognised_prompt_copy():
    """
    The reported failure: the bot answers Change Number with a number prompt
    whose copy classify() does not know ("got unknown"). It must be accepted as
    the prompt - no main-menu restart, no offer reroll - and the replacement
    number must go out from there.
    """
    client, bot = build_client(referral_link=None, referral_script="absent",
                               change_number_screen=PROMPT_ALT_COPY)
    client.prepare_login("9876543210")
    menus_before = menu_pushes(bot)
    taps_before = list(bot.tapped)

    res = client.change_number(None)
    check("alt copy: Change Number reaches the prompt",
          res["stage"] == "prompt", res)
    check("alt copy: one tap was enough", bot.change_taps == 1, bot.change_taps)
    check("alt copy: bot NOT reset to the main menu",
          menu_pushes(bot) == menus_before, [m.text[:24] for m in bot.messages])
    check("alt copy: only the Change Number tap was added",
          bot.tapped == taps_before + ["\U0001f504 Change Number"], bot.tapped)

    rerolls_before = bot.reroll_count
    res2 = client.continue_with_number("9876543231")
    check("alt copy: replacement number sent from the prompt",
          bot.sent_numbers[-1] == "9876543231", bot.sent_numbers)
    check("alt copy: OTP screen reached", res2["stage"] == "otp_sent", res2)
    check("alt copy: no offer reroll for the replacement",
          res2.get("rerolls", 0) == 0 and bot.reroll_count == rerolls_before,
          f"res={res2} rerolls={bot.reroll_count} (was {rerolls_before})")
    check("alt copy: prompt reuse flagged on the change-number path",
          res2.get("upi") == 45.0, res2)


def scenario_change_number_tap_ignored_then_retried():
    """A Change Number tap the bot ignores is retried, not abandoned."""
    client, bot = build_client(referral_link=None, referral_script="absent",
                               ignore_change_taps=1, change_number_retries=2)
    client.prepare_login("9876543210")
    menus_before = menu_pushes(bot)

    res = client.change_number(None)
    check("ignored tap: retried and reached the prompt",
          res["stage"] == "prompt", res)
    check("ignored tap: the bot ignored the first tap",
          bot.ignored_change_taps == 1, bot.ignored_change_taps)
    check("ignored tap: exactly two Change Number taps",
          bot.change_taps == 2, bot.change_taps)
    check("ignored tap: no main-menu reset",
          menu_pushes(bot) == menus_before, menu_pushes(bot))

    res2 = client.continue_with_number("9876543232")
    check("ignored tap: replacement number sent",
          bot.sent_numbers[-1] == "9876543232", bot.sent_numbers)
    check("ignored tap: OTP screen reached", res2["stage"] == "otp_sent", res2)


def scenario_change_number_bot_left_the_flow():
    """
    The bot dropped out of the login flow by itself (its OTP prompt expired, a
    manual /start): report needs_full_flow instead of raising - it is already
    where a full flow starts, so nothing has to be reset.
    """
    client, bot = build_client(referral_link=None, referral_script="absent")
    client.prepare_login("9876543210")
    sent_before = list(bot.sent_numbers)
    menus_before = menu_pushes(bot)

    bot.state = "menu"
    bot._push(MAIN_MENU)
    res = client.change_number(None)
    check("left flow: reports needs_full_flow",
          res["stage"] == "needs_full_flow", res)
    check("left flow: nothing typed into the menu",
          bot.sent_numbers == sent_before, bot.sent_numbers)
    check("left flow: no /start reset on top of it",
          menu_pushes(bot) == menus_before + 1, menu_pushes(bot))
    check("left flow: no Change Number tap on the menu",
          bot.change_taps == 0, bot.change_taps)


def scenario_change_number_gives_up_bounded():
    """
    A dead-end screen: the recovery gives up within its own short budget and
    raises (so the coordinator decides what to do next) instead of burning the
    full step timeout or silently resetting the bot to the menu.
    """
    client, bot = build_client(referral_link=None, referral_script="absent",
                               change_number_screen=BROKEN_SCREEN,
                               change_number_retries=1,
                               change_number_timeout_seconds=1,
                               change_number_budget_seconds=6,
                               step_timeout_seconds=30)
    client.prepare_login("9876543210")
    menus_before = menu_pushes(bot)

    started = time.time()
    try:
        client.change_number(None)
        check("dead end: raises MeeshoBotUnknownScreen", False, "no exception")
    except MeeshoBotUnknownScreen as exc:
        check("dead end: raises MeeshoBotUnknownScreen", True)
        check("dead end: error says how to teach the prompt copy",
              "number_prompt_hints" in str(exc), str(exc))
        check("dead end: error carries the screen text",
              "Something went wrong" in (exc.screen_text or ""), exc.screen_text)
    took = time.time() - started
    check("dead end: bot NOT reset to the main menu inside change_number",
          menu_pushes(bot) == menus_before, menu_pushes(bot))
    check("dead end: gave up quickly, not after the 30s step timeout",
          took < 10, f"{took:.1f}s")


def scenario_change_number_config_hint():
    """meesho_bot.number_prompt_hints teaches an otherwise unknown copy."""
    client, bot = build_client(referral_link=None, referral_script="absent",
                               change_number_screen=PROMPT_LOCAL_COPY,
                               number_prompt_hints=["daalein"])
    client.prepare_login("9876543210")
    res = client.change_number(None)
    check("config hint: prompt recognized", res["stage"] == "prompt", res)
    res2 = client.continue_with_number("9876543250")
    check("config hint: replacement number sent",
          bot.sent_numbers[-1] == "9876543250", bot.sent_numbers)
    check("config hint: OTP screen reached", res2["stage"] == "otp_sent", res2)

    # Without the hint the same copy is not recognizable: the recovery gives up
    # (and the coordinator decides), it does not guess.
    client2, bot2 = build_client(referral_link=None, referral_script="absent",
                                 change_number_screen=PROMPT_LOCAL_COPY,
                                 change_number_retries=0,
                                 change_number_timeout_seconds=1,
                                 change_number_budget_seconds=4)
    client2.prepare_login("9876543210")
    try:
        client2.change_number(None)
        check("config hint: without it the copy stays unknown", False, "no exception")
    except MeeshoBotUnknownScreen:
        check("config hint: without it the copy stays unknown", True)
    check("config hint: nothing typed without the hint",
          bot2.sent_numbers == ["9876543210"], bot2.sent_numbers)


def scenario_at_number_prompt_probe():
    """at_number_prompt() is a read-only probe the coordinator can trust."""
    client, bot = build_client(referral_link=None, referral_script="absent")
    client.prepare_login("9876543210")
    check("probe: the OTP screen is not the number prompt",
          client.at_number_prompt() is False, client.screen_state())
    client.change_number(None)
    check("probe: the offer screen is the number prompt",
          client.at_number_prompt() is True, client.screen_state())
    bot._push(CHECK_PROMPT_ONLY)
    check("probe: the bot checker's prompt is not the login prompt",
          client.at_number_prompt() is False)
    check("probe: the probe typed nothing",
          bot.sent_numbers == ["9876543210"] and bot.sent_codes == [],
          f"numbers={bot.sent_numbers} codes={bot.sent_codes}")


def scenario_prepare_login_reuses_prompt():
    """
    The bot is already sitting on a good offer/number prompt (a Change Number
    that reported "unknown" but worked, or a flow the coordinator deliberately
    kept in place because the checker API answers the number checks): a fresh
    login sends its number from there - no main-menu walk, no offer reroll.
    """
    client, bot = build_client(referral_link=None, referral_script="absent")
    bot.state = "offer"
    bot.offer_prices = [45]
    bot._push(bot._offer_screen())
    menus_before = menu_pushes(bot)

    res = client.prepare_login("9876543240")
    check("reuse: number sent", bot.sent_numbers == ["9876543240"], bot.sent_numbers)
    check("reuse: OTP screen reached", res["stage"] == "otp_sent", res)
    check("reuse: flagged as a reused prompt", res.get("reused_prompt") is True, res)
    check("reuse: no menu walk (not a single tap)", bot.tapped == [], bot.tapped)
    check("reuse: no offer reroll",
          res["rerolls"] == 0 and bot.reroll_count == 0, res)
    check("reuse: no /start reset", menu_pushes(bot) == menus_before, menu_pushes(bot))
    check("reuse: UPI price kept", res["upi"] == 45.0, res)


def scenario_prepare_login_rerolls_over_target_prompt_in_place():
    """An over-target prompt is rerolled where the bot already sits."""
    client, bot = build_client(referral_link=None, referral_script="absent")
    bot.state = "offer"
    bot.offer_prices = [60, 45]
    bot._push(bot._offer_screen())  # ₹60 on screen, above the ₹47 target

    res = client.prepare_login("9876543241")
    check("reroll in place: number sent",
          bot.sent_numbers == ["9876543241"], bot.sent_numbers)
    check("reroll in place: prompt reused", res.get("reused_prompt") is True, res)
    check("reroll in place: only the reroll was tapped",
          bot.tapped == ["\U0001f504 Try Another Offer"], bot.tapped)
    check("reroll in place: price brought down to the target",
          res["rerolls"] == 1 and res["upi"] == 45.0, res)


def scenario_prepare_login_does_not_reuse_checker_prompt():
    """
    A cold read must not mistake the bot CHECKER's "send the number" prompt for
    the login prompt - the paid number would vanish into the checker.
    """
    client, bot = build_client(referral_link=None, referral_script="absent")
    bot.state = "check_prompt"
    bot._push(CHECK_PROMPT_ONLY)

    res = client.prepare_login("9876543242")
    check("checker prompt: NOT reused as a login prompt",
          res.get("reused_prompt") is not True, res)
    check("checker prompt: flow walked back to the menu first",
          bool(bot.tapped) and "Main Menu" in bot.tapped[0], bot.tapped)
    check("checker prompt: number only sent after the full walk",
          res["stage"] == "otp_sent" and bot.sent_numbers == ["9876543242"],
          f"res={res} sent={bot.sent_numbers}")


def scenario_reuse_disabled_by_config():
    """reuse_number_prompt=false restores the always-restart behaviour."""
    client, bot = build_client(referral_link=None, referral_script="absent",
                               reuse_number_prompt=False)
    bot.state = "offer"
    bot.offer_prices = [45, 45]
    bot._push(bot._offer_screen())

    res = client.prepare_login("9876543243")
    check("reuse disabled: prompt not reused", res.get("reused_prompt") is not True, res)
    check("reuse disabled: full menu walk happened",
          any("Add Account" in tap for tap in bot.tapped), bot.tapped)
    check("reuse disabled: number still sent",
          bot.sent_numbers == ["9876543243"] and res["stage"] == "otp_sent",
          f"res={res} sent={bot.sent_numbers}")


def main():
    print("=== PRIMES referral-flow replay ===\n")
    scenario_reroll_button_parsing()
    scenario_screen_parsing()
    scenario_try_again_variant()
    scenario_try_again_variant_first()
    scenario_setup_interstitial()
    scenario_verifying_transient()
    scenario_verifying_wrong_code_after_transient()
    scenario_verifying_stuck()
    scenario_flow_watchdog()
    scenario_sending_otp_transient()
    scenario_sending_otp_stuck()
    scenario_with_link()
    scenario_two_screens_like_screenshot()
    scenario_acknowledgement_screen()
    scenario_without_link_skip_mode()
    scenario_link_rejected_skip_mode()
    scenario_link_silently_reasked()
    scenario_unknown_referral_screen()
    scenario_screen_absent_strict_mode()
    scenario_screen_present_strict_no_link()
    scenario_link_rejected_strict()
    scenario_change_number_no_referral_screen()
    scenario_change_number_with_link_no_referral_screen()
    scenario_menu_refer_earn_not_mistaken()
    scenario_referral_mid_otp_wait()
    scenario_change_number_with_referral()
    scenario_number_prompt_recognition()
    scenario_change_number_unrecognised_prompt_copy()
    scenario_change_number_tap_ignored_then_retried()
    scenario_change_number_bot_left_the_flow()
    scenario_change_number_gives_up_bounded()
    scenario_change_number_config_hint()
    scenario_at_number_prompt_probe()
    scenario_prepare_login_reuses_prompt()
    scenario_prepare_login_rerolls_over_target_prompt_in_place()
    scenario_prepare_login_does_not_reuse_checker_prompt()
    scenario_reuse_disabled_by_config()

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
