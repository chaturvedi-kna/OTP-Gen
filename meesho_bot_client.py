"""
Telegram USER-account automation ("userbot") for the PRIMES Meesho concierge bot.

Why a userbot: normal Telegram bots (Bot API) cannot tap another bot's inline
buttons or even see its traffic. Logged in as a regular Telegram USER account
via Telethon (MTProto), inline buttons are first-class API objects: "clicking"
is a direct callback call - there is no screen, no coordinates and no UI
automation involved.

Flow automated (from the recorded screens + screenshot):
  /start -> [Add Account] -> [Login with Number]
         -> "🔗 Set Refer Link" / "🎁 Referral link?" screen: paste
            meesho_bot.referral_link when one is configured (once per login),
            otherwise tap the bot's own "🚫 I don't have a refer code" /
            "Don't have one" option - never Cancel, never a silent stop
         -> [Normal] -> offer screen: reroll [Try Another Offer] until UPI <=
            target price (some bot revisions show a three-button "Try Again"
            variant instead of an offer - tapped the same way)
         -> send 10-digit number -> brief "⏳ Sending your OTP…" screen ->
            "OTP on its way" screen
         -> (coordinator polls the OTP provider) -> send OTP code
         -> brief "🔎 Verifying your code…" screen -> "Account linked!"
            (parse User ID / account #)
Recovery:
  OTP missing/wrong/expired/blocked -> [Change Number] -> send the next number
  (the bot keeps the current offer screen), instead of redoing the whole menu.

Number checker (checker.mode = "bot" / "auto"):
  the same userbot can ask the bot whether a number is already registered on
  Meesho, instead of / in addition to the HTTP checker API: main menu ->
  [checker button] (or checker.bot.command) -> send the 10-digit number ->
  read the "registered" / "not registered" verdict -> back to the main menu.
  Used as a fallback when the API is down / too slow / rejects every key (see
  checker_router.py). All screen wording is configurable under
  "checker" -> "bot", because bot copy varies between revisions.

Timeouts: every step (screen poll, tap, settle) has its own budget
(step_timeout_seconds), and _run() additionally aborts a whole flow with a
MeeshoBotTimeout when it stops making progress for flow_timeout_seconds
(default auto) - a hung Telegram call. Flows that keep making progress (e.g.
long offer-reroll sessions) are never killed by a blanket wall-clock cap
anymore; the aborted coroutine is cancelled on its loop so no task leaks.

One-time setup: run `python login_userbot.py` to create the StringSession file.

This module degrades gracefully: if telethon is missing, the session is
missing, or config is incomplete, .ready is False and the coordinator falls
back to the existing manual Telegram trigger flow.
"""

import asyncio
import concurrent.futures
import random
import re
import threading
import time
import unicodedata


class MeeshoBotError(Exception):
    pass


class MeeshoBotNotConfigured(MeeshoBotError):
    pass


class MeeshoBotUnknownScreen(MeeshoBotError):
    def __init__(self, message, screen_text="", buttons=None):
        super().__init__(message)
        self.screen_text = screen_text
        # Flattened button labels of the offending screen, so alerts can show
        # the exact options the bot offered (e.g. "Yes / No / Skip").
        self.buttons = buttons or []


class MeeshoBotReferralError(MeeshoBotUnknownScreen):
    """
    The referral step could not be completed the way it is configured.

    With meesho_bot.referral_failure_action = "stop" (default) this is raised
    instead of falling back to the bot's own skip button: the coordinator stops
    the automation, reports why, and cancels the number with a refund tally -
    the login is never continued without the referral link.
    """


class MeeshoBotTimeout(MeeshoBotError):
    """
    A bot flow was aborted because the Telegram side stopped responding.

    Raised by _run() when a coroutine exceeds a hard deadline (stop()/diagnostics)
    or stops making progress for flow_timeout_seconds (a hung Telethon call).
    This is a MeeshoBotError, so the coordinator's existing handlers turn it
    into an alert + number cancellation instead of an unhandled crash, and the
    aborted coroutine is cancelled on its loop so no task is left pending.
    """


def _is_timeout_error(exc):
    """
    True for every "this call ran out of time" exception Python can raise here.

    `concurrent.futures.Future.result(timeout=...)` raises
    `concurrent.futures.TimeoutError`, and `asyncio` used to raise the same
    class - but NEITHER is the builtin `TimeoutError` before Python 3.11: the
    three were only unified in 3.11 ("Changed in version 3.11: This class was
    made an alias of TimeoutError", concurrent.futures / asyncio docs). So on
    the Python 3.8-3.10 anaconda ships, a bare `except TimeoutError:` does NOT
    catch what `future.result(timeout=...)` raises.

    That is not a cosmetic detail: _run() polls the future in short slices and
    treats the slice timeout as "keep waiting, the flow is still working". With
    a bare `except TimeoutError` the very first slice escapes _run() as a
    message-less TimeoutError - so every PRIMES flow was reported as failed
    after ~2s, the still-running coroutine kept tapping the same Telegram chat
    while the next flow started, and the coordinator logged a bare
    "Offer pre-warm failed: " / "Unexpected error ... Error: " with no reason.

    The classes are read at call time (not frozen into a module constant) so
    the pre-3.11 split stays testable on a 3.11+ interpreter.
    """
    for cls in (TimeoutError, concurrent.futures.TimeoutError,
                getattr(asyncio, "TimeoutError", None)):
        if cls is not None and isinstance(exc, cls):
            return True
    return False


# ---------------------------------------------------------------------------
# Screen model / parsing (pure - no Telethon dependency, fully unit-testable)
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # symbols, emoji, transport, supplemental
    "\U00002600-\U000027BF"   # misc symbols / dingbats
    "\U00002190-\U000021FF"   # arrows
    "\U000025A0-\U000025FF"   # geometric shapes (▶ ◀ ● ■ ...)
    "\U00002B00-\U00002BFF"   # misc symbols and arrows
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "️‍⃠"                        # variation selector-16, ZWJ, no-entry overlay
    "]+",
    flags=re.UNICODE,
)


def normalize_label(text):
    """Lowercase, strip emoji/selectors and collapse whitespace."""
    if text is None:
        return ""
    text = str(text)
    text = unicodedata.normalize("NFKC", text)
    text = _EMOJI_RE.sub("", text)
    text = text.replace("️", "")
    return re.sub(r"\s+", " ", text).strip().lower()


# Screen state identifiers
S_MENU = "main_menu"
S_LINK_CHOICE = "link_choice"
S_LOGIN_MODE = "login_mode"
S_REFERRAL = "referral"
S_OFFER = "offer"
S_OTP_WAIT = "otp_wait"
# The brief "⏳ Sending your OTP…" screen the bot shows between the number
# being sent and the "OTP on its way" screen: transient, keep waiting.
S_SENDING_OTP = "otp_sending"
# The brief "🔎 Verifying your code…" screen the bot shows right after the OTP
# code is submitted, before "Account linked!" (or a wrong-code error) appears:
# transient, keep waiting - never report it as the flow's outcome.
S_VERIFYING = "verifying"
# Transient "the bot is preparing the next screen" copy ("⏳ Setting things
# up…") the bot shows between a tap (Try Another Offer / Try Again / Change
# Number / Normal) and the offer that follows it. It normally lasts a second or
# two, but can outlive a whole step timeout when the bot throttles after many
# rerolls - it must be waited through, never read as a dead end.
S_WORKING = "working"
S_LINKED = "linked"
S_WRONG_OTP = "wrong_otp"
S_EXPIRED = "otp_expired"
S_BLOCKED = "blocked"
S_UNKNOWN = "unknown"

# Number-checker screens (the bot's own "is this number registered?" service,
# used as a fallback when the checker API is down / too slow - see
# checker_router.py).
S_CHECK_PROMPT = "check_prompt"      # the bot asks for the 10-digit number
S_CHECKING = "checking"              # transient "checking..." screen
S_CHECK_RESULT = "check_result"      # "registered" / "not registered"

# -- default screen hints for the bot checker --------------------------------
#
# All of these can be overridden/extended from
# config.json -> "checker" -> "bot" (see SETUP_CHECKER.md), because bot copy
# varies between revisions. Matching is done on normalized labels (lowercase,
# no emoji), so hints here stay plain text.

# Buttons on the main menu that open the checker.
DEFAULT_CHECK_BUTTON_HINTS = (
    "check number", "number check", "check registration", "registration check",
    "check account", "account check", "check status", "verify number",
    "number status", "check meesho", "check user", "check number status",
)
# A last-resort generic pass: any button with one of these words, as long as
# it does not contain one of the excluded words below.
DEFAULT_CHECK_BUTTON_FALLBACK_HINTS = ("check", "verify", "validate", "status")
DEFAULT_CHECK_BUTTON_EXCLUDE = (
    "balance", "price", "offer", "shop", "wallet", "upi", "order", "payment",
    "support", "help", "referr", "invite", "menu", "cancel", "back", "otp",
)

# Buttons that open the next check directly from a result screen (e.g.
# "Check Another Number" / "Check Another"). Used for continuous checking
# so a dedicated checker bot can receive the next number without tapping
# Start / Main Menu again.
DEFAULT_CHECK_ANOTHER_HINTS = (
    "check another", "check again", "another number", "another check",
    "check another number", "next number", "new check",
)

# Text on the screen that asks for the number to check.
DEFAULT_CHECK_PROMPT_HINTS = (
    "send the number", "send your number", "send number", "send me the number",
    "send me a number", "enter the number", "enter number", "enter your number",
    "enter a number", "type the number", "type number", "paste the number",
    "paste number", "provide the number", "share the number", "give me the number",
    "which number", "number to check", "check which number", "10-digit",
    "10 digit", "10digit",
)
_CHECK_NUMBER_WORDS = ("number", "mobile", "contact", "phone")
_CHECK_ASK_WORDS = ("send", "enter", "type", "paste", "provide", "share",
                    "give", "which", "what", "check")

# Result wording. Checked BEFORE the positive hints, because "not registered"
# contains "registered".
DEFAULT_CHECK_NOT_REGISTERED_HINTS = (
    "not registered", "isn't registered", "isnt registered", "is not registered",
    "no longer registered", "unregistered", "not linked", "isn't linked",
    "not associated", "no account", "doesn't have", "does not have",
    "don't have", "not found", "no user found", "no record", "not in our",
    "not on meesho", "never registered", "no meesho account",
    "not a meesho user", "not available", "available for registration",
    "available on meesho", "fresh number", "new number", "no existing account",
)
DEFAULT_CHECK_REGISTERED_HINTS = (
    "already registered", "is registered", "registered on meesho",
    "registered with meesho", "registration found", "already exists",
    "number exists", "account exists", "user exists", "existing account",
    "existing user", "already has an account", "has an account",
    "has a meesho account", "already linked", "linked to an account",
    "already associated", "found on meesho", "old account", "already in use",
    "in use",
)
# Emoji confirmation next to a "registered"/"registration" word.
_CHECK_YES_EMOJI = ("✅", "✔", "☑")
_CHECK_NO_EMOJI = ("❌", "✖", "🚫", "⛔", "❎")

# Words that mean "the bot is working on the check right now, keep waiting".
_CHECKING_HINTS = (
    "checking", "please wait", "just a moment", "one moment", "processing",
    "searching", "looking up", "fetching", "wait a", "hold on",
)

# -- login number-prompt wording ---------------------------------------------
#
# classify() only recognises the offer/number prompt through the "Try Another
# Offer" button or "10-digit mobile" + "continue" copy. Bot revisions whose
# prompt is worded differently (no reroll button, "Enter the mobile number you
# want to link", a translated copy, ...) land on S_UNKNOWN - which used to make
# the Change Number recovery give up, drop the bot back to the main menu and
# re-roll the offer from scratch (slow, and it burns the paid number's OTP
# window). These hints let that screen be recognised as the prompt it is; they
# can be extended per bot revision with meesho_bot.number_prompt_hints.
NUMBER_PROMPT_HINTS = (
    "10-digit mobile", "10 digit mobile", "10digit mobile",
    "10-digit number", "10 digit number", "10digit number",
    "mobile number", "phone number", "enter your number", "send your number",
    "type your number", "paste your number", "enter the number",
    "send the number", "number to link", "number you want to link",
    "number you wish to link", "link your number", "new number",
    "another number", "different number",
)
_NUMBER_ASK_WORDS = ("enter", "send", "type", "paste", "provide", "share",
                     "give", "input", "submit", "your", "new", "another")
# The "⚠️ Failed to fetch offer" three-button variant (🔄 Try Again /
# ➡️ Continue without offer / ❌ Cancel). It is a reroll screen, NOT a number
# prompt, even though it carries price lines.
_OFFER_VARIANT_HINTS = (
    "failed to fetch offer", "couldn't load", "could not load", "unable to load",
    "offer · null", "offer null", "continue without offer", "try again to retry",
)
# The bot's "I am fetching the next offer right now" copy, shown as an edit
# between a reroll tap and the offer screen.
WORKING_HINTS = (
    "setting things up", "setting up", "setting-up", "setup in progress",
    "preparing", "loading", "working on it", "working on your",
    "hold on", "please wait", "just a moment", "one moment", "wait a moment",
    "wait a sec", "fetching your offer", "getting your offer",
    "finding the best offer", "looking for an offer", "checking offers",
    "grabbing your offer",
)
# Wording that belongs to the bot CHECKER, not to the login flow. A cold read
# (no tap context) must never type a paid number into the checker's "send the
# number" prompt, and that prompt is worded almost exactly like the login
# prompt - the check/verify/registration vocabulary is what tells them apart.
_CHECKER_WORD_HINTS = ("check", "verify", "registered", "registration")

# A short hint (a single short word) is matched on word boundaries so e.g.
# "no" can never match inside "notification".
_HINT_WORD_RE = re.compile(r"[a-z0-9]+")


def _hint_matches(text, hint):
    """True when `hint` appears in the normalized `text`."""
    hint = normalize_label(hint)
    if not hint:
        return False
    if " " in hint or len(hint) > 4 or not hint.isalnum():
        return hint in text
    return re.search(rf"\b{re.escape(hint)}\b", text) is not None


def _hint_match(text, hints):
    for hint in hints:
        if _hint_matches(text, hint):
            return hint
    return None

# The promotional screen the bot inserts between "Login with Number" and the
# login-mode ("Normal" / "Auto") choice. Two copies have been observed:
#
#   "🔗 Set Refer Link - You haven't saved a referral link yet. Paste your
#    Meesho referral link once and I'll use it automatically every time you add
#    an account ... e.g. https://app.meesho.com/...?via=...   [🏠 Main Menu]"
#
#   "🎁 Referral link? Paste your Meesho referral link (e.g. ...?via=...)
#    Don't have one? Tap below."   [🚫 I don't have a refer code] [❌ Cancel]
#
# It is matched on a referral/invite word plus a link/code/paste word, so it
# stays robust to the copy changing between bot revisions while not turning
# every screen that happens to mention "link" into a referral screen.
REFERRAL_HINT_WORDS = ("referral", "refer link", "refer and earn", "refer & earn",
                       "invite link", "invite code", "referred by", "refer code")
REFERRAL_ACTION_WORDS = ("link", "code", "paste", "url", "http")


REFERRAL_PASTE_HINTS = ("paste here", "paste your", "paste link", "type here",
                        "enter link", "enter referral")
REFERRAL_SKIP_HINTS = (
    "don't have", "dont have", "do not have", "no referral", "without referral",
    "skip", "not now", "later", "no thanks", "no thank you", "continue without",
    "proceed without",
)
REFERRAL_ERROR_HINTS = (
    "invalid", "not valid", "doesn't look like", "does not look like",
    "expired", "already used", "couldn't", "could not", "wrong link",
    "wrong code", "try again",
)

# A dedicated "Try Again" button (some bot revisions show a three-button
# offer variant with it instead of "Try Another Offer"). Matched on the whole
# normalised label, so prose like "please try again" or an option such as
# "Try Again Later" never counts.
_REROLL_TRY_AGAIN_RE = re.compile(r"^try again[\s!?.]*$")
# "✅ Referral link saved!" style confirmations. They mention a referral link
# but ask for nothing, so they must not be answered again. The negations keep
# the actual prompt ("You haven't saved a referral link yet.") out.
REFERRAL_ACK_HINTS = ("saved", "added", "applied", "recorded", "updated",
                      "set successfully", "success")
REFERRAL_ACK_NEGATIONS = ("haven't", "have not", "hasn't", "has not", "not saved",
                          "not added", "don't have", "dont have", "no referral", "yet")
REFERRAL_ACK_BUTTONS = ("continue", "next", "ok", "done", "got it", "proceed",
                        "start login", "add account")



class Screen:
    def __init__(self, text="", buttons=None):
        self.text = text or ""
        # list of rows; each row is a list of button labels
        self.buttons = buttons or []

    @classmethod
    def from_telethon(cls, message):
        buttons = []
        if getattr(message, "buttons", None):
            for row in message.buttons:
                labels = []
                for btn in row:
                    labels.append(getattr(btn, "text", None) or "")
                buttons.append(labels)
        return cls(text=getattr(message, "text", "") or "", buttons=buttons)

    @classmethod
    def from_text(cls, text, buttons=None):
        return cls(text=text, buttons=buttons)

    # -- buttons -----------------------------------------------------------

    @property
    def button_labels(self):
        """Every button label on the screen, flattened row by row."""
        return [label for row in self.buttons for label in row]

    @property
    def button_summary(self):
        """Compact one-line rendering of the available buttons."""
        labels = [l for l in self.button_labels if l]
        return " / ".join(labels[:8]) if labels else "(no buttons)"


    def find_button(self, *needles):
        """
        Return (row, col, label) for the first button whose normalized label
        contains any of the needles, or None.
        """
        norm_needles = [normalize_label(n) for n in needles]
        for r, row in enumerate(self.buttons):
            for c, label in enumerate(row):
                norm = normalize_label(label)
                if any(n in norm for n in norm_needles):
                    return r, c, label
        return None

    def has_button(self, *needles):
        return self.find_button(*needles) is not None

    def reroll_button(self):
        """
        (row, col, label) of the button that rerolls the offer.

        Normally "Try Another Offer". Some bot revisions instead show a
        three-button variant with no price on the screen whose reroll button
        is "Try Again" - it must be tapped exactly the same way. Only a
        dedicated "Try Again" button label matches, so other copy is ignored.
        """
        hit = self.find_button("try another offer")
        if hit:
            return hit
        for r, row in enumerate(self.buttons):
            for c, label in enumerate(row):
                if _REROLL_TRY_AGAIN_RE.match(normalize_label(label)):
                    return r, c, label
        return None

    # -- login number prompt -----------------------------------------------

    def looks_like_number_prompt(self, extra_hints=(), require_offer_marker=False):
        """
        True when this screen is the LOGIN number prompt - the offer screen the
        bot shows before it sends the OTP - including revisions whose copy or
        buttons classify() does not recognise as S_OFFER (the real one e.g. is
        "✏️ Change Number - Send the 10-digit mobile number you'd like to use
        instead." with a lone Cancel button).

        `require_offer_marker` is the COLD-READ mode (no tap context: the
        coordinator asking "may I send a number here?", or a login deciding to
        reuse the screen the bot sits on). The one screen it must never accept
        is the bot CHECKER's "send the number to check" prompt - worded almost
        exactly like the login prompt - so checker vocabulary
        (check/verify/registered) rules it out there. Configured
        `extra_hints` always win, in both modes.

        Known screens (menu, login mode, referral, OTP-wait, linked, blocked,
        the transient "sending/verifying/preparing" ones) are never a number
        prompt, and neither is the "⚠️ Failed to fetch offer / 🔄 Try Again"
        variant: it carries a decoy price and must be rerolled, not typed into.
        """
        state = self.classify()
        if state == S_OFFER:
            return True
        if state != S_UNKNOWN:
            return False
        text = normalize_label(self.text)
        if self.looks_like_checking():
            return False
        if self.check_verdict() is not None:
            return False
        # The offer-fetch variant asks for a tap, not for a number: it carries
        # a decoy price ("Offer · Null" next to "UPI · ₹83") and must be
        # rerolled, never typed into.
        if self.reroll_button() is not None and any(
                hint in text for hint in _OFFER_VARIANT_HINTS):
            return False
        # Explicit per-revision hints win in both modes.
        if extra_hints and _hint_match(text, tuple(extra_hints)):
            return True
        # Cold read: the checker's prompt is worded like the login prompt; its
        # check/verify/registration vocabulary is what tells them apart.
        if require_offer_marker and _hint_match(text, _CHECKER_WORD_HINTS):
            return False
        if _hint_match(text, NUMBER_PROMPT_HINTS):
            return True
        # Unlisted copy: an ask-for-a-number sentence on a screen that carries
        # an offer marker ("Enter the mobile no. to continue with this offer").
        marker = (
            self.reroll_button() is not None
            or self.upi_price is not None
            or "offer" in text
            or self.has_button("continue", "change number", "submit", "next")
        )
        if require_offer_marker and not marker:
            return False
        return bool(
            marker
            and any(word in text for word in ("number", "mobile", "phone"))
            and any(word in text for word in _NUMBER_ASK_WORDS)
        )

    # -- referral screen ---------------------------------------------------

    @property
    def is_referral(self):
        """
        True for the "🔗 Set Refer Link" / "🎁 Referral link?" screen the bot
        shows between "Login with Number" and the NORMAL/AUTO login mode (it may
        also not appear at all on a given login).

        Matching looks for a referral/invite word plus a link/code/paste word,
        which survives copy changes between bot revisions. Menu, login-mode and
        offer screens are excluded up front (they carry their own buttons), so
        a "Refer & Earn" entry in the main menu can never be mistaken for the
        prompt and cost us a number.
        """
        if self.has_button("try another offer", "add account", "open shop",
                           "login with numb", "choose login mode",
                           "try another number"):
            return False
        # "✅ Referral link saved! Now choose how you want to log in." is the
        # login-mode screen, not a referral prompt.
        if self.has_button("normal") and self.has_button("auto"):
            return False

        text = normalize_label(self.text)
        if any(word in text for word in REFERRAL_HINT_WORDS):
            return any(word in text for word in REFERRAL_ACTION_WORDS)

        # Body text missing/unhelpful: fall back to the buttons themselves.
        labels = normalize_label(" ".join(self.button_labels))
        return any(word in labels for word in ("referral", "refer a", "invite"))

    @property
    def referral_prompt(self):
        """True when the screen is asking for the link/code itself."""
        if not self.is_referral:
            return False
        text = normalize_label(self.text)
        if any(hint in text for hint in REFERRAL_PASTE_HINTS):
            return True
        # Buttons only, or unfamiliar copy: with one option to move forward,
        # the referral screen is a prompt whatever its wording.
        return self.referral_skip_button() is not None and self.referral_yes_button() is None

    @property
    def referral_rejected(self):
        """True when a referral attempt was visibly refused (bad/expired link)."""
        if not self.is_referral:
            return False
        text = normalize_label(self.text)
        return any(hint in text for hint in REFERRAL_ERROR_HINTS)

    @property
    def referral_acknowledged(self):
        """
        True for a confirmation such as "✅ Referral link saved!" - it mentions
        a referral link but asks for nothing, so tapping a skip option on it
        would be wrong (it would look like refusing the saved link).
        """
        if not self.is_referral:
            return False
        text = normalize_label(self.text)
        if any(hint in text for hint in REFERRAL_ACK_NEGATIONS):
            return False
        return any(hint in text for hint in REFERRAL_ACK_HINTS)

    def referral_ack_button(self):
        """(row, col, label) of the button that dismisses a confirmation screen."""
        for r, row in enumerate(self.buttons):
            for c, label in enumerate(row):
                norm = normalize_label(label)
                for hint in REFERRAL_ACK_BUTTONS:
                    if norm == hint or norm.startswith(hint + " "):
                        return r, c, label
                    # Longer hints may be decorated ("Continue → my login").
                    if len(hint) > 4 and hint in norm:
                        return r, c, label
        return None

    def referral_skip_button(self):
        """(row, col, label) of the bot's own skip / "don't have one" option."""
        for r, row in enumerate(self.buttons):
            for c, label in enumerate(row):
                norm = normalize_label(label)
                if any(hint in norm for hint in REFERRAL_SKIP_HINTS):
                    return r, c, label
        # Bare "No" style answers only - never substring matches, so options
        # such as "Normal" or "No KYC" can't be picked up by accident.
        for r, row in enumerate(self.buttons):
            for c, label in enumerate(row):
                if normalize_label(label) in ("no", "nope", "none", "not really"):
                    return r, c, label
        return None

    def referral_yes_button(self):
        """(row, col, label) of a Yes / "I have one" option, if present."""
        for r, row in enumerate(self.buttons):
            for c, label in enumerate(row):
                norm = normalize_label(label)
                if norm == "yes" or norm.startswith("yes,") or norm.startswith("yes "):
                    return r, c, label
                if "i have" in norm and "don't" not in norm and "dont" not in norm:
                    return r, c, label
        return None

    # -- number-checker screens --------------------------------------------

    def checker_button(self, extra_hints=(),
                       fallback_hints=DEFAULT_CHECK_BUTTON_FALLBACK_HINTS,
                       exclude=DEFAULT_CHECK_BUTTON_EXCLUDE):
        """
        (row, col, label) of the button that opens the bot's number checker,
        or None.

        Tried in order: the configured/default checker phrases, then a
        generic "check/verify/validate/status" pass over the remaining
        buttons. A button containing an excluded word (balance, offer, shop,
        ...) is never picked, so "Check Balance" can't be mistaken for it.
        """
        def blocked(label_norm):
            return any(word in label_norm for word in exclude)

        def pick(hints):
            for hint in hints:
                norm_hint = normalize_label(hint)
                if not norm_hint:
                    continue
                for r, row in enumerate(self.buttons):
                    for c, label in enumerate(row):
                        norm = normalize_label(label)
                        if norm_hint in norm and not blocked(norm):
                            return r, c, label
            return None

        return pick(tuple(extra_hints or ()) + tuple(DEFAULT_CHECK_BUTTON_HINTS)) \
            or pick(fallback_hints)

    def check_another_button(self, extra_hints=()):
        """
        (row, col, label) of a button that starts the NEXT check directly
        from a result screen (e.g. "Check Another Number"), or None.

        Used for continuous checking so the next number can be sent without
        tapping Start / Main Menu. For bots that accept a number directly at
        the result screen, this is a fallback when direct send doesn't work.
        """
        hints = tuple(extra_hints or ()) + tuple(DEFAULT_CHECK_ANOTHER_HINTS)
        for hint in hints:
            norm_hint = normalize_label(hint)
            if not norm_hint:
                continue
            for r, row in enumerate(self.buttons):
                for c, label in enumerate(row):
                    if norm_hint in normalize_label(label):
                        return r, c, label
        return None

    def looks_like_check_prompt(self, extra_hints=()):
        """
        True when the screen is asking for the number to check (the step
        between the checker button and the result). Kept conservative: the
        number is only typed into a screen this accepts.
        """
        # Menu / login / offer screens carry their own buttons and must never
        # be read as the checker's number prompt.
        if self.has_button("try another offer", "add account", "open shop",
                           "login with numb", "choose login mode",
                           "try another number"):
            return False
        text = normalize_label(self.text)
        if _hint_match(text, tuple(extra_hints or ()) + DEFAULT_CHECK_PROMPT_HINTS):
            return True
        if any(word in text for word in _CHECK_NUMBER_WORDS) and (
            any(word in text for word in _CHECK_ASK_WORDS)
        ):
            return True
        # A bare "10-digit mobile number" style heading with only its own
        # navigation buttons is still the prompt.
        return (
            any(word in text for word in ("10-digit", "10 digit", "10digit"))
            and self.has_button("main menu", "cancel", "back")
            and not self.has_button("add account", "open shop", "try another offer")
        )

    def looks_like_checking(self):
        """True for a transient 'checking, please wait...' screen."""
        text = normalize_label(self.text)
        if any(hint in text for hint in _CHECKING_HINTS):
            return not self.has_button("add account", "open shop", "try another offer")
        return False

    def check_verdict(self, registered_hints=(), not_registered_hints=()):
        """
        Read a checker RESULT screen.

        Returns True (registered), False (not registered) or None when the
        screen carries no verdict. Negative wording is checked first because
        "not registered" contains "registered"; extra phrase lists can be
        supplied from config for a bot revision whose copy is different.
        """
        text = normalize_label(self.text)
        raw = self.text or ""
        reg = tuple(DEFAULT_CHECK_REGISTERED_HINTS) + tuple(registered_hints or ())
        unreg = tuple(DEFAULT_CHECK_NOT_REGISTERED_HINTS) + tuple(not_registered_hints or ())

        # A screen that is asking for the number, or showing the transient
        # "checking…" state, is never a verdict - however it is worded (a
        # prompt may legitimately say "...tell you whether it IS REGISTERED").
        # Unless it also quotes the number next to a ✅/❌ marker, in which case
        # it is a result.
        marked_result = bool(re.search(r"\b\d{10}\b", raw)) and any(
            emoji in raw for emoji in _CHECK_YES_EMOJI + _CHECK_NO_EMOJI
        )
        if not marked_result and (self.looks_like_check_prompt()
                                  or self.looks_like_checking()):
            return None

        neg = _hint_match(text, unreg)
        pos = _hint_match(text, reg)
        if neg and not pos:
            return False
        if pos and not neg:
            return True
        if pos and neg:
            # Both vocabularies matched: trust the explicit negative wording.
            return False

        # No configured phrase matched: look for the word itself next to a
        # negation or an ❌ / ✅ marker.
        for match in re.finditer(r"regist(?:ered|ration|er)", text):
            window = text[max(0, match.start() - 32): match.end() + 24]
            raw_window = raw[max(0, match.start() - 32): match.end() + 24]
            if (any(emoji in raw_window for emoji in _CHECK_NO_EMOJI)
                    or re.search(r"\b(not|no|never|isn'?t|isnt|wasn'?t|hasn'?t|without|un)\b",
                                 window)):
                return False
        for match in re.finditer(r"regist(?:ered|ration|er)", text):
            window = text[max(0, match.start() - 16): match.end() + 16]
            raw_window = raw[max(0, match.start() - 16): match.end() + 16]
            if any(emoji in raw_window for emoji in _CHECK_YES_EMOJI):
                return True

        # Emoji-only results: a "check"/"result" screen quoting the number
        # with exactly one kind of marker.
        if (re.search(r"\b\d{10}\b", raw)
                and any(word in text for word in ("check", "result", "status"))):
            yes = [e for e in _CHECK_YES_EMOJI if e in raw]
            no = [e for e in _CHECK_NO_EMOJI if e in raw]
            if no and not yes:
                return False
            if yes and not no:
                return True

        # Last resort: the screen talks about registration without negating
        # it, and is not itself a prompt / transient / menu / offer screen.
        if ("regist" in text and not self.looks_like_check_prompt()
                and not self.looks_like_checking()
                and not self.has_button("add account", "open shop",
                                        "login with numb", "try another offer")):
            return True
        return None

    # Matches a 10-digit number or an "+91<10 digits>" one (the dedicated
    # checker bot prints the latter in its result lines).
    _CHECK_NUMBER_RE = re.compile(r"(?:\+?91|91)?\D?(\d{10})(?!\d)")

    @classmethod
    def _extract_check_numbers(cls, text):
        results = []
        for match in cls._CHECK_NUMBER_RE.finditer(text or ""):
            digits = match.group(1)
            if digits not in results:
                results.append(digits)
        return results

    def _check_line_verdict(self, line, registered_hints, not_registered_hints):
        """
        The verdict carried by ONE line of a checker result screen.

        Lines like 'NEW +91889... — NEW USER' / '+91889... — REGISTERED' /
        'NOT REGISTERED (NEW USER)' each say their own verdict; verdict words
        for OTHER numbers quoted on the same line are not trusted.
        """
        text = normalize_label(line)
        raw = line or ""
        reg = tuple(DEFAULT_CHECK_REGISTERED_HINTS) + tuple(registered_hints or ())
        unreg = tuple(DEFAULT_CHECK_NOT_REGISTERED_HINTS) + tuple(not_registered_hints or ())
        neg = _hint_match(text, unreg)
        pos = _hint_match(text, reg)
        if neg and not pos:
            return False
        if pos and not neg:
            return True
        if pos and neg:
            return False  # an explicit negative always wins
        # Emoji badges next to the number: ✅ = registered, 🆕/❌ = fresh.
        if any(emoji in raw for emoji in ("✅", "✔", "☑")):
            return True
        if any(emoji in raw for emoji in ("🆕", "🚫", "⛔", "❌", "✖", "❎")):
            return False
        return None

    def parse_check_verdicts(self, registered_hints=(), not_registered_hints=()):
        """
        Extract {digits: verdict} from a checker result screen (or a small set
        of contiguous result messages).

        The dedicated checker bot reports one number per line in a MIXED
        order (a multi-number check answers with lines like
        '+91889... — NEW USER' / '+91889... — REGISTERED'), so the result is
        keyed by the 10-digit number, never by position.
        """
        raw = self.text or ""
        lines = [line for line in (line.strip() for line in raw.splitlines()) if line]
        results = {}
        all_numbers = []
        for line in lines:
            numbers = self._extract_check_numbers(line)
            if not numbers:
                continue
            for digits in numbers:
                if digits not in all_numbers:
                    all_numbers.append(digits)
            verdict = self._check_line_verdict(line, registered_hints, not_registered_hints)
            if verdict is not None:
                for digits in numbers:
                    results[digits] = verdict

        if not all_numbers:
            return results

        # Single-number screens ("NOT REGISTERED (NEW USER) / <number>"): the
        # verdict is NOT on the number's own line, so read the whole screen.
        unique = [n for n in all_numbers if n not in results]
        if len(all_numbers) == 1 and unique:
            verdict = self.check_verdict(registered_hints, not_registered_hints)
            if verdict is not None:
                results[all_numbers[0]] = verdict
        return results

    def classify_check(self, registered_hints=(), not_registered_hints=(),
                       prompt_hints=()):
        """
        Classification used by the bot-checker flow. The transient
        "checking..." state is tested before the prompt so a working screen
        never gets the number typed into it a second time.
        """
        if self.check_verdict(registered_hints, not_registered_hints) is not None:
            return S_CHECK_RESULT
        if self.looks_like_checking():
            return S_CHECKING
        if self.looks_like_check_prompt(prompt_hints):
            return S_CHECK_PROMPT
        return S_UNKNOWN

    # -- regex helpers -----------------------------------------------------

    def search(self, pattern):
        m = re.search(pattern, self.text or "", flags=re.IGNORECASE | re.DOTALL)
        return m

    @property
    def upi_price(self):
        """Parse the 'UPI . ₹47' line into a float, or None."""
        m = self.search(r"upi\s*[^\d₹]{0,6}₹?\s*(\d+(?:\.\d+)?)")
        return float(m.group(1)) if m else None

    @property
    def user_id(self):
        m = self.search(r"user\s*id\s*[·\-:|]?\s*(\d{5,})")
        return m.group(1) if m else None

    @property
    def account_number(self):
        m = self.search(r"account\s*#\s*(\d+)")
        if m:
            return m.group(1)
        m = self.search(r"#\s*(\d{4,})")
        return m.group(1) if m else None

    @property
    def linked_number(self):
        m = self.search(r"Mobile\s*[·\-:|]?\s*(\d{10})")
        return m.group(1) if m else None

    # -- classification ----------------------------------------------------

    def classify(self):
        t = normalize_label(self.text)
        if "account linked" in t:
            return S_LINKED
        if ("otp on its way" in t or "we've sent a code" in t
                or "sent a code to" in t or "type it here when it arrives" in t):
            return S_OTP_WAIT
        # Transient: shown right after the number is sent, before the
        # "OTP on its way" screen. Checked after S_OTP_WAIT so a screen that
        # mentions both still wins as the real OTP prompt.
        if "sending your otp" in t or "sending otp" in t:
            return S_SENDING_OTP
        # Transient: shown right after the OTP code is submitted, before the
        # "Account linked!" (or code-error) screen appears. Matched on the
        # "-ing" form together with a code/otp word, so failure copy
        # ("verification failed", "could not verify your code") never lands
        # here - it falls through to the error screens below.
        if ("verifying" in t and ("code" in t or "otp" in t)) or (
            t.startswith("checking your code") or t.startswith("confirming your code")
        ):
            return S_VERIFYING
        # Transient: the bot is fetching the next screen ("⏳ Setting things
        # up…") between a tap and the offer it produces. Checked before the
        # offer heuristics so such an edit is waited through instead of being
        # read as a dead end; a screen carrying a price or a reroll button is
        # offer family and keeps its own classification.
        if (self.upi_price is None and self.reroll_button() is None
                and _hint_match(t, WORKING_HINTS)):
            return S_WORKING
        # Menu / login-mode / offer screens claim priority over the referral
        # check: only the dedicated promotion screen may classify as referral.
        if self.has_button("try another offer") or (
            "10-digit mobile" in t and "continue" in t
        ):
            return S_OFFER
        if "choose login mode" in t or (
            self.has_button("normal") and self.has_button("auto")
        ):
            return S_LOGIN_MODE
        if "how would you like to link" in t or self.has_button("login with numb"):
            return S_LINK_CHOICE
        if self.has_button("add account") and self.has_button("open shop"):
            return S_MENU
        if self.is_referral:
            return S_REFERRAL
        if "expired" in t and ("code" in t or "otp" in t):
            return S_EXPIRED
        if ("blocked" in t or "banned" in t) and ("meesho" in t or "account" in t or "number" in t):
            return S_BLOCKED
        if ("incorrect" in t or "wrong" in t or "invalid code" in t) and (
            "code" in t or "otp" in t
        ):
            return S_WRONG_OTP
        if ("already" in t and "registered" in t) or "already have an account" in t:
            return S_BLOCKED
        return S_UNKNOWN


# ---------------------------------------------------------------------------
# Userbot
# ---------------------------------------------------------------------------

def normalize_username(username):
    """'@SomeBot', 'SomeBot', ' somebot ' -> 'somebot' ('' for nothing)."""
    return str(username or "").strip().lstrip("@").strip().lower()


def _make_conversation_proxy_class(base_cls):
    """
    Build the "second conversation" class for `base_cls` (see
    MeeshoBotClient._conversation_proxy).
    """

    class _ConversationProxy(base_cls):

        _PROXY_FIELDS = ("_proxy_base", "_proxy_entity", "_proxy_username")

        def __init__(self, base, entity, username):
            object.__setattr__(self, "_proxy_base", base)
            object.__setattr__(self, "_proxy_entity", entity)
            object.__setattr__(self, "_proxy_username", username)

        # -- everything shared comes from the base client -------------------

        def __getattr__(self, name):
            if name in self._PROXY_FIELDS:
                raise AttributeError(name)
            return getattr(object.__getattribute__(self, "_proxy_base"), name)

        def __setattr__(self, name, value):
            if name in self._PROXY_FIELDS:
                return object.__setattr__(self, name, value)
            if name == "_bot_entity":
                # Never steer the shared conversation from a proxy.
                return
            setattr(object.__getattribute__(self, "_proxy_base"), name, value)

        # -- the one thing that is NOT shared --------------------------------

        @property
        def _bot_entity(self):
            return object.__getattribute__(self, "_proxy_entity")

        # -- binding ---------------------------------------------------------

        @property
        def bound(self):
            return object.__getattribute__(self, "_proxy_entity") is not None

        @property
        def conversation_username(self):
            return object.__getattribute__(self, "_proxy_username")

        async def _a_bind(self):
            """
            Resolve this conversation's entity ON the userbot loop and pin it.
            Awaiting here is what makes a dedicated checker bot real: before
            this, `get_entity` was called from a worker thread and (on an
            already-running loop) threw - and the old code answered that
            failure with the PRIMES bot's entity, so the "dedicated" check
            ran in the login conversation (and leaked an un-awaited
            coroutine: "coroutine '...get_entity' was never awaited").
            """
            base = object.__getattribute__(self, "_proxy_base")
            name = object.__getattribute__(self, "_proxy_username")
            entity = await base._a_bind_entity(name)
            object.__setattr__(self, "_proxy_entity", entity)
            return entity

        @property
        def shares_login_conversation(self):
            """
            True when this proxy would talk to the PRIMES LOGIN bot - i.e.
            when the configured "dedicated" checker bot is the login bot
            (misconfiguration) or the entity was never bound.
            """
            entity = object.__getattribute__(self, "_proxy_entity")
            if entity is None:
                return True  # unbound: the call would land on the login bot
            base = object.__getattribute__(self, "_proxy_base")
            name = normalize_username(object.__getattribute__(self, "_proxy_username"))
            login = normalize_username(getattr(base, "bot_username", ""))
            if name and name == login:
                return True
            return entity is getattr(base, "_bot_entity", None)

        def conversation_conflict_reason(self, owner):
            """
            "" or why `owner` must not navigate this conversation: it is the
            shared PRIMES chat and something else is driving it.
            """
            if not self.shares_login_conversation:
                return ""  # its own chat: it can never collide
            base = object.__getattribute__(self, "_proxy_base")
            return base.conversation_conflict_reason(owner)

        def _conversation_label(self):
            name = normalize_username(object.__getattribute__(self, "_proxy_username"))
            if not name:
                return ""
            if self.shares_login_conversation:
                return f"@{name} - the PRIMES login bot"
            return f"@{name} - the dedicated checker bot"

    return _ConversationProxy

class MeeshoBotClient:

    def __init__(self, config=None, log_fn=print, bot_username=None):
        config = config or {}
        conf = config.get("meesho_bot", {}) or {}
        self.conf = conf
        self._log = log_fn

        self.enabled = bool(conf.get("enabled", False))
        self.api_id = conf.get("api_id")
        self.api_hash = (conf.get("api_hash") or "").strip()
        self.session_file = conf.get("session_file", "userbot.session.txt")
        # The login bot's username. Overrides (a dedicated checker bot driven
        # through the same Telethon account) are applied after __init__.
        self.bot_username = (conf.get("bot_username") or "").strip()

        self.target_upi_price = float(conf.get("target_upi_price", 47))
        self.max_offer_rerolls = int(conf.get("max_offer_rerolls", 30))
        # Reroll budget for the OFFER PRE-WARM (the bot is parked on an agreed
        # offer while the workers are still hunting). It has no paid number
        # waiting, so it may use the normal budget; lower it only if the bot
        # throttles long reroll sessions. 0 = same as max_offer_rerolls.
        try:
            warm_budget = int(conf.get("warmup_max_offer_rerolls", 0) or 0)
        except (TypeError, ValueError):
            warm_budget = 0
        self.warmup_max_offer_rerolls = warm_budget if warm_budget > 0 else self.max_offer_rerolls
        # How often a parked offer prompt is re-verified (seconds). The
        # coordinator re-arms the bot when the prompt has disappeared.
        try:
            self.offer_warm_refresh_seconds = float(
                conf.get("offer_warm_refresh_seconds", 90) or 0) or 90.0
        except (TypeError, ValueError):
            self.offer_warm_refresh_seconds = 90.0
        self.max_change_number = int(conf.get("max_change_number", 5))
        self.step_timeout = float(conf.get("step_timeout_seconds", 60))
        self.poll_interval = float(conf.get("poll_interval_seconds", 1.2))

        # Change Number recovery tuning. The number prompt normally appears a
        # second or two after the tap, so it gets its own (short) settle budget
        # instead of the full step timeout, and a tap the bot ignores is
        # retried: both are far cheaper than the alternative - dropping back to
        # the main menu, walking Add Account -> Login with Number -> Normal and
        # re-rolling the offer while the paid number's OTP window runs.
        self.change_number_retries = max(0, int(conf.get("change_number_retries", 2)))
        try:
            change_timeout = float(conf.get("change_number_timeout_seconds", 0) or 0)
        except (TypeError, ValueError):
            change_timeout = 0.0
        self.change_number_timeout = (
            change_timeout if change_timeout > 0
            else max(5.0, min(self.step_timeout, 20.0))
        )
        self.change_number_variant_taps = max(
            0, int(conf.get("change_number_variant_taps", 3)))
        # Extra wait rounds for the transient "⏳ Setting things up…" screen the
        # bot shows between a reroll tap and the offer it produces. It is
        # normally over in a second, but a throttled bot (many rerolls in a row)
        # can hold it longer than one step timeout - that must not kill a paid
        # number with "Offer screen has no reroll button".
        self.working_screen_waits = max(0, int(conf.get("working_screen_waits", 2)))
        # Overall cap for one Change Number recovery (taps + waits together), so
        # the retry loop can never take longer than the single full-step wait it
        # replaces. 0 = auto: 30-45s, whatever is closest to the step timeout.
        try:
            change_budget = float(conf.get("change_number_budget_seconds", 0) or 0)
        except (TypeError, ValueError):
            change_budget = 0.0
        self.change_number_budget = (
            change_budget if change_budget > 0
            else max(30.0, min(self.step_timeout, 45.0))
        )
        # Extra wording for the login number prompt (per bot revision), on top
        # of NUMBER_PROMPT_HINTS.
        self.number_prompt_hints = tuple(conf.get("number_prompt_hints") or ())
        # Reuse a number prompt the bot is already sitting on instead of
        # restarting the flow from the main menu.
        self.reuse_number_prompt = bool(conf.get("reuse_number_prompt", True))
        # What the coordinator does when Change Number cannot be recovered:
        #   "auto"   - reset to the main menu only when the bot checker is
        #              needed for the next number check (checker.mode "bot", or
        #              "auto" while the checker API is down / cooling down);
        #              with a working API the bot stays in-flow.
        #   "always" - always reset (the old behaviour).
        #   "never"  - never reset from here (prepare_login still walks back to
        #              the menu itself when the bot really is lost).
        policy = str(conf.get("reset_to_menu_on_change_failure", "auto")
                     or "auto").strip().lower()
        self.menu_reset_policy = policy if policy in ("auto", "always", "never") else "auto"

        # Hang watchdog for a whole bot flow. A flow is aborted only when it
        # stops making progress for this long (a single Telethon request may
        # legitimately be slow, but every screen poll / tap / settle step that
        # completes resets the clock). 0 = auto: max(180, 4 x step_timeout).
        # Without this, a full prepare_login with many offer rerolls could
        # legitimately run for many minutes - the old blanket cap of
        # step_timeout + 30 killed healthy flows mid-reroll.
        self.flow_timeout_seconds = float(conf.get("flow_timeout_seconds", 0) or 0)
        delays = conf.get("human_delay_seconds", [1.0, 2.5])
        try:
            self.delay_min = float(delays[0])
            self.delay_max = float(delays[1])
        except Exception:
            self.delay_min, self.delay_max = 1.0, 2.5

        # Referral step (between "Login with Number" and the Normal/Auto mode).
        # The screen may or may not appear on a given login; when it does,
        # referral_link is pasted. referral_failure_action decides what happens
        # when that cannot be done:
        #   "stop" (default) - stop, report, cancel the number and tally the refund
        #   "skip"           - tap the bot's own "I don't have a refer code" button
        link = conf.get("referral_link") or conf.get("meesho_referral_link") or ""
        self.referral_link = link.strip() if isinstance(link, str) else ""
        action = str(conf.get("referral_failure_action", "stop") or "stop").strip().lower()
        self.referral_failure_action = action if action in ("stop", "skip") else "stop"
        # The bot commonly asks twice in one login - first "🔗 Set Refer Link"
        # (saves it for future logins), then "🎁 Referral link?" per account -
        # so the link is pasted once for each prompt by default.
        self.max_referral_pastes = int(conf.get("max_referral_pastes", 2))
        self.max_referral_events = int(conf.get("max_referral_events", 4))

        # Bot number-checker (checker.mode = "bot" / "auto", see
        # checker_router.py): the bot's own "is this number registered?"
        # service. Configuration lives under "checker" -> "bot"; every hint
        # list can be extended for a bot revision whose copy differs.
        self.checker_conf = dict((config.get("checker", {}) or {}).get("bot", {}) or {})

        # One conversation with one bot: a number check and a login flow must
        # never interleave. Reentrant, because a public wrapper may end up
        # calling another one from the same thread.
        self._flow_lock = threading.RLock()

        # Telegram FloodWait tracking: same account drives both PRIMES and
        # dedicated checker bot, so a FloodWait on one applies to the other.
        # _floodwait_until = timestamp until which we should not send.
        self._floodwait_until = 0.0
        self._floodwait_lock = threading.RLock()

        # Per-login referral bookkeeping (reset by _a_prepare_login).
        self._referral_events = 0
        self._referral_pastes_in_flow = 0
        self._last_referral_problem = None
        self.last_referral_action = None

        self._client = None
        self._loop = None
        self._thread = None
        self._bot_entity = None
        self._start_error = None

        # Entities of the OTHER conversations opened on this session, keyed by
        # normalized username (a dedicated checker bot is driven through the
        # same Telethon account). They can only be resolved by awaiting
        # get_entity() ON the userbot loop (see _a_bind_entity), so this cache
        # is what the synchronous code paths read.
        self._entity_lock = threading.Lock()
        self._entities = {}

        # Who is driving the shared PRIMES conversation right now
        # ("login" / "prewarm" / None). The coordinator's _BotClaim sets it;
        # a check that would navigate the SAME chat refuses to walk away from
        # a pre-warm / login screen and says so instead (see
        # conversation_conflict_reason).
        self._conversation_lock = threading.RLock()
        self._conversation_holder = None
        self._conversation_since = 0.0

        # Cross-thread bookkeeping for _run's hang watchdog: the loop thread
        # bumps _last_progress whenever a step completes, and records what the
        # in-flight flow was doing in _step_note so a timeout alert can say
        # whether e.g. the number/code had already been sent.
        self._last_progress = time.time()
        self._step_note = ""

    # -- lifecycle ----------------------------------------------------------

    @property
    def ready(self):
        return bool(
            self.enabled
            and self.api_id
            and self.api_hash
            and self.bot_username
            and self._session_string()
            and self._client is not None
        )

    @property
    def start_error(self):
        return self._start_error

    @property
    def referral_summary(self):
        """One-line description of the referral configuration, for logs/alerts."""
        if self.referral_link:
            return (f"referral link configured: {self.referral_link} "
                    f"(on failure: {self.referral_failure_action})")
        if self.referral_required:
            return ("no referral link configured - if the bot asks for one the "
                    "automation stops and reports (set it with /referral <link>)")
        return ("no referral link configured - the bot's own skip option is used "
                "if the bot asks for one")

    @property
    def referral_required(self):
        """True when a referral link is mandatory for any login that asks for it."""
        return self.referral_failure_action == "stop"

    def set_referral_link(self, link):
        """
        Apply a referral link at runtime (used by the Telegram /referral
        command). An empty value clears it. Returns the previous value.
        """
        previous = self.referral_link
        self.referral_link = (link or "").strip()
        return previous

    def _session_string(self):
        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""

    # -- hang watchdog --------------------------------------------------------

    @property
    def stall_timeout(self):
        """Seconds without any bot-screen progress before a flow is aborted."""
        if self.flow_timeout_seconds > 0:
            return self.flow_timeout_seconds
        # Generous on purpose: this must never fire on a healthy (if slow)
        # flow - the flow's own per-step budgets bound those. It exists to
        # catch a truly hung Telegram call (no poll, tap or settle finishing).
        return max(180.0, 4.0 * self.step_timeout)

    def _progress(self):
        """
        Heartbeat from the loop thread: any completed step (screen fetch,
        poll, tap, message send) keeps the running flow alive. Called from
        coordinator threads it (re)arms the watchdog for a new _run() call.
        """
        self._last_progress = time.time()

    def _note(self, what):
        """Record what the in-flight flow is doing, for timeout alerts."""
        self._step_note = what


    def _human_delay(self):
        if self.delay_max <= 0:
            return
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def start(self):
        """Start the background asyncio loop and connect the user session."""
        if not self.enabled:
            self._start_error = "meesho_bot.enabled is false"
            return False
        missing = []
        if not self.api_id:
            missing.append("api_id")
        if not self.api_hash:
            missing.append("api_hash")
        if not self.bot_username:
            missing.append("bot_username")
        if not self._session_string():
            missing.append(f"session file ({self.session_file}) - run login_userbot.py")
        if missing:
            self._start_error = "Missing: " + ", ".join(missing)
            self._log(f"[MEESHO-BOT] userbot not ready: {self._start_error}")
            return False

        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError:
            self._start_error = "telethon not installed (pip install telethon)"
            self._log("[MEESHO-BOT] telethon not installed; manual trigger flow will be used.")
            return False

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="meesho-bot-loop", daemon=True
        )
        self._thread.start()

        async def _connect():
            client = TelegramClient(
                StringSession(self._session_string()),
                int(self.api_id),
                self.api_hash,
                connection_retries=2,
                timeout=self.step_timeout,
            )
            await client.connect()
            if not await client.is_user_authorized():
                raise MeeshoBotNotConfigured("userbot session is not authorized; re-run login_userbot.py")
            self._bot_entity = await client.get_entity(self.bot_username)
            with self._entity_lock:
                self._entities[normalize_username(self.bot_username)] = self._bot_entity
            self._client = client
            return True

        try:
            self._run(_connect())
            self._log(f"[MEESHO-BOT] userbot connected; controlling {self.bot_username}")
            return True
        except Exception as exc:
            self._start_error = str(exc)
            self._client = None
            self._log(f"[MEESHO-BOT] failed to start: {exc}")
            return False

    def stop(self):
        if self._client is None or self._loop is None:
            return

        async def _disconnect():
            try:
                await self._client.disconnect()
            except Exception:
                pass
            # Abort any step still in flight (e.g. one that hit the watchdog)
            # so no task is left pending when the loop stops ("Task was
            # destroyed but it is pending!").
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            self._run(_disconnect(), timeout=10)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        # Mark the client stopped: .ready turns False and a later start()
        # builds a fresh loop/thread/session instead of reusing a closed loop.
        self._client = None

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            # Close the loop in its own thread once it stops: an unclosed
            # loop raises "Invalid file descriptor" noise from __del__ when
            # the process (or a test run) tears down.
            try:
                self._loop.close()
            except Exception:
                pass

    def _run(self, coro, timeout=None):
        """
        Run `coro` on the userbot loop from a coordinator thread and wait.

        `timeout` is an optional hard wall-clock cap (used by stop() and the
        diagnostics). Without one there is NO total cap: a full
        prepare_login with up to max_offer_rerolls rerolls legitimately takes
        longer than one step, so the old blanket cap of step_timeout + 30
        aborted healthy flows mid-reroll with a bare TimeoutError that crashed
        the whole automation. Instead, a watchdog aborts the call when the
        coroutine stops making progress for stall_timeout seconds (a hung
        Telethon call / dead connection) - every completed poll, tap or
        settle resets that clock.

        On abort the coroutine is cancelled on its loop (so it cannot leak as
        a pending task) and a MeeshoBotTimeout is raised - a MeeshoBotError,
        which the coordinator handles like any other bot failure.

        Every timeout below is recognised through _is_timeout_error(), never
        with a bare `except TimeoutError`: on Python < 3.11 future.result()
        raises a DIFFERENT class, so the first slice used to escape this
        method as a bare, message-less TimeoutError - "every flow failed
        after ~2s" while its coroutine kept driving the chat (see that
        helper's docstring).
        """
        if self._loop is None:
            raise MeeshoBotError("userbot event loop not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        self._progress()
        deadline = (time.time() + timeout) if timeout else None
        stall = self.stall_timeout
        op = (getattr(coro, "__name__", "") or "bot step").replace("_a_", "", 1)
        # Poll in short slices so the watchdog is checked between them.
        slice_wait = max(0.1, min(2.0, stall / 4.0))

        try:
            while True:
                wait = slice_wait
                if deadline is not None:
                    wait = min(wait, max(0.05, deadline - time.time()))
                try:
                    return future.result(timeout=wait)
                except Exception as exc:
                    if not _is_timeout_error(exc):
                        # The flow itself failed (unknown screen, referral
                        # problem, ...): its own error is the informative one.
                        raise
                    if future.done():
                        # The future finished in the race window right at the
                        # slice boundary: return its value, or - if the
                        # coroutine itself failed with a timeout (e.g. a
                        # Telethon request timeout) - surface that as a bot
                        # failure, never as a bare TimeoutError that crashes
                        # the coordinator.
                        try:
                            return future.result()
                        except Exception as inner:
                            if not _is_timeout_error(inner):
                                raise
                            raise MeeshoBotTimeout(
                                f"PRIMES bot flow '{op}' failed with a "
                                f"Telegram timeout"
                                f"{' (stage: ' + self._step_note + ')' if self._step_note else ''}."
                            ) from inner
                    # else: just the slice elapsing - check the deadlines below.

                now = time.time()
                hard = deadline is not None and now >= deadline
                stalled = now - self._last_progress >= stall
                if not (hard or stalled):
                    continue

                note = f" (stage: {self._step_note})" if self._step_note else ""
                reason = (
                    f"hard {timeout:.0f}s limit reached" if hard
                    else f"no screen/tap progress for {now - self._last_progress:.0f}s "
                         f"(watchdog {stall:.0f}s)"
                )
                message = (f"PRIMES bot flow '{op}' aborted: {reason}{note}. The "
                           f"Telegram side stopped responding mid-flow, so the "
                           f"number/code state is unknown - check the bot manually.")
                if future.cancel():
                    # run_coroutine_threadsafe chains this cancel onto the asyncio
                    # task, so the coroutine unwinds on its loop instead of
                    # lingering as a pending task until the loop is torn down.
                    raise MeeshoBotTimeout(message)
                # It finished inside the race window right at the deadline: its
                # real result (or error, e.g. an unknown screen with its text and
                # buttons) is more informative than the timeout.
                try:
                    return future.result(timeout=1.0)
                except Exception as exc:
                    if not _is_timeout_error(exc):
                        raise
                    raise MeeshoBotTimeout(message) from exc
        finally:
            # Leak guard: however this call exits, the coroutine must not be
            # left running on the userbot loop. It drives the SAME Telegram
            # chat, so an abandoned flow keeps tapping it while the next flow
            # starts (two flows in one conversation: the offer never sticks
            # and a paid number's OTP screen gets walked away from). Calling
            # cancel() on this not-yet-finished concurrent future cancels the
            # asyncio task it was chained to.
            if not future.done() and future.cancel():
                self._log(f"[MEESHO-BOT] Cancelled an abandoned '{op}' flow "
                          f"that was still running on the userbot loop.")

    # -- low-level Telethon helpers ----------------------------------------

    async def _latest_screen(self, limit=6):
        messages = await self._client.get_messages(self._bot_entity, limit=limit)
        self._progress()
        for message in messages:
            if getattr(message, "text", None):
                return Screen.from_telethon(message)
        return Screen()

    async def _signatures(self, limit=6):
        signatures = set()
        messages = await self._client.get_messages(self._bot_entity, limit=limit)
        self._progress()
        for m in messages:
            signatures.add((m.id, str(getattr(m, "edit_date", None)), (m.text or "")[:32]))
        return signatures

    async def _wait_new_screen(self, before, timeout=None, expect=None):
        """
        Poll until the bot shows a message different from the `before`
        signatures (new message id or edited message), then return it.
        """
        timeout = timeout or self.step_timeout
        deadline = time.time() + timeout
        last_screen = Screen()
        while time.time() < deadline:
            await asyncio.sleep(self.poll_interval)
            try:
                messages = await self._client.get_messages(self._bot_entity, limit=4)
            except Exception:
                self._progress()
                continue
            self._progress()
            for m in messages:
                text = getattr(m, "text", None)
                if not text:
                    continue
                last_screen = Screen.from_telethon(m)
                signature = (m.id, str(getattr(m, "edit_date", None)), (text or "")[:32])
                if signature not in before:
                    return last_screen
        raise MeeshoBotUnknownScreen(
            f"Timed out waiting for bot screen (expected {expect})",
            last_screen.text, last_screen.button_labels,
        )

    def _check_floodwait(self):
        """Raise if we are still in FloodWait cooldown."""
        with self._floodwait_lock:
            until = self._floodwait_until
        if until > time.time():
            remaining = int(until - time.time())
            raise MeeshoBotError(
                f"A wait of {remaining} seconds is required (FloodWait cooldown, "
                f"until {time.strftime('%H:%M:%S', time.localtime(until))})"
            )

    def _set_floodwait(self, seconds):
        """Record FloodWait so both PRIMES and dedicated checker share the limit."""
        seconds = max(0, int(seconds or 0))
        if seconds <= 0:
            return
        with self._floodwait_lock:
            self._floodwait_until = max(self._floodwait_until, time.time() + seconds)
        self._log(f"[MEESHO-BOT] Telegram FloodWait: {seconds}s - pausing all bot checks "
                  f"until {time.strftime('%H:%M:%S', time.localtime(self._floodwait_until))}")

    def floodwait_remaining(self):
        """Seconds left in FloodWait cooldown, or 0."""
        with self._floodwait_lock:
            until = self._floodwait_until
        return max(0.0, until - time.time())

    @staticmethod
    def _extract_floodwait_seconds(exc):
        """Extract seconds from FloodWaitError or its message."""
        # Telethon FloodWaitError has .seconds attribute
        try:
            secs = getattr(exc, "seconds", None)
            if secs is not None:
                return int(secs)
        except Exception:
            pass
        # Parse from message: \"A wait of X seconds is required\"
        import re as _re
        m = _re.search(r"wait of (\d+)", str(exc), _re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
        return None

    async def _click(self, screen, *needles):
        self._check_floodwait()
        found = screen.find_button(*needles)
        if not found:
            raise MeeshoBotUnknownScreen(
                f"Button {needles} not found on screen",
                screen.text, screen.button_labels,
            )
        row, col, _label = found

        # Find the actual message carrying this button (usually the newest).
        messages = await self._client.get_messages(self._bot_entity, limit=6)
        target_msg = None
        for m in messages:
            if Screen.from_telethon(m).find_button(*needles):
                target_msg = m
                break
        if target_msg is None:
            raise MeeshoBotUnknownScreen(
                f"Button {needles} vanished before click",
                screen.text, screen.button_labels,
            )

        before = await self._signatures()
        try:
            await target_msg.click(i=row, j=col)
        except Exception as exc:
            secs = self._extract_floodwait_seconds(exc)
            if secs is not None:
                self._set_floodwait(secs)
                raise MeeshoBotError(
                    f"A wait of {secs} seconds is required (caused by "
                    f"{type(exc).__name__}: {exc})"
                ) from exc
            raise
        self._progress()
        self._human_delay()
        return await self._wait_new_screen(before)

    async def _send_text(self, text, timeout=None):
        self._check_floodwait()
        before = await self._signatures()
        try:
            await self._client.send_message(self._bot_entity, str(text))
        except Exception as exc:
            secs = self._extract_floodwait_seconds(exc)
            if secs is not None:
                self._set_floodwait(secs)
                raise MeeshoBotError(
                    f"A wait of {secs} seconds is required (caused by "
                    f"{type(exc).__name__}: {exc})"
                ) from exc
            raise
        self._progress()
        self._human_delay()
        return await self._wait_new_screen(before, timeout=timeout)

    async def _cancel_to_menu(self):
        """
        Best-effort: tap Cancel / Main Menu until the main menu shows. A
        referral prompt is answered with its own skip option first (Cancel
        there only abandons the login, it does not reach the menu).
        """
        for _ in range(6):
            screen = await self._latest_screen()
            if screen.classify() == S_MENU:
                return screen

            needles = []
            if screen.classify() == S_REFERRAL and not screen.referral_acknowledged:
                skip = screen.referral_skip_button()
                if skip:
                    needles.append(skip[2])
            needles += ["cancel", "main menu"]

            advanced = False
            for needle in needles:
                try:
                    screen = await self._click(screen, needle)
                    advanced = True
                    break
                except MeeshoBotError:
                    continue
            if not advanced:
                break
        # Hard reset.
        before = await self._signatures()
        await self._client.send_message(self._bot_entity, "/start")
        self._human_delay()
        return await self._wait_new_screen(before)

    # -- referral screen -----------------------------------------------------

    def _referral_screen_error(self, screen, problem):
        """
        Loud, actionable error for a referral screen we cannot answer.

        In "stop" mode this is a MeeshoBotReferralError, which makes the
        coordinator stop the automation, report it, and cancel the number with
        a refund tally, rather than logging in without the referral link.
        """
        problem = problem or self._last_referral_problem or "unknown reason"
        configured = (f"meesho_bot.referral_link is set ({self.referral_link})"
                      if self.referral_link else
                      "meesho_bot.referral_link is empty")
        next_step = ("Set it with /referral <link> in Telegram or "
                     "python main.py --set-referral-link <link>, then start again; "
                     'or set meesho_bot.referral_failure_action to "skip" to log in '
                     "without a referral link.")
        message = (f"Referral step failed: {problem}. {configured}. "
                   f"Buttons on screen: {screen.button_summary}. {next_step}")
        error_class = MeeshoBotReferralError if self.referral_required else MeeshoBotUnknownScreen
        return error_class(message, screen.text, screen.button_labels)

    async def _a_resolve_referral(self, screen):
        """
        Answer the bot's referral screen and return the screen it produced.

        Order of preference with a usable link:
          1. a configured referral_link that has not been pasted yet in this
             login -> paste it (a Yes/No question is answered with Yes first);
          2. if the link was refused/asked for again, or none is configured:
             - "stop" mode (default): raise MeeshoBotReferralError so the
               coordinator stops, reports, and cancels the number with a refund
               tally - the login never continues without the referral link;
             - "skip" mode: tap the bot's own "I don't have a refer code" button.
        """
        self._referral_events += 1
        if self._referral_events > self.max_referral_events:
            raise self._referral_screen_error(
                screen,
                f"the screen keeps re-appearing ({self._referral_events - 1} answered already)",
            )

        skip = screen.referral_skip_button()
        yes = screen.referral_yes_button()
        can_paste = bool(self.referral_link) and self._referral_pastes_in_flow < self.max_referral_pastes

        self._log(f"[MEESHO-BOT] Referral screen: {screen.button_summary} "
                  f"(link {'set' if self.referral_link else 'unset'}, "
                  f"paste #{self._referral_pastes_in_flow + 1}, "
                  f"seen #{self._referral_events}, on failure: {self.referral_failure_action})")

        if can_paste:
            if yes is not None and not screen.referral_prompt:
                # "Do you have a referral link?" -> Yes reveals the paste prompt.
                self.last_referral_action = f"answered '{yes[2]}'"
                self._log("[MEESHO-BOT] Referral screen: tapping the Yes option.")
                new_screen = await self._click(screen, yes[2])
                return new_screen
            self._referral_pastes_in_flow += 1
            self.last_referral_action = "pasted referral link"
            self._log(f"[MEESHO-BOT] Referral screen: pasting referral link "
                      f"({self.referral_link}).")
            before = await self._signatures()
            await self._client.send_message(self._bot_entity, self.referral_link)
            self._human_delay()
            new_screen = await self._wait_new_screen(before)
            if new_screen.classify() == S_REFERRAL:
                if new_screen.referral_rejected:
                    # A refused link is not retried: no point pasting it again.
                    self._last_referral_problem = (
                        "the bot rejected the configured referral link "
                        "(invalid, expired or already used)"
                    )
                    self._referral_pastes_in_flow = self.max_referral_pastes
                else:
                    # Being asked again is normal (save-the-link screen, then the
                    # per-account prompt); the paste budget decides when it stops
                    # making sense.
                    remaining = max(0, self.max_referral_pastes - self._referral_pastes_in_flow)
                    self._log("[MEESHO-BOT] Referral screen reappeared after pasting; "
                              f"{remaining} paste(s) left this login.")
                    if remaining <= 0:
                        self._last_referral_problem = (
                            f"the bot kept asking for the referral link after "
                            f"{self._referral_pastes_in_flow} paste(s) "
                            f"(max_referral_pastes={self.max_referral_pastes})"
                        )
            return new_screen

        # There is nothing more to paste: explain exactly why.
        if self._last_referral_problem is None:
            if self.referral_link:
                self._last_referral_problem = (
                    f"the link was already pasted {self._referral_pastes_in_flow}x in "
                    f"this login (max_referral_pastes={self.max_referral_pastes})"
                )
            else:
                self._last_referral_problem = "no referral link is configured"

        if self.referral_required:
            raise self._referral_screen_error(screen, self._last_referral_problem)

        if skip is not None:
            self.last_referral_action = f"tapped '{skip[2]}'"
            self._log(f"[MEESHO-BOT] Referral screen: tapping '{skip[2]}' "
                      f"({self._last_referral_problem}; skip mode).")
            new_screen = await self._click(screen, skip[2])
            return new_screen

        raise self._referral_screen_error(
            screen, self._last_referral_problem + " and the screen offers no skip option"
        )

    # -- step settling -------------------------------------------------------

    async def _settle(self, screen, states, timeout=None, accept_prompt=False):
        """
        Poll the newest screens until one of `states` shows, answering the
        referral prompt whenever it interrupts (the bot inserts it between
        "Login with Number" and the login-mode/offer steps, and can re-offer it
        later in the flow).

        accept_prompt also stops on a number prompt that classify() does not
        recognise as S_OFFER (bot revisions with different copy) - without it
        such a screen burns the whole timeout before the caller can see it.

        Returns the matching screen, or whatever is on screen at the deadline -
        callers decide whether that is an error.
        """
        timeout = timeout or self.step_timeout
        deadline = time.time() + timeout
        current = screen

        def wanted(screen_):
            if screen_.classify() in states:
                return True
            return accept_prompt and self._at_number_prompt(screen_)

        while True:
            self._progress()
            if current.classify() == S_REFERRAL:
                if current.referral_acknowledged:
                    # Confirmation, not a question: dismiss it if it has its own
                    # button, otherwise let the bot move on by itself.
                    ack = current.referral_ack_button()
                    if ack:
                        current = await self._click(current, ack[2])
                        continue
                else:
                    current = await self._a_resolve_referral(current)
                    continue
            if wanted(current):
                return current
            if time.time() >= deadline:
                return current
            await asyncio.sleep(self.poll_interval)
            current = await self._latest_screen()

    # -- high-level flow -----------------------------------------------------

    def _at_number_prompt(self, screen, require_marker=False):
        """
        True when `screen` is the login number prompt - classify()'s S_OFFER or
        a revision copy recognised by looks_like_number_prompt().
        """
        return screen.classify() == S_OFFER or screen.looks_like_number_prompt(
            self.number_prompt_hints, require_offer_marker=require_marker
        )

    def _reusable_prompt(self, screen):
        """
        True when a fresh login may send its number from this screen instead of
        walking back to the main menu and re-rolling the offer.

        Only a prompt the offer loop can actually work with is reused: one whose
        price already fits the target, or one that can be rerolled in place (the
        loop taps "Try Another Offer" / "Try Again" until the price fits). An
        unpriced prompt is reused only when its wording is an explicit number
        prompt - the price cannot be verified then, so the recognition has to be
        unambiguous (never the bot checker's "send the number" prompt).
        """
        if not self.reuse_number_prompt:
            return False
        if not self._at_number_prompt(screen, require_marker=True):
            return False
        price = screen.upi_price
        reroll = screen.reroll_button() is not None
        if price is not None:
            return price <= self.target_upi_price or reroll
        if reroll:
            return True
        return bool(_hint_match(
            normalize_label(screen.text),
            tuple(self.number_prompt_hints) + NUMBER_PROMPT_HINTS,
        ))

    async def _a_reach_offer(self, screen=None, allow_reuse=True,
                             price_optional=False, reroll_budget=None):
        """
        Walk the bot to a usable login number prompt with the offer agreed:

            reuse a prompt it already sits on
            -> Add Account -> Login with Number -> (referral) -> Normal
            -> reroll the offer until UPI <= target_upi_price

        No number is sent, so this can run BEFORE a number exists (offer
        pre-warm): the coordinator parks the bot here while the provider
        workers are still hunting, so the number that is eventually found is
        typed into a screen that is already waiting for it instead of paying
        for Add Account -> ... -> offer rerolls while its OTP window runs.

        Returns (screen, info):
            info["rerolls"]        - offer rerolls used
            info["upi"]            - the agreed UPI price (None when unreadable)
            info["reused_prompt"]  - the bot was already on a usable prompt
            info["early_stage"]    - "otp_sent" / "blocked" when the bot is
                                     already past the prompt, so the caller
                                     must not type a number anymore
        """
        rerolls = 0
        reused_prompt = False
        try:
            budget = int(reroll_budget if reroll_budget is not None
                         else self.max_offer_rerolls)
        except (TypeError, ValueError):
            budget = self.max_offer_rerolls
        budget = max(0, budget)
        info = {"rerolls": 0, "upi": None, "reused_prompt": False,
                "early_stage": None, "message": ""}

        # Referral budget is per login: one link paste, plus a couple of
        # interruptions, then the bot's own skip option is used. It is reset
        # here too because a pre-warm walk answers the referral step for the
        # login that will use this offer.
        self._referral_events = 0
        self._referral_pastes_in_flow = 0
        self._last_referral_problem = None
        self.last_referral_action = None

        if screen is None:
            screen = await self._latest_screen()

        if allow_reuse:
            state = screen.classify()
            if state != S_MENU and self._reusable_prompt(screen):
                # The bot is ALREADY sitting on a usable number prompt: send
                # the number from here later - no main-menu restart, no Add
                # Account / Login with Number / Normal walk and no reroll (the
                # loop below still rerolls in place when the price is high).
                reused_prompt = True
                price = screen.upi_price
                self._log("[MEESHO-BOT] Bot is already at the number prompt "
                          f"(UPI Rs.{price if price is not None else 'n/a'}); reusing "
                          "it - no main-menu restart, no offer reroll.")
                self._note("reusing the number prompt the bot is already on")
                screen = await self._settle(
                    screen, (S_OFFER, S_OTP_WAIT, S_BLOCKED, S_LINKED),
                    accept_prompt=True, timeout=self.change_number_timeout,
                )
                state = screen.classify()
                if state in (S_OTP_WAIT, S_LINKED, S_BLOCKED):
                    self._log(f"[MEESHO-BOT] Bot moved to '{state}' while the "
                              f"number prompt was reused; not typing the number.")
                    info.update({
                        "upi": screen.upi_price,
                        "reused_prompt": True,
                        "message": screen.text,
                        "early_stage": "blocked" if state == S_BLOCKED else "otp_sent",
                    })
                    return screen, info
            elif state != S_MENU:
                screen = await self._cancel_to_menu()
        elif screen.classify() != S_MENU:
            screen = await self._cancel_to_menu()

        # Add Account (skipped when the number prompt above is reused).
        if screen.classify() != S_LINK_CHOICE and not self._at_number_prompt(screen):
            screen = await self._click(screen, "add account")
        if not self._at_number_prompt(screen):
            screen = await self._settle(screen, (S_LINK_CHOICE,))
            if screen.classify() != S_LINK_CHOICE:
                raise MeeshoBotUnknownScreen(
                    "Expected 'How would you like to link'",
                    screen.text, screen.button_labels,
                )

            # Login with Number -> referral screen -> login mode.
            screen = await self._click(screen, "login with numb")
            screen = await self._settle(screen, (S_LOGIN_MODE,))
            if screen.classify() != S_LOGIN_MODE:
                raise MeeshoBotUnknownScreen(
                    "Expected 'Choose login mode'",
                    screen.text, screen.button_labels,
                )

            # Normal mode
            self._log("[MEESHO-BOT] Tapping the 'Normal' login mode.")
            screen = await self._click(screen, "normal")

        # Offer screen + reroll until UPI price target is met. The reroll
        # button is normally "Try Another Offer"; some bot revisions show a
        # three-button "Try Again" variant instead, which is tapped the same
        # way. Variant screens that appear while waiting for an offer are
        # also tapped inside _settle_offer (bounded by the reroll budget).
        screen = await self._settle_offer(screen, budget)
        # A prompt with no readable price may only be accepted when the offer
        # was already agreed (a reused prompt) and cannot be rerolled - a
        # fresh flow keeps insisting on a price it can compare with the target.
        price_optional = bool(price_optional or reused_prompt)
        while True:
            self._progress()
            self._note(f"rolling offers for a UPI price <= Rs.{self.target_upi_price} "
                       f"({rerolls}/{budget} rerolls used)")
            state = screen.classify()
            if state in (S_BLOCKED, S_LINKED, S_OTP_WAIT, S_WRONG_OTP, S_EXPIRED):
                break
            if state == S_WORKING:
                # The preparing screen outlived every wait round: the next
                # offer never appeared (a throttled bot after many rerolls).
                first_line = (screen.text or "").strip().splitlines()
                raise MeeshoBotUnknownScreen(
                    f"The bot stayed on its preparing screen "
                    f"({first_line[0][:60] if first_line else 'working'}) through "
                    f"{self.working_screen_waits + 1} wait round(s) after reroll "
                    f"#{rerolls} - the next offer never appeared",
                    screen.text, screen.button_labels,
                )
            upi = screen.upi_price
            info["upi"] = upi
            if self._at_number_prompt(screen):
                if upi is not None and upi <= self.target_upi_price:
                    break
                if upi is None and price_optional and screen.reroll_button() is None:
                    self._log("[MEESHO-BOT] Number prompt without a readable UPI "
                              "price and no reroll button; the offer already "
                              "stood, so it is accepted as it is.")
                    break
            if rerolls >= budget:
                raise MeeshoBotUnknownScreen(
                    f"UPI price never reached Rs.{self.target_upi_price} after {rerolls} rerolls",
                    screen.text, screen.button_labels,
                )
            reroll = screen.reroll_button()
            if reroll is None:
                raise MeeshoBotUnknownScreen(
                    "Offer screen has no reroll button (expected 'Try Another "
                    "Offer' or 'Try Again')",
                    screen.text, screen.button_labels,
                )
            self._log(f"[MEESHO-BOT] Reroll #{rerolls + 1} "
                      f"(UPI Rs.{upi if upi is not None else 'n/a'} vs target "
                      f"Rs.{self.target_upi_price}): tapping '{reroll[2]}'.")
            screen = await self._click(screen, reroll[2])
            screen = await self._settle_offer(screen, budget)
            rerolls += 1

        info["rerolls"] = rerolls
        info["upi"] = screen.upi_price
        info["reused_prompt"] = reused_prompt
        return screen, info

    async def _a_resolve_offer_to_target(self, screen, max_rerolls=None):
        """
        Verify the number prompt's UPI price is <= target_upi_price BEFORE a
        paid number is typed into it (a parked pre-warmed offer may have
        drifted, or the price line was unreadable, so the number would end up
        at a higher UPI price).

        - price readable and <= target: send is safe ("ok").
        - price readable and > target: reroll IN PLACE until it fits; if the
          budget is exhausted, raise UnknownScreen WITHOUT typing the number.
        - price unreadable:
            * with a reroll button: reroll until a price shows (same budget);
            * without any reroll button: cannot verify - accepted as-is (a
              genuine Change Number prompt's price was already agreed earlier).
        Returns (screen, info): info["upi"] / info["rerolls"] / info["accepted"].
        """
        budget = max_rerolls if max_rerolls is not None else self.max_offer_rerolls
        budget = max(0, int(budget))
        rerolls = 0
        info = {"upi": screen.upi_price, "rerolls": 0, "accepted": False}
        while True:
            self._progress()
            state = screen.classify()
            if state in (S_BLOCKED, S_LINKED, S_OTP_WAIT, S_WRONG_OTP, S_EXPIRED):
                info["accepted"] = True
                info["upi"] = screen.upi_price
                return screen, info
            upi = screen.upi_price
            info["upi"] = upi
            if not self._at_number_prompt(screen):
                raise MeeshoBotUnknownScreen(
                    "Expected the number prompt before verifying its UPI price",
                    screen.text, screen.button_labels,
                )
            if upi is not None and upi <= self.target_upi_price:
                info["accepted"] = True
                self._log("[MEESHO-BOT] Offer price verified: "
                          f"UPI ₹{upi} <= ₹{self.target_upi_price}.")
                return screen, info
            if upi is None and screen.reroll_button() is None:
                # No price to check and nothing to reroll: the offer stood
                # earlier in THIS login, so it is accepted as before.
                self._log("[MEESHO-BOT] Number prompt without a readable price "
                          "and no reroll button; price was agreed earlier in this "
                          "login, so it is accepted as-is.")
                info["accepted"] = True
                return screen, info
            if rerolls >= budget:
                raise MeeshoBotUnknownScreen(
                    f"Pre-warmed offer never settled at UPI ≤ ₹{self.target_upi_price} "
                    f"(last seen: ₹{upi if upi is not None else 'unreadable'}) after "
                    f"{rerolls} reroll(s) - the number was NOT typed",
                    screen.text, screen.button_labels,
                )
            reroll = screen.reroll_button()
            if reroll is None:
                raise MeeshoBotUnknownScreen(
                    f"Offer price ₹{upi} is above ₹{self.target_upi_price} and "
                    "has no reroll button - the number was NOT typed",
                    screen.text, screen.button_labels,
                )
            rerolls += 1
            info["rerolls"] = rerolls
            self._log(f"[MEESHO-BOT] Price check: UPI ₹{upi if upi is not None else 'unreadable'} "
                      f"> ₹{self.target_upi_price}; reroll #{rerolls} '{reroll[2]}'.")
            screen = await self._click(screen, reroll[2])
            screen = await self._settle_offer(screen, budget)

    async def _a_prepare_offer(self, reroll_budget=None):
        """
        Park the bot on an agreed offer WITHOUT sending a number (pre-warm).

        Returns {"stage": "offer", "upi": <price>, "rerolls": n} - the caller
        then only has to type the number into the prompt the bot is waiting on.
        """
        if reroll_budget is None:
            reroll_budget = self.warmup_max_offer_rerolls
        screen = await self._latest_screen()

        # Already parked on an offer that fits: nothing to do.
        if self._at_number_prompt(screen, require_marker=True):
            price = screen.upi_price
            if price is not None and price <= self.target_upi_price:
                self._log("[MEESHO-BOT] Bot is already parked on an agreed offer "
                          f"(UPI Rs.{price}); no pre-warm needed.")
                return {"stage": "offer", "upi": price, "rerolls": 0,
                        "reused_prompt": True}
            self._log("[MEESHO-BOT] Bot is on an offer priced "
                      f"Rs.{price if price is not None else 'n/a'} (target "
                      f"Rs.{self.target_upi_price}); re-rolling it.")

        screen, info = await self._a_reach_offer(
            screen, allow_reuse=True, price_optional=False,
            reroll_budget=reroll_budget,
        )
        if info.get("early_stage"):
            return {
                "stage": info["early_stage"],
                "upi": info.get("upi"),
                "rerolls": info.get("rerolls", 0),
                "reused_prompt": info.get("reused_prompt", False),
                "message": info.get("message", ""),
            }
        return {
            "stage": "offer",
            "upi": info.get("upi"),
            "rerolls": info.get("rerolls", 0),
            "reused_prompt": info.get("reused_prompt", False),
        }

    async def _a_prepare_login(self, number, continue_from_prompt=False):
        """
        Full navigation up to the number being submitted and the bot showing
        its "OTP on its way" screen. If continue_from_prompt is True, the bot
        is assumed to already sit on the offer/number prompt (after a previous
        Change Number), so only the number is sent.
        """
        rerolls = 0
        result = {"rerolls": 0, "upi": None}
        self._note("starting the login flow" if not continue_from_prompt
                   else "resuming at the number prompt")

        # Referral budget is per login: one link paste, plus a couple of
        # interruptions, then the bot's own skip option is used.
        self._referral_events = 0
        self._referral_pastes_in_flow = 0
        self._last_referral_problem = None
        self.last_referral_action = None

        screen = await self._latest_screen()

        if continue_from_prompt:
            # "Change Number" path: the caller believes the bot awaits a
            # number. Never type a paid 10-digit number into something else -
            # in particular the referral prompt, which would read it as a link
            # and burn the activation. Settle the screen first.
            screen = await self._settle(
                screen, (S_OFFER, S_OTP_WAIT, S_BLOCKED, S_LINKED), accept_prompt=True
            )
            state = screen.classify()
            if state in (S_OTP_WAIT, S_LINKED, S_BLOCKED):
                # The number was already submitted (retry after a timeout, or
                # the bot moved on): report it instead of sending a second one.
                self._log(f"[MEESHO-BOT] Bot already at '{state}' before the "
                          f"replacement number was sent; not typing it again.")
                result.update({
                    "upi": screen.upi_price,
                    "referral_action": self.last_referral_action,
                    "message": screen.text,
                    "stage": "blocked" if state == S_BLOCKED else "otp_sent",
                })
                return result
            if state == S_SENDING_OTP:
                raise MeeshoBotUnknownScreen(
                    "Bot is still on 'Sending your OTP…' for a previous "
                    "number - the replacement number was NOT typed",
                    screen.text, screen.button_labels,
                )
            if not self._at_number_prompt(screen):
                raise MeeshoBotUnknownScreen(
                    "Expected the number prompt before sending the replacement number",
                    screen.text, screen.button_labels,
                )
            # A parked (pre-warmed) prompt may no longer be at the agreed
            # price: NEVER type the number into an offer above the target -
            # reroll it in place until it fits (or fail without typing).
            screen, guard = await self._a_resolve_offer_to_target(screen)
            if screen.classify() in (S_OTP_WAIT, S_LINKED, S_BLOCKED):
                state = screen.classify()
                result.update({
                    "upi": guard.get("upi", screen.upi_price),
                    "message": screen.text,
                    "stage": "blocked" if state == S_BLOCKED else "otp_sent",
                })
                return result
            if guard.get("rerolls"):
                result["rerolls"] = guard["rerolls"]
            result["upi"] = guard.get("upi", screen.upi_price)
        else:
            # Walk to the offer (reusing a prompt the bot already sits on).
            screen, info = await self._a_reach_offer(screen)
            rerolls = info["rerolls"]
            result["rerolls"] = info["rerolls"]
            result["upi"] = info["upi"]
            result["reused_prompt"] = info["reused_prompt"]
            result["referral_action"] = self.last_referral_action
            if info.get("early_stage"):
                # The bot is already past the prompt (a previous number's OTP
                # screen, or a blocked/linked account): never type a number.
                result.update({
                    "message": info.get("message", ""),
                    "stage": info["early_stage"],
                })
                return result

        state = screen.classify()
        if state == S_BLOCKED:
            result["stage"] = "blocked"
            result["message"] = screen.text
            return result

        if not self._at_number_prompt(screen):
            raise MeeshoBotUnknownScreen(
                "Expected offer/number-prompt screen", screen.text, screen.button_labels
            )

        # Send the 10-digit number. The bot briefly shows "⏳ Sending your
        # OTP…" between the number and the "OTP on its way" screen: settle
        # through that transient state instead of erroring on it (the Change
        # Number recovery path already works this way).
        self._log(f"[MEESHO-BOT] Offer accepted at UPI ₹{result['upi']} "
                  f"({rerolls} reroll(s)); sending number {number}.")
        self._note(f"sending number {number} to the bot")
        screen = await self._send_text(str(number))
        self._note(f"number {number} was sent; waiting for the 'OTP on its way' screen")
        screen = await self._settle(
            screen, (S_OTP_WAIT, S_BLOCKED, S_LINKED, S_WRONG_OTP, S_EXPIRED),
        )
        state = screen.classify()
        if state == S_BLOCKED:
            result["stage"] = "blocked"
            result["message"] = screen.text
            return result
        if state == S_SENDING_OTP:
            raise MeeshoBotUnknownScreen(
                "Bot is stuck on 'Sending your OTP…' - the 'OTP on its way' "
                "screen never appeared. The number WAS submitted to the bot, "
                "so the SMS may still arrive (the refund tally will flag it "
                "if it does).",
                screen.text, screen.button_labels,
            )
        if state != S_OTP_WAIT:
            raise MeeshoBotUnknownScreen(
                f"Expected 'OTP on its way' after sending number, got {state}",
                screen.text, screen.button_labels,
            )

        result["stage"] = "otp_sent"
        result["message"] = screen.text
        return result

    async def _settle_offer(self, screen, extra_taps=0, timeout=None):
        """
        After a click that should lead to an offer, wait for the offer screen.
        Handles two interruptions:
          * the referral prompt the bot may re-offer mid-flow (via _settle);
          * the three-button "Try Again" variant some bot revisions show
            instead of an offer (no price on the screen): it waits for a tap,
            so it is tapped like "Try Another Offer" and waited on again -
            bounded by extra_taps so a stuck bot cannot spin the flow forever.
        `timeout` shortens the wait for callers that must not sit idle (the
        Change Number recovery, where a paid number's OTP window is running).
        Returns the settled screen; callers decide whether it is an error.
        """
        states = (S_OFFER, S_BLOCKED, S_LINKED, S_OTP_WAIT, S_LOGIN_MODE, S_LINK_CHOICE)
        taps = 0
        working = 0
        while True:
            state = screen.classify()
            if state == S_WORKING:
                # "⏳ Setting things up…": the next offer is being fetched. Wait
                # it out (bounded) instead of returning a screen the caller can
                # only misread as a dead end.
                if working >= self.working_screen_waits:
                    return screen
                working += 1
                first_line = (screen.text or "").strip().splitlines()
                self._log(f"[MEESHO-BOT] Bot is preparing the next offer "
                          f"({first_line[0][:60] if first_line else 'working'}); waiting "
                          f"it out (round {working}/{self.working_screen_waits}).")
                self._note(f"waiting for the offer behind the preparing screen "
                           f"(round {working}/{self.working_screen_waits})")
                screen = await self._settle(screen, states, timeout=timeout,
                                            accept_prompt=True)
                continue
            if (state not in states
                    and not self._at_number_prompt(screen)
                    and state != S_REFERRAL
                    and screen.reroll_button() is not None):
                # A non-offer screen that offers to reroll is the "Try Again"
                # variant: tapping it now is faster (and more correct) than
                # waiting out the full step timeout for a screen that will
                # not change until it is tapped.
                if taps >= extra_taps:
                    return screen
                label = screen.reroll_button()[2]
                self._log(f"[MEESHO-BOT] Offer variant without a price: "
                          f"tapping '{label}' (variant tap "
                          f"{taps + 1}/{extra_taps}).")
                screen = await self._click(screen, label)
                taps += 1
                continue
            screen = await self._settle(screen, states, timeout=timeout,
                                        accept_prompt=True)
            if screen.classify() in states or self._at_number_prompt(screen):
                return screen
            reroll = (None if screen.classify() == S_REFERRAL
                      else screen.reroll_button())
            if reroll is None or taps >= extra_taps:
                return screen  # let the caller decide what to do with it
            self._log(f"[MEESHO-BOT] Offer variant without a price: tapping "
                      f"'{reroll[2]}' (variant tap {taps + 1}/{extra_taps}).")
            screen = await self._click(screen, reroll[2])
            taps += 1

    async def _a_submit_otp(self, code):
        # The bot can re-ask for a referral link after the number was sent
        # (observed: it reposts the prompt). Answer it, but never type the code
        # into a referral field - if the bot is no longer waiting for the code,
        # that has to be reported, not papered over.
        self._note(f"checking the bot screen before submitting code {code}")
        screen = await self._latest_screen()
        if screen.classify() == S_REFERRAL and not screen.referral_acknowledged:
            self._log("[MEESHO-BOT] Referral screen on screen before code submission; "
                      "answering it first.")
            try:
                screen = await self._a_resolve_referral(screen)
            except MeeshoBotReferralError as exc:
                # "stop" mode: never guess. Report the referral failure with the
                # code context so the activation can be handled deliberately.
                raise MeeshoBotReferralError(
                    f"The referral step could not be completed while the OTP code "
                    f"was pending ({exc}); code {code} was NOT submitted",
                    exc.screen_text, exc.buttons,
                ) from exc
            if screen.classify() != S_OTP_WAIT:
                raise MeeshoBotUnknownScreen(
                    f"Bot is not waiting for the OTP code anymore "
                    f"(screen: {screen.classify()}) - the referral step interrupted "
                    f"the number flow; code {code} was NOT submitted",
                    screen.text, screen.button_labels,
                )

        self._note(f"submitting code {code} to the bot")
        screen = await self._send_text(str(code))
        self._note(f"code {code} was sent; waiting for the verification outcome")

        # The bot first shows a transient "🔎 Verifying your code…" screen (or
        # an edit of the prompt) before the real outcome appears: settle
        # through it to the linked / wrong / expired / blocked screen instead
        # of reporting the transient itself as an unknown result.
        try:
            screen = await self._settle(
                screen,
                (S_LINKED, S_WRONG_OTP, S_EXPIRED, S_BLOCKED, S_OTP_WAIT,
                 S_OFFER, S_MENU),
            )
        except MeeshoBotReferralError as exc:
            # The referral step interrupted the verification AFTER the code
            # was already sent: the outcome is unknown, and the alert must say
            # the code WAS submitted (the pre-send wording would be wrong).
            raise MeeshoBotReferralError(
                f"The referral step interrupted the code verification ({exc}); "
                f"code {code} WAS submitted - check the bot for the outcome",
                exc.screen_text, exc.buttons,
            ) from exc

        state = screen.classify()
        if state == S_VERIFYING:
            raise MeeshoBotUnknownScreen(
                "Bot is stuck on 'Verifying your code…' - the linked/error "
                "screen never appeared. The code WAS submitted, so the login "
                "may still complete; check the bot manually.",
                screen.text, screen.button_labels,
            )
        result = {"status": state, "screen": screen.text}
        if state == S_LINKED:
            result.update({
                "status": "linked",
                "user_id": screen.user_id,
                "account_number": screen.account_number,
                "number": screen.linked_number,
            })
        return result

    async def _a_change_number(self, new_number=None):
        """
        Tap Change Number from the OTP-wait screen and (optionally) submit the
        replacement number, returning once the bot shows OTP-on-its-way again.
        Without a number, returns once the number-prompt/offer screen is shown.

        Getting back to the number prompt is worth a bounded amount of effort:
        the alternative is a full flow from the main menu (Add Account -> Login
        with Number -> Normal -> offer rerolls), which is minutes slower and
        burns the paid number's OTP window. So

          * a prompt is recognised with looks_like_number_prompt() as well, not
            only through classify()'s S_OFFER heuristics (bot revisions word it
            differently - the old code gave up on those with "got unknown");
          * a tap the bot ignores (it is still on the OTP screen, or still
            working) is retried up to change_number_retries times;
          * the waits use the short change_number_timeout / overall
            change_number_budget instead of the full step timeout;
          * only when the bot really left the login flow (main menu / link
            choice / login mode) is "needs_full_flow" reported.
        """
        self._note("tapping Change Number")
        screen = await self._latest_screen()
        max_taps = max(1, self.change_number_retries + 1)
        max_waits = max(1, self.change_number_retries)
        taps = 0
        waits = 0
        deadline = time.time() + self.change_number_budget

        def remaining(default=None):
            """Time left in the recovery budget (never below a second)."""
            default = self.change_number_timeout if default is None else default
            return max(1.0, min(default, deadline - time.time()))

        def give_up(state, why):
            raise MeeshoBotUnknownScreen(
                f"Expected number prompt after Change Number, got {state} ({why}). "
                "If this screen IS the bot's number prompt, add its wording to "
                "meesho_bot.number_prompt_hints",
                screen.text, screen.button_labels,
            )

        while True:
            if self._at_number_prompt(screen):
                break  # the bot is asking for the number

            state = screen.classify()

            if state in (S_MENU, S_LINK_CHOICE, S_LOGIN_MODE):
                # The bot left the login flow by itself (its OTP prompt expired,
                # a manual /start, ...): the next number needs a full flow.
                self._log("[MEESHO-BOT] Change Number: the bot is back at the "
                          f"menu/login steps ('{state}'); a full flow is needed.")
                if new_number is None:
                    return {"stage": "needs_full_flow", "screen": screen.text}
                return await self._a_prepare_login(new_number)

            if state in (S_LINKED, S_BLOCKED):
                # The previous number already produced an outcome: report it
                # instead of tapping on - there is nothing to change.
                return {"stage": "blocked" if state == S_BLOCKED else "linked",
                        "message": screen.text, "screen": screen.text}

            if time.time() >= deadline:
                give_up(state, f"recovery budget of {self.change_number_budget:.0f}s spent")

            if screen.has_button("change number"):
                if taps >= max_taps:
                    give_up(state, f"Change Number tapped {taps} time(s)")
                taps += 1
                self._note(f"tapping Change Number (attempt {taps}/{max_taps})")
                if taps > 1:
                    self._log(f"[MEESHO-BOT] Change Number tap #{taps}/{max_taps}: "
                              f"the bot is still on '{state}' - tapping again.")
                screen = await self._click(screen, "change number")
                screen = await self._settle_offer(
                    screen, self.change_number_variant_taps, timeout=remaining(),
                )
                continue

            # Not the prompt, not the menu and no Change Number button: the bot
            # may still be working (a transient screen, a slow in-place edit).
            # Wait - briefly - and look again.
            if waits >= max_waits:
                give_up(state, "no Change Number button and the screen never settled")
            waits += 1
            self._note(f"waiting for the number prompt (round {waits}/{max_waits})")
            screen = await self._settle(
                screen,
                (S_OFFER, S_MENU, S_LINK_CHOICE, S_LOGIN_MODE, S_OTP_WAIT,
                 S_LINKED, S_BLOCKED),
                timeout=remaining(), accept_prompt=True,
            )

        if new_number is None:
            return {"stage": "prompt", "screen": screen.text, "upi": screen.upi_price,
                    "taps": taps}

        self._note(f"sending replacement number {new_number} to the bot")
        screen = await self._send_text(str(new_number))
        self._note(f"replacement number {new_number} was sent; waiting for the "
                   f"'OTP on its way' screen")
        # A re-offered referral prompt here must never swallow the replacement
        # number: answer it and wait for the OTP-wait screen instead (the
        # "⏳ Sending your OTP…" transient settles through as well).
        screen = await self._settle(
            screen, (S_OTP_WAIT, S_BLOCKED, S_LINKED, S_WRONG_OTP, S_EXPIRED),
        )
        if screen.classify() == S_BLOCKED:
            return {"stage": "blocked", "message": screen.text}
        if screen.classify() == S_SENDING_OTP:
            raise MeeshoBotUnknownScreen(
                "Bot is stuck on 'Sending your OTP…' after the replacement "
                "number - the 'OTP on its way' screen never appeared",
                screen.text, screen.button_labels,
            )
        if screen.classify() != S_OTP_WAIT:
            raise MeeshoBotUnknownScreen(
                "Expected OTP-on-its-way after new number",
                screen.text, screen.button_labels,
            )
        return {"stage": "otp_sent", "screen": screen.text, "upi": screen.upi_price}

    # -- bot number checker --------------------------------------------------

    def _resolve_bot_entity(self, username):
        """
        Cached entity for a conversation on this session, or None.

        Telethon's get_entity() is a COROUTINE and the userbot loop is already
        running in its own thread, so it can only be awaited from INSIDE that
        loop (`_a_bind_entity`). This used to call
        `self._loop.run_until_complete(...)`, which raises "This event loop is
        already running" for every caller that is not the loop thread - and the
        bare `except Exception` then returned the PRIMES bot's entity. A
        "dedicated" checker bot therefore typed its numbers into the LOGIN
        conversation (and leaked an un-awaited coroutine: the RuntimeWarning).

        There is no safe synchronous resolution here: return the cached entity
        when it is known, otherwise None - NEVER another bot's entity.
        """
        with self._entity_lock:
            entity = self._entities.get(normalize_username(username))
        if entity is not None:
            return entity
        if normalize_username(username) == normalize_username(
                getattr(self, "bot_username", "")):
            return getattr(self, "_bot_entity", None)
        return None

    async def _a_bind_entity(self, username):
        """
        Resolve `username` to a Telegram entity ON the userbot loop and cache
        it. This is the only place `get_entity()` may be called for another
        conversation: it is awaited, so nothing is left pending.
        """
        key = normalize_username(username)
        if not key:
            raise MeeshoBotError("Cannot open a conversation without a username")
        with self._entity_lock:
            cached = self._entities.get(key)
        if cached is not None:
            return cached
        entity = await self._client.get_entity(username)
        if entity is None:
            raise MeeshoBotError(
                f"Telegram has no conversation for '@{key}' on this account")
        with self._entity_lock:
            self._entities[key] = entity
        return entity

    # -- who is driving the shared PRIMES conversation -----------------------

    def hold_conversation(self, owner):
        """
        Mark the PRIMES (login) conversation as being driven by `owner`
        ("login" / "prewarm"). A number check that would navigate the same chat
        refuses to walk away from that screen - see conversation_conflict_reason.
        """
        with self._conversation_lock:
            self._conversation_holder = owner or None
            self._conversation_since = time.time() if self._conversation_holder else 0.0

    def release_conversation(self, owner=None):
        """Drop the PRIMES conversation lease (any owner if `owner` is None)."""
        with self._conversation_lock:
            if owner is None or self._conversation_holder == owner:
                self._conversation_holder = None
                self._conversation_since = 0.0

    @property
    def conversation_holder(self):
        """(owner, since) - who is driving the PRIMES conversation right now."""
        with self._conversation_lock:
            return self._conversation_holder, self._conversation_since

    def conversation_conflict_reason(self, owner):
        """
        "" or a readable reason why `owner` must NOT navigate the PRIMES
        conversation right now. Both the offer pre-warm and the number checker
        drive the SAME Telegram chat when no dedicated checker bot is in play:
        a check that starts while the pre-warm rerolls the offer walks the bot
        off that screen (the offer is gone, the pre-warm fails with "no reroll
        button") and the check itself reads a half-finished screen.
        """
        holder, since = self.conversation_holder
        if not holder or holder == owner:
            return ""
        age = max(0.0, time.time() - since) if since else 0.0
        held = f" (running for {age:.0f}s)" if age >= 1 else ""
        return (
            f"Refusing to run a number check: the PRIMES bot conversation is "
            f"being driven by the {holder} flow{held}. A number check and the "
            f"{holder} drive the SAME chat, so the check would walk the bot "
            f"away from that screen - nothing was sent and the number is left "
            f"untouched. Use a dedicated checker bot "
            f"(checker.telegram_bot.username) so checks never share the login "
            f"conversation."
        )

    def _conversation_label(self):
        """Where this client's taps/text go - for logs (the PRIMES login chat)."""
        name = normalize_username(getattr(self, "bot_username", ""))
        return f"@{name} - the PRIMES login bot" if name else ""

    def in_checker_screen(self):
        """
        Read-only: True when the PRIMES bot is currently showing its number
        checker (prompt / "checking..." / result). Used to tell a pre-warm that
        lost its offer screen apart from one that was hijacked by a check.
        """
        with self._flow_lock:
            screen = self._run(self._latest_screen())
        return self._looks_like_checker_screen(screen)

    def _looks_like_checker_screen(self, screen):
        """
        True only for a screen that really belongs to the bot CHECKER.

        classify_check() is deliberately loose - it has to accept whatever the
        configured checker bot shows - so it also accepts two LOGIN screens:

          * the login number prompt ("✏️ Change Number - Send the 10-digit
            mobile number you'd like to use instead.", lone Cancel button),
            matched by its "10-digit ... + navigation button" rule;
          * the bot's own "⏳ Fetching your offer… / setting things up" copy,
            matched by the generic "please wait / fetching / processing"
            checking hints.

        Reporting either as "a number check is using this conversation" while
        the offer pre-warm walks the login flow produced the bogus "the PRIMES
        bot is on its number checker screen - configure a dedicated checker
        bot" warning (with a dedicated checker bot configured and the API
        answering the check). Neither is a checker screen: the login prompt is
        excluded through the cold-read test, and "the bot is working on the
        next offer" copy carries no check/verify/registration wording at all.
        """
        if self._at_number_prompt(screen, require_marker=True):
            return False
        text = normalize_label(screen.text)
        if (_hint_match(text, WORKING_HINTS)
                and not _hint_match(text, _CHECKER_WORD_HINTS)):
            return False
        return screen.classify_check((), (), ()) in (
            S_CHECK_PROMPT, S_CHECKING, S_CHECK_RESULT)

    def _checker_settings(self, overrides=None):
        """Resolve checker.bot config (defaults + overrides) into a plain dict."""
        conf = dict(self.checker_conf)
        conf.update(overrides or {})
        entry = str(conf.get("entry", "auto") or "auto").strip().lower()
        if entry not in ("auto", "button", "command"):
            entry = "auto"
        try:
            step_timeout = float(conf.get("step_timeout_seconds", self.step_timeout))
        except (TypeError, ValueError):
            step_timeout = self.step_timeout
        step_timeout = step_timeout if step_timeout > 0 else self.step_timeout
        try:
            attempts = max(1, int(conf.get("max_attempts", 2)))
        except (TypeError, ValueError):
            attempts = 2
        # For dedicated checker bots (telegram_bot) continuous checking is
        # preferred: after a result you can directly send another number
        # without tapping Start. reset_after_check=False enables that, but
        # we also keep the screen so next check reuses prompt/result.
        # `continuous` / `reuse_checker` / `direct_next` are aliases.
        continuous = conf.get("continuous")
        if continuous is None:
            continuous = conf.get("reuse_checker")
        if continuous is None:
            continuous = conf.get("direct_next")
        if continuous is None:
            # If reset_after_check is explicitly False, that's continuous.
            # Otherwise default True for PRIMES, False for dedicated bots
            # is decided by caller; here we keep the raw flag but also expose
            # continuous for logic.
            continuous = not bool(conf.get("reset_after_check", True))
        else:
            continuous = bool(continuous)

        return {
            "entry": entry,
            "command": str(conf.get("command") or "").strip(),
            "button_hints": tuple(conf.get("button_hints") or ()),
            "button_fallbacks": tuple(conf.get("button_fallback_hints")
                                      or DEFAULT_CHECK_BUTTON_FALLBACK_HINTS),
            "button_exclude": tuple(conf.get("button_exclude")
                                    or DEFAULT_CHECK_BUTTON_EXCLUDE),
            "prompt_hints": tuple(conf.get("number_prompt_hints") or ()),
            "registered_hints": tuple(conf.get("registered_hints") or ()),
            "not_registered_hints": tuple(conf.get("not_registered_hints") or ()),
            "check_another_hints": tuple(conf.get("check_another_hints")
                                         or conf.get("another_button_hints") or ()),
            "step_timeout": step_timeout,
            "attempts": attempts,
            "reset_after_check": bool(conf.get("reset_after_check", True)),
            "continuous": bool(continuous),
            "poll_interval": float(getattr(self, "poll_interval", 2.0) or 2.0),
        }

    @staticmethod
    def _ten_digit(number):
        digits = re.sub(r"\D", "", str(number or ""))
        if len(digits) == 12 and digits.startswith("91"):
            return digits[2:]
        if len(digits) == 11 and digits.startswith("0"):
            return digits[1:]
        return digits[-10:] if len(digits) >= 10 else digits

    # Screens that belong to a login that is already in flight. Leaving one of
    # these (to walk to the checker) destroys the OTP screen of a paid number.
    LOGIN_IN_FLIGHT_STATES = (
        S_OTP_WAIT, S_SENDING_OTP, S_VERIFYING, S_WRONG_OTP, S_EXPIRED, S_LINKED,
    )

    def _login_in_progress(self, screen):
        """True when the bot is mid-login and must not be navigated away."""
        # Checker screens must never be treated as a login in progress:
        # their wording ("already registered") overlaps with S_BLOCKED.
        # For continuous checking we are at S_CHECK_RESULT and want to send
        # the next number directly - that must not be refused.
        try:
            # If checker hints are available, use them to exclude checker.
            # Fallback to generic detection: check_verdict or prompt/checking.
            if screen.check_verdict() is not None or screen.looks_like_check_prompt() or screen.looks_like_checking():
                return False
        except Exception:
            pass
        state = screen.classify()
        if state in self.LOGIN_IN_FLIGHT_STATES:
            return True
        return state == S_BLOCKED

    def _check_state(self, screen, conf):
        return screen.classify_check(
            conf["registered_hints"], conf["not_registered_hints"], conf["prompt_hints"]
        )

    def _check_verdict(self, screen, conf):
        return screen.check_verdict(conf["registered_hints"], conf["not_registered_hints"])

    async def _settle_check(self, screen, conf, want_result):
        """
        Poll until the checker has something to show: the number prompt
        (want_result=False) or a readable result (want_result=True).

        Returns the last screen; the caller raises if it is not usable. Login
        screens short-circuit so a drifted flow fails fast with the screen text
        instead of burning the whole step timeout.
        """
        deadline = time.time() + conf["step_timeout"]
        while True:
            state = self._check_state(screen, conf)
            if state == S_CHECK_RESULT:
                return screen
            if not want_result and state == S_CHECK_PROMPT:
                return screen
            if state == S_CHECKING:
                self._note("bot checker is working (transient screen)")
            elif screen.classify() in (S_MENU, S_LINK_CHOICE, S_LOGIN_MODE,
                                       S_OFFER, S_REFERRAL, S_OTP_WAIT, S_LINKED,
                                       S_BLOCKED, S_WRONG_OTP, S_EXPIRED):
                # The bot left the checker (or never entered it): report now.
                return screen
            if time.time() >= deadline:
                return screen
            await asyncio.sleep(self.poll_interval)
            self._progress()
            screen = await self._latest_screen()

    def _conversation_proxy_class(self):
        """The proxy class for this client's type, built once and cached."""
        cls = self.__dict__.get("_conversation_proxy_cls")
        if cls is None:
            cls = _make_conversation_proxy_class(type(self))
            self.__dict__["_conversation_proxy_cls"] = cls
        return cls

    def _conversation_proxy(self, username):
        """
        A client that shares EVERYTHING with this one (Telethon session,
        config, flow locks, watchdog state, referral bookkeeping) except the
        conversation pointer `_bot_entity`, which is pinned to the second bot.
        A checker conversation then runs against that bot without ever
        touching an in-flight login conversation.

        It is a real subclass, not a thin `__getattr__` wrapper: with a
        wrapper, `proxy._a_check_registration()` resolved to the BASE client's
        bound method, so the coroutine ran with `self` = the login client and
        read `self._bot_entity` from there - the check was typed into the
        PRIMES conversation no matter what the proxy pinned.

        The entity is NOT resolved here: get_entity() is a coroutine and this
        runs on a coordinator/worker thread, not on the userbot loop. Call
        `proxy._a_bind()` inside the loop first (CheckerBotClient does) - an
        unbound proxy refuses to check instead of silently falling back to the
        PRIMES conversation.
        """
        cls = self._conversation_proxy_class()
        return cls(self, self._resolve_bot_entity(username), username)


    async def _a_check_registration(self, number, overrides=None, _bot_username=None):
        """
        Ask the bot whether `number` is registered on Meesho.

        `self` may be a conversation proxy bound to a DEDICATED checker bot
        (see _conversation_proxy / CheckerBotClient); the check then runs in
        that second conversation and leaves the PRIMES login chat untouched.

        Returns {"success": True, "is_registered": bool, "source": "bot", ...}
        - the same shape the API checker returns, so callers can treat both
        the same way. Raises MeeshoBotError subclasses when no verdict can be
        read; the caller (checker_router) turns those into CheckerUnavailable.

        Dedicated checker bot improvement: after every number check there is
        no need for tapping Start - the next number can be given directly.
        If the bot is already at its check prompt or result screen, the
        number is sent straight away without navigating via Main Menu /
        Check Number. A "Check Another" button is used as fallback when
        direct send is not accepted.
        """
        # A check that drives the SHARED login conversation must not run while
        # the pre-warm / a login owns it: both would tap in the same chat.
        conflict = self.conversation_conflict_reason("check")
        if conflict:
            raise MeeshoBotError(conflict)
        conf = self._checker_settings(overrides)
        digits = self._ten_digit(number)
        if len(digits) != 10:
            raise MeeshoBotError(f"Not a usable 10-digit number: {number!r}")

        where = self._conversation_label()
        self._note(f"checking {digits} with the bot checker"
                   + (f" ({where})" if where else ""))
        self._log(f"[MEESHO-BOT] Bot checker: checking {digits} "
                  f"(entry: {conf['entry']}){(' in ' + where) if where else ''}.")

        screen = await self._latest_screen()
        initial_state = self._check_state(screen, conf)
        already_in_checker = False
        button = None
        command_sent = False

        # ---- Continuous / reuse path ------------------------------------
        # If the bot is already sitting on its checker prompt, result or
        # transient checking screen, reuse it directly - no need to tap
        # Start / Main Menu / Check Number again. This is the requested
        # improvement for the dedicated checker bot.
        if initial_state in (S_CHECK_PROMPT, S_CHECK_RESULT, S_CHECKING):
            if initial_state == S_CHECKING:
                self._log("[MEESHO-BOT] Bot checker: already checking - waiting for its result before next number.")
                screen = await self._settle_check(screen, conf, want_result=True)
                initial_state = self._check_state(screen, conf)

            if initial_state in (S_CHECK_PROMPT, S_CHECK_RESULT):
                already_in_checker = True
                self._log(f"[MEESHO-BOT] Bot checker: already at {initial_state} - sending {digits} directly, no Start tap.")
                # For result screens that don't accept direct numbers, we will
                # try tapping "Check Another" later if direct send fails.
            else:
                # Settled but not at prompt/result (e.g. drifted) - need fresh start
                already_in_checker = False

        # ---- Normal entry path (not already in checker) ------------------
        if not already_in_checker:
            if conf["entry"] in ("auto", "button"):
                if screen.classify() != S_MENU:
                    # A number check must never walk the bot out of a login flow:
                    # that would throw away the OTP screen of a PAID number (its
                    # OTP could then not even be entered by hand) just to ask
                    # whether some other number is registered. The coordinator
                    # normally prevents this (a login claims the bot), so getting
                    # here means the flow drifted - fail the check, not the login.
                    if self._login_in_progress(screen):
                        raise MeeshoBotError(
                            "Refusing to run a number check: the bot is in the "
                            f"middle of a login flow ({screen.classify()}). Cancel "
                            "the check instead of losing the number that is "
                            "waiting for its OTP."
                        )
                    # Never type a number into a leftover login/OTP screen.
                    screen = await self._cancel_to_menu()
                button = screen.checker_button(
                    conf["button_hints"], conf["button_fallbacks"], conf["button_exclude"]
                )

            if button is not None:
                self._log(f"[MEESHO-BOT] Bot checker: tapping '{button[2]}'.")
                screen = await self._click(screen, button[2])
            elif conf["command"] and conf["entry"] in ("auto", "command"):
                # If we are already in checker but entry is command-only,
                # still send the command only when not already at prompt/result
                text = conf["command"].format(number=digits)
                self._log(f"[MEESHO-BOT] Bot checker: sending command '{text}'.")
                screen = await self._send_text(text, timeout=conf["step_timeout"])
                command_sent = True
            elif already_in_checker:
                # Already at prompt/result, no button/command needed - will send number directly
                pass
            else:
                raise MeeshoBotUnknownScreen(
                    "No bot-checker menu button found and checker.bot.command is "
                    "empty. Add the checker button's exact label to "
                    "checker.bot.button_hints (or set checker.bot.command, e.g. "
                    "'/check {number}') - see SETUP_CHECKER.md.",
                    screen.text, screen.button_labels,
                )

            screen = await self._settle_check(screen, conf, want_result=False)

        # When we are reusing a RESULT screen for the NEXT number (continuous
        # mode), the verdict on that screen belongs to the PREVIOUS number and
        # must not be returned for the new one. Force a fresh send.
        if already_in_checker and initial_state == S_CHECK_RESULT:
            verdict = None
        else:
            verdict = self._check_verdict(screen, conf)

        def no_result_error():
            if self._check_state(screen, conf) == S_CHECKING:
                return MeeshoBotUnknownScreen(
                    f"The bot stayed on its 'checking...' screen for "
                    f"{conf['step_timeout']:.0f}s without answering - the check did "
                    f"not complete",
                    screen.text, screen.button_labels,
                )
            return MeeshoBotUnknownScreen(
                "The bot checker gave no readable result for this number. Add "
                "its result wording to checker.bot.registered_hints / "
                "checker.bot.not_registered_hints - see SETUP_CHECKER.md.",
                screen.text, screen.button_labels,
            )

        # If verdict already matches current digits (e.g. bot already answered
        # same number), we can return it; otherwise we must send.
        # For continuous reuse we always send because the screen is old.
        if verdict is not None and not already_in_checker:
            # Fresh check that already produced verdict (e.g. command included number)
            pass
        elif verdict is None or (already_in_checker and initial_state == S_CHECK_RESULT):
            # Need to send number (or next number directly from result)
            pass
        # unify: if verdict is None -> enter sending loop, else skip
        if verdict is None:
            # If we are at prompt, send the number. If we are at result,
            # dedicated checker bots allow directly sending another number
            # without tapping Start / Check Another - try that first.
            state = self._check_state(screen, conf)
            if state == S_CHECKING:
                raise no_result_error()

            # For result screen: try direct send first (continuous mode)
            if state == S_CHECK_RESULT and already_in_checker:
                self._log(f"[MEESHO-BOT] Bot checker: at result, trying direct send of {digits} without tapping Start.")
                # Fall through to attempt loop which will send directly

            if state not in (S_CHECK_PROMPT, S_CHECK_RESULT):
                if command_sent:
                    raise MeeshoBotUnknownScreen(
                        f"The bot did not answer the checker command "
                        f"'{conf['command']}' with a readable result. Add its "
                        f"result wording to checker.bot.registered_hints / "
                        f"checker.bot.not_registered_hints - see SETUP_CHECKER.md.",
                        screen.text, screen.button_labels,
                    )
                raise MeeshoBotUnknownScreen(
                    "Expected the bot to ask for the number to check, got an "
                    "unreadable screen. Add its wording to "
                    "checker.bot.number_prompt_hints - see SETUP_CHECKER.md.",
                    screen.text, screen.button_labels,
                )

            # Attempt to get verdict by sending number (direct next number)
            for attempt in range(conf["attempts"]):
                if verdict is not None:
                    break
                if self._login_in_progress(screen):
                    raise MeeshoBotError(
                        "Refusing to send a number to check: the bot is in the "
                        f"middle of a login flow ({screen.classify()})."
                    )

                # If we are still at result and direct send hasn't worked,
                # try tapping "Check Another" as fallback before sending again
                current_state = self._check_state(screen, conf)
                if current_state == S_CHECK_RESULT and attempt > 0:
                    another = screen.check_another_button(conf.get("check_another_hints", ()))
                    if another is not None:
                        self._log(f"[MEESHO-BOT] Bot checker: direct send didn't yield result, tapping '{another[2]}' to get fresh prompt.")
                        try:
                            screen = await self._click(screen, another[2])
                            screen = await self._settle_check(screen, conf, want_result=False)
                        except MeeshoBotError as exc:
                            self._log(f"[MEESHO-BOT] Bot checker: could not tap '{another[2]}' ({exc}), trying direct send anyway.")

                self._note(f"sending {digits} to the bot checker")
                self._log(f"[MEESHO-BOT] Bot checker: sending number {digits}"
                          + ("" if attempt == 0 else f" (attempt {attempt + 1})") + ".")
                screen = await self._send_text(str(digits), timeout=conf["step_timeout"])
                screen = await self._settle_check(screen, conf, want_result=True)
                verdict = self._check_verdict(screen, conf)
                if verdict is None and attempt + 1 < conf["attempts"]:
                    if self._check_state(screen, conf) == S_CHECK_PROMPT:
                        continue  # the bot asked again: send it once more
                    break

        if verdict is None:
            raise no_result_error()

        result = {
            "success": True,
            "is_registered": bool(verdict),
            "source": "bot",
            "number": digits,
            "message": screen.text,
            "stage": "checked",
        }
        self._log(f"[MEESHO-BOT] Bot checker: {digits} -> "
                  f"{'REGISTERED' if verdict else 'NOT registered'}.")

        # Continuous mode: stay at result/prompt so next number can be sent directly
        # without tapping Start. Only reset when explicitly configured.
        if conf["reset_after_check"]:
            # If continuous is also True, we keep the result screen for direct next
            # number but still note that a reset would be needed for PRIMES.
            # For dedicated checker bots, reset_after_check should be False.
            if conf.get("continuous"):
                self._log("[MEESHO-BOT] Bot checker: continuous mode - staying at result, next number will be sent directly (no Start tap).")
            else:
                try:
                    await self._cancel_to_menu()
                    self._note("bot checker: back at the main menu")
                except MeeshoBotError as exc:
                    self._log(f"[MEESHO-BOT] Bot checker: could not return to the "
                              f"main menu after the check ({exc}).")
        else:
            self._log("[MEESHO-BOT] Bot checker: staying at result/prompt - next check will send directly without Start.")
            self._note("bot checker: staying at checker for direct next number")

        return result

    async def _a_check_registration_many(self, numbers, overrides=None):
        """
        Check several numbers in one visit to the checker bot by sending them
        comma-separated (the dedicated bot answers with one verdict line per
        number). Returns {"success": True, "verdicts": {digits: bool}, ...} -
        verdicts are keyed BY NUMBER because the bot's reply is not ordered.
        Raises MeeshoBotError subclasses when no/all verdicts can be read.

        Dedicated checker bot improvement: if the bot is already at its check
        prompt/result screen, the batch payload is sent directly without
        tapping Start / Main Menu again.
        """
        # Same guard as the single check: never navigate the shared PRIMES
        # chat while the pre-warm / a login is driving it.
        conflict = self.conversation_conflict_reason("check")
        if conflict:
            raise MeeshoBotError(conflict)
        conf = self._checker_settings(overrides)
        targets = []
        seen = set()
        for raw in numbers:
            digits = self._ten_digit(raw)
            if len(digits) == 10 and digits not in seen:
                seen.add(digits)
                targets.append(digits)
        if not targets:
            raise MeeshoBotError("check many: no usable 10-digit numbers given")

        where = self._conversation_label()
        self._note(f"checking {len(targets)} numbers with the bot checker")
        self._log(f"[MEESHO-BOT] Bot checker: batch of {len(targets)} "
                  f"(entry: {conf['entry']}){(' in ' + where) if where else ''}.")

        screen = await self._latest_screen()
        initial_state = self._check_state(screen, conf)
        already_in_checker = False
        button = None

        # ---- Continuous / reuse path ------------------------------------
        if initial_state in (S_CHECK_PROMPT, S_CHECK_RESULT, S_CHECKING):
            if initial_state == S_CHECKING:
                self._log("[MEESHO-BOT] Bot checker (batch): already checking - waiting for its result before next batch.")
                screen = await self._settle_check(screen, conf, want_result=True)
                initial_state = self._check_state(screen, conf)
            if initial_state in (S_CHECK_PROMPT, S_CHECK_RESULT):
                already_in_checker = True
                self._log(f"[MEESHO-BOT] Bot checker (batch): already at {initial_state} - sending {len(targets)} numbers directly, no Start tap.")
            else:
                already_in_checker = False

        if not already_in_checker:
            if conf["entry"] in ("auto", "button"):
                if screen.classify() != S_MENU:
                    if self._login_in_progress(screen):
                        raise MeeshoBotError(
                            "Refusing to run a batch check: the bot is in the "
                            f"middle of a login flow ({screen.classify()})."
                        )
                    screen = await self._cancel_to_menu()
                button = screen.checker_button(
                    conf["button_hints"], conf["button_fallbacks"], conf["button_exclude"]
                )

            if button is not None:
                self._log(f"[MEESHO-BOT] Bot checker: tapping '{button[2]}'.")
                screen = await self._click(screen, button[2])
            elif conf["command"] and conf["entry"] in ("auto", "command"):
                text = conf["command"].format(number=targets[0])
                self._log(f"[MEESHO-BOT] Bot checker: sending command '{text}'.")
                screen = await self._send_text(text, timeout=conf["step_timeout"])
            else:
                raise MeeshoBotUnknownScreen(
                    "No bot-checker menu button found (batch check). See "
                    "checker.bot.button_hints in SETUP_CHECKER.md.",
                    screen.text, screen.button_labels,
                )

            screen = await self._settle_check(screen, conf, want_result=False)

        state = self._check_state(screen, conf)

        # If we were at result and want to send a new batch directly, try that.
        # For bots that require "Check Another" tap, fallback if direct send fails.
        if state == S_CHECK_RESULT and already_in_checker:
            self._log(f"[MEESHO-BOT] Bot checker (batch): at result, trying direct send of {len(targets)} numbers without Start.")
            # We'll attempt direct send; if it doesn't settle to prompt, we'll
            # tap Check Another below.
        elif state != S_CHECK_PROMPT and state != S_CHECK_RESULT:
            raise MeeshoBotUnknownScreen(
                "Expected the bot to ask for the numbers to check, got an "
                "unreadable screen (batch check). Add its wording to "
                "checker.bot.number_prompt_hints - see SETUP_CHECKER.md.",
                screen.text, screen.button_labels,
            )

        # If we are at result and the bot needs an explicit "Check Another",
        # try direct send first, then fallback to button tap on retry.
        if state == S_CHECK_RESULT:
            # Attempt direct send; _send_text will produce new screen
            pass
        else:
            # Normal prompt path
            if self._login_in_progress(screen):
                raise MeeshoBotError(
                    "Refusing to send numbers to check: the bot is in the middle "
                    f"of a login flow ({screen.classify()})."
                )

        payload = ", ".join(targets)
        self._note(f"sending {len(targets)} numbers to the bot checker")
        self._log(f"[MEESHO-BOT] Bot checker: sending {payload}.")
        screen = await self._send_text(payload, timeout=conf["step_timeout"])
        screen = await self._settle_check(screen, conf, want_result=True)

        # If direct send from result didn't produce verdict, try Check Another
        if self._check_state(screen, conf) != S_CHECK_RESULT:
            # Check if we got stuck still at result/prompt without verdicts
            # Try tapping Check Another if available
            maybe_verdicts = screen.parse_check_verdicts(
                conf["registered_hints"], conf["not_registered_hints"]
            )
            if not maybe_verdicts and already_in_checker:
                another = screen.check_another_button(conf.get("check_another_hints", ()))
                if another is not None:
                    self._log(f"[MEESHO-BOT] Bot checker (batch): direct send didn't yield result, tapping '{another[2]}' then resending.")
                    try:
                        screen = await self._click(screen, another[2])
                        screen = await self._settle_check(screen, conf, want_result=False)
                        screen = await self._send_text(payload, timeout=conf["step_timeout"])
                        screen = await self._settle_check(screen, conf, want_result=True)
                    except MeeshoBotError as exc:
                        self._log(f"[MEESHO-BOT] Bot checker (batch): could not tap '{another[2]}' ({exc}).")

        # Collect per-number verdicts. The reply order is NOT the input order,
        # so verdicts come out of parse_check_verdicts keyed by number, and we
        # keep polling (merging new screens) until every number has a verdict.
        deadline = time.monotonic() + max(conf["step_timeout"], 8.0)
        verdicts = {}
        screen_budget = 0
        while True:
            found = screen.parse_check_verdicts(
                conf["registered_hints"], conf["not_registered_hints"],
            )
            for digits, verdict in found.items():
                if digits in targets and digits not in verdicts:
                    verdicts[digits] = verdict
            missing = [d for d in targets if d not in verdicts]
            if not missing:
                break
            if time.monotonic() >= deadline or screen_budget >= 6:
                raise MeeshoBotUnknownScreen(
                    f"The bot's batch result is missing verdicts for "
                    f"{', '.join(missing)} (read {len(verdicts)}/{len(targets)}). "
                    "Add the bot's result wording to checker.bot."
                    "registered_hints / not_registered_hints - see SETUP_CHECKER.md.",
                    screen.text, screen.button_labels,
                )
            screen_budget += 1
            await asyncio.sleep(conf["poll_interval"])
            screen = await self._latest_screen()

        self._log("[MEESHO-BOT] Bot checker (batch): "
                  + ", ".join(f"{d}={'REG' if v else 'NEW'}"
                              for d, v in verdicts.items())
                  + ".")

        if conf["reset_after_check"]:
            if conf.get("continuous"):
                self._log("[MEESHO-BOT] Bot checker (batch): continuous mode - staying at result for direct next batch.")
            else:
                try:
                    await self._cancel_to_menu()
                    self._note("bot checker: back at the main menu")
                except MeeshoBotError as exc:
                    self._log(f"[MEESHO-BOT] Bot checker: could not return to the "
                              f"main menu after the batch check ({exc}).")
        else:
            self._log("[MEESHO-BOT] Bot checker (batch): staying at result/prompt - next batch will send directly without Start.")
            self._note("bot checker: staying at checker for direct next batch")

        return {
            "success": True,
            "verdicts": {d: bool(v) for d, v in verdicts.items()},
            "source": "bot",
            "message": screen.text,
            "stage": "checked",
        }

    async def _a_return_to_menu(self):
        screen = await self._latest_screen()
        if screen.classify() == S_REFERRAL and not screen.referral_acknowledged:
            screen = await self._a_resolve_referral(screen)
        if screen.has_button("main menu"):
            screen = await self._click(screen, "main menu")
        if screen.classify() != S_MENU:
            await self._cancel_to_menu()
        return {"stage": "menu"}

    async def _a_cancel_flow(self):
        await self._cancel_to_menu()
        return {"stage": "menu"}

    # -- synchronous wrappers (called from coordinator threads) -------------
    #
    # Every wrapper takes _flow_lock: there is exactly one conversation with
    # the bot, so a worker's number check can never interleave with the
    # coordinator's login flow (the second caller simply waits its turn).

    def screen_state(self):
        """
        Classify the bot's CURRENT screen without tapping or typing anything
        (read-only). Used by the coordinator before it commits to anything
        destructive - e.g. a late OTP salvaged during a cancellation race is
        only auto-submitted when the bot is genuinely still waiting for the
        code, and Change Number is only tapped once the provider cancellation
        is settled.
        """
        with self._flow_lock:
            return self._run(self._latest_screen()).classify()

    def at_number_prompt(self):
        """
        Read-only: True when the bot currently sits on the LOGIN number prompt
        (the offer screen), including a revision copy classify() reports as
        "unknown". The offer marker is required here - a cold read cannot tell
        from context whether the bot is in the login flow, and the bot CHECKER's
        "send the number to check" prompt must never be mistaken for it.

        Lets the coordinator keep a flow it deliberately left in place (checker
        API answering, so no main-menu reset was needed) instead of paying for a
        full restart.
        """
        with self._flow_lock:
            screen = self._run(self._latest_screen())
        return self._at_number_prompt(screen, require_marker=True)

    def check_registration(self, number, overrides=None):
        """
        Ask the bot's own checker whether `number` is registered on Meesho.

        Returns the API checker's dict shape ({"success": True,
        "is_registered": bool, "source": "bot", ...}) or raises a
        MeeshoBotError subclass when no verdict could be read.
        """
        with self._flow_lock:
            return self._run(self._a_check_registration(number, overrides))

    def prepare_login(self, number):
        with self._flow_lock:
            return self._run(self._a_prepare_login(number, continue_from_prompt=False))

    def prepare_offer(self, reroll_budget=None):
        """
        Park the bot on an agreed offer (UPI <= target_upi_price) BEFORE a
        number exists, so the number that is found next only has to be typed
        into the prompt the bot is already waiting on.

        Returns {"stage": "offer", "upi": <price>, "rerolls": n} - or
        {"stage": "otp_sent"/"blocked", ...} when the bot is already past the
        prompt (nothing may be typed anymore).
        """
        with self._flow_lock:
            return self._run(self._a_prepare_offer(reroll_budget=reroll_budget))

    def continue_with_number(self, number):
        with self._flow_lock:
            return self._run(self._a_prepare_login(number, continue_from_prompt=True))

    def submit_otp(self, code):
        with self._flow_lock:
            return self._run(self._a_submit_otp(code))

    def change_number(self, new_number=None):
        with self._flow_lock:
            return self._run(self._a_change_number(new_number))

    def return_to_menu(self):
        with self._flow_lock:
            return self._run(self._a_return_to_menu())

    def cancel_flow(self):
        with self._flow_lock:
            return self._run(self._a_cancel_flow())


class CheckerBotClient:
    """
    A dedicated checker bot driven through the SAME logged-in Telegram account
    (api_id/api_hash + userbot.session.txt) as the PRIMES login userbot - but a
    SECOND conversation, so a number check never walks the login bot out of a
    waiting OTP screen.

    The only thing that differs from MeeshoBotClient is the conversation:
    every call goes to the checker bot's own username, and the check-entry
    hints come from config["checker"]["telegram_bot"] (button / command /
    prompt / verdict wording), which need not match the PRIMES bot's copy.
    """

    def __init__(self, base_client, username, conf=None, log_fn=None):
        self._base = base_client
        self._log_fn = log_fn or base_client._log
        import threading as _threading
        # Own lock: a checker conversation runs independently of the login
        # conversation, so it never waits behind a login flow.
        self._checker_lock = _threading.RLock()
        username = (username or "").strip()
        if not username.startswith("@"):
            username = "@" + username
        self.bot_username = username
        # Dedicated checker bot defaults to continuous checking: after every
        # number check there is no need for tapping Start - next number can be
        # given directly. reset_after_check=False enables that.
        base_conf = dict(conf or {})
        base_conf.setdefault("reset_after_check", False)
        base_conf.setdefault("continuous", True)
        base_conf.setdefault("reuse_checker", True)
        self.conf = base_conf

    # -- pass-through to the shared session --------------------------------

    @property
    def ready(self):
        base = self._base
        return bool(base.ready and base.enabled and base.api_id and base.api_hash
                    and base._session_string() and base._client is not None)

    @property
    def start_error(self):
        return self._base.start_error

    @property
    def enabled(self):
        return self._base.enabled

    def start(self):
        return self._base.start()

    def stop(self):
        # Never stop the shared conversation: the PRIMES login may be active.
        return True

    def set_referral_link(self, *a, **k):
        return self._base.set_referral_link(*a, **k)

    def __getattr__(self, name):
        # Anything the checker needs that we have not specialised (session
        # access, logging, referral state, ...) is taken from the base client.
        return getattr(self._base, name)

    @property
    def shares_login_conversation(self):
        """
        True when the configured "dedicated" checker bot IS the PRIMES login
        bot (same @handle). Then there is no second conversation and every
        check has to respect the login claim like the PRIMES checker does.
        """
        return normalize_username(self.bot_username) == normalize_username(
            getattr(self._base, "bot_username", ""))

    def _bound_proxy(self):
        """
        A proxy pinned to the DEDICATED checker bot's conversation.

        The entity is bound ON the userbot loop (get_entity is a coroutine and
        the loop already runs in its own thread - resolving it from here used
        to raise and silently fall back to the PRIMES bot entity, so the
        "dedicated" check was typed into the login conversation and collided
        with a running login / offer pre-warm). A binding failure is an error,
        never a reason to use the login conversation.
        """
        username = self.bot_username
        proxy = self._base._conversation_proxy(username)
        try:
            proxy._run(proxy._a_bind())
        except Exception as exc:
            raise MeeshoBotError(
                f"Could not open the dedicated checker conversation with "
                f"@{normalize_username(username)} on this session ({exc}). "
                f"Nothing was sent - the PRIMES login bot was NOT used as a "
                f"stand-in. Check checker.telegram_bot.username (the bot's "
                f"@handle) and that the account has started that bot."
            ) from exc
        return proxy

    # -- checks against the DEDICATED bot -----------------------------------

    def check_registration(self, number, overrides=None):
        """Ask the dedicated checker bot whether `number` is registered."""
        if not self.ready:
            raise MeeshoBotError(
                f"Checker bot userbot is not ready: {self._base.start_error}"
            )
        merged = dict(self.conf)
        merged.update(overrides or {})
        with self._checker_lock:
            proxy = self._bound_proxy()
            return proxy._run(
                proxy._a_check_registration(number, overrides=merged)
            )

    def check_registration_many(self, numbers, overrides=None):
        """
        Check several numbers in a single visit to the dedicated bot. The bot
        accepts a comma-separated list and answers with one verdict line per
        number, and (per the observed bot) the output order may differ from
        the input order. Verdicts are therefore matched BY NUMBER, and the
        result is returned as {10-digit number: True/False}.
        """
        numbers = [str(n).strip() for n in (numbers or []) if str(n).strip()]
        if not numbers:
            return {}
        if len(numbers) == 1:
            return {numbers[0]: self.check_registration(numbers[0], overrides=overrides)}
        if not self.ready:
            raise MeeshoBotError(
                f"Checker bot userbot is not ready: {self._base.start_error}"
            )
        merged = dict(self.conf)
        merged.update(overrides or {})
        with self._checker_lock:
            proxy = self._bound_proxy()
            return proxy._run(
                proxy._a_check_registration_many(numbers, overrides=merged)
            )




# ---------------------------------------------------------------------------
# Live diagnostic (no taps, no numbers):  python meesho_bot_client.py
# ---------------------------------------------------------------------------

def _dump_screen(screen, title, checker=None):
    print(f"\n--- {title} ---")
    print(f"classified : {screen.classify()}")
    print(f"number prompt: {screen.looks_like_number_prompt()} "
          f"(strict/offer-marker: {screen.looks_like_number_prompt(require_offer_marker=True)})")
    print(f"referral   : {screen.is_referral} "
          f"(prompt={screen.referral_prompt}, rejected={screen.referral_rejected})")
    skip = screen.referral_skip_button()
    yes = screen.referral_yes_button()
    print(f"skip option: {skip[2] if skip else None}")
    print(f"yes option : {yes[2] if yes else None}")
    if checker is not None:
        conf = checker
        verdict = screen.check_verdict(conf["registered_hints"],
                                       conf["not_registered_hints"])
        entry = screen.checker_button(conf["button_hints"],
                                      conf["button_fallbacks"],
                                      conf["button_exclude"])
        print(f"checker    : {screen.classify_check(conf['registered_hints'], conf['not_registered_hints'], conf['prompt_hints'])}"
              f" | verdict={verdict} | entry button={entry[2] if entry else None}")
    print(f"buttons    : {screen.button_summary}")
    print(f"upi price  : {screen.upi_price}")
    print("text       :")
    for line in (screen.text or "").splitlines():
        print(f"  | {line}")


def main(argv=None):
    """
    Print how the userbot currently sees the PRIMES bot screen. Read-only by
    default, so it is safe to run while automation is stopped; a referral
    screen showing up here is classified and resolved by the flow
    automatically.

    With a number it performs ONE check through the bot's checker and prints
    the verdict - the fastest way to tune checker.bot hints against the real
    bot:

        python meesho_bot_client.py 9876543210
    """
    import json
    import os
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    check_number = None
    for arg in argv:
        if arg.isdigit():
            check_number = arg
        elif arg in ("-h", "--help"):
            print(main.__doc__)
            return 0

    config = {}
    for name in ("config.json", "config.jon"):
        if os.path.exists(name):
            with open(name, "r", encoding="utf-8") as f:
                config = json.load(f)
            break

    client = MeeshoBotClient(config, log_fn=print)
    print(f"Referral step: {client.referral_summary}")
    print(f"Target UPI price: ₹{client.target_upi_price}")
    checker_conf = client._checker_settings()
    print(f"Bot checker: entry={checker_conf['entry']} "
          f"command={checker_conf['command'] or '(none)'} "
          f"step_timeout={checker_conf['step_timeout']:.0f}s")

    if not client.enabled:
        print("\nmeesho_bot.enabled is false - the flow will not run. "
              "Set it to true in config.json first.")
        return 1

    if not client.start():
        print(f"\nCould not start the userbot: {client.start_error}")
        return 1

    try:
        screen = client._run(client._latest_screen(), timeout=30)
        _dump_screen(screen, f"Current screen ({client.bot_username})", checker_conf)
        if check_number:
            print(f"\n--- Bot checker: {check_number} ---")
            try:
                result = client.check_registration(check_number)
            except Exception as exc:
                print(f"check failed: {exc}")
                return 1
            print(f"is_registered = {result['is_registered']} "
                  f"(source: {result.get('source', 'bot')})")
            print(f"raw result screen: {(result.get('message') or '').strip()}")
        return 0
    finally:
        client.stop()


if __name__ == "__main__":
    raise SystemExit(main())
