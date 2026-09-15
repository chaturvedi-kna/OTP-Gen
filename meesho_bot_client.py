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
            target price
         -> send 10-digit number -> "OTP on its way" screen
         -> (coordinator polls the OTP provider) -> send OTP code
         -> "Account linked!" (parse User ID / account #)
Recovery:
  OTP missing/wrong/expired/blocked -> [Change Number] -> send the next number
  (the bot keeps the current offer screen), instead of redoing the whole menu.

One-time setup: run `python login_userbot.py` to create the StringSession file.

This module degrades gracefully: if telethon is missing, the session is
missing, or config is incomplete, .ready is False and the coordinator falls
back to the existing manual Telegram trigger flow.
"""

import asyncio
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
S_LINKED = "linked"
S_WRONG_OTP = "wrong_otp"
S_EXPIRED = "otp_expired"
S_BLOCKED = "blocked"
S_UNKNOWN = "unknown"

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

class MeeshoBotClient:

    def __init__(self, config=None, log_fn=print):
        config = config or {}
        conf = config.get("meesho_bot", {}) or {}
        self.conf = conf
        self._log = log_fn

        self.enabled = bool(conf.get("enabled", False))
        self.api_id = conf.get("api_id")
        self.api_hash = (conf.get("api_hash") or "").strip()
        self.session_file = conf.get("session_file", "userbot.session.txt")
        self.bot_username = (conf.get("bot_username") or "").strip()

        self.target_upi_price = float(conf.get("target_upi_price", 47))
        self.max_offer_rerolls = int(conf.get("max_offer_rerolls", 30))
        self.max_change_number = int(conf.get("max_change_number", 5))
        self.step_timeout = float(conf.get("step_timeout_seconds", 60))
        self.poll_interval = float(conf.get("poll_interval_seconds", 1.2))
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

        try:
            self._run(_disconnect(), timeout=10)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro, timeout=None):
        if self._loop is None:
            raise MeeshoBotError("userbot event loop not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout or self.step_timeout + 30)

    # -- low-level Telethon helpers ----------------------------------------

    async def _latest_screen(self, limit=6):
        messages = await self._client.get_messages(self._bot_entity, limit=limit)
        for message in messages:
            if getattr(message, "text", None):
                return Screen.from_telethon(message)
        return Screen()

    async def _signatures(self, limit=6):
        signatures = set()
        messages = await self._client.get_messages(self._bot_entity, limit=limit)
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
                continue
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

    async def _click(self, screen, *needles):
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
        await target_msg.click(i=row, j=col)
        self._human_delay()
        return await self._wait_new_screen(before)

    async def _send_text(self, text, timeout=None):
        before = await self._signatures()
        await self._client.send_message(self._bot_entity, str(text))
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

    async def _settle(self, screen, states, timeout=None):
        """
        Poll the newest screens until one of `states` shows, answering the
        referral prompt whenever it interrupts (the bot inserts it between
        "Login with Number" and the login-mode/offer steps, and can re-offer it
        later in the flow).

        Returns the matching screen, or whatever is on screen at the deadline -
        callers decide whether that is an error.
        """
        timeout = timeout or self.step_timeout
        deadline = time.time() + timeout
        current = screen
        while True:
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
            if current.classify() in states:
                return current
            if time.time() >= deadline:
                return current
            await asyncio.sleep(self.poll_interval)
            current = await self._latest_screen()

    # -- high-level flow -----------------------------------------------------

    async def _a_prepare_login(self, number, continue_from_prompt=False):
        """
        Full navigation up to the number being submitted and the bot showing
        its "OTP on its way" screen. If continue_from_prompt is True, the bot
        is assumed to already sit on the offer/number prompt (after a previous
        Change Number), so only the number is sent.
        """
        rerolls = 0
        result = {"rerolls": 0, "upi": None}

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
            screen = await self._settle(screen, (S_OFFER, S_OTP_WAIT, S_BLOCKED, S_LINKED))
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
            if state != S_OFFER:
                raise MeeshoBotUnknownScreen(
                    "Expected the number prompt before sending the replacement number",
                    screen.text, screen.button_labels,
                )
        else:
            state = screen.classify()
            if state != S_MENU:
                screen = await self._cancel_to_menu()

            # Add Account
            if screen.classify() != S_LINK_CHOICE:
                screen = await self._click(screen, "add account")
            screen = await self._settle(screen, (S_LINK_CHOICE,))
            if screen.classify() != S_LINK_CHOICE:
                raise MeeshoBotUnknownScreen(
                    "Expected 'How would you like to link'",
                    screen.text, screen.button_labels,
                )

            # Login with Number -> referral screen (🔗 Set Refer Link /
            # 🎁 Referral link?) -> login mode. The referral step sits between
            # these two on current bot revisions; _settle answers it whenever
            # it appears, so both orderings work.
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

        # Offer screen + reroll until UPI price target is met
        screen = await self._settle_offer(screen)
        while True:
            state = screen.classify()
            if state in (S_BLOCKED, S_LINKED, S_OTP_WAIT, S_WRONG_OTP, S_EXPIRED):
                break
            upi = screen.upi_price
            result["upi"] = upi
            if state == S_OFFER and upi is not None and upi <= self.target_upi_price:
                break
            if rerolls >= self.max_offer_rerolls:
                raise MeeshoBotUnknownScreen(
                    f"UPI price never reached ₹{self.target_upi_price} after {rerolls} rerolls",
                    screen.text, screen.button_labels,
                )
            screen = await self._click(screen, "try another offer")
            screen = await self._settle_offer(screen)
            rerolls += 1

        result["rerolls"] = rerolls
        result["upi"] = screen.upi_price
        result["referral_action"] = self.last_referral_action

        state = screen.classify()
        if state == S_BLOCKED:
            result["stage"] = "blocked"
            result["message"] = screen.text
            return result

        if state != S_OFFER:
            raise MeeshoBotUnknownScreen(
                "Expected offer/number-prompt screen", screen.text, screen.button_labels
            )

        # Send the 10-digit number.
        self._log(f"[MEESHO-BOT] Offer accepted at UPI ₹{result['upi']} "
                  f"({rerolls} reroll(s)); sending number {number}.")
        screen = await self._send_text(str(number))
        state = screen.classify()
        if state == S_BLOCKED:
            result["stage"] = "blocked"
            result["message"] = screen.text
            return result
        if state != S_OTP_WAIT:
            raise MeeshoBotUnknownScreen(
                f"Expected 'OTP on its way' after sending number, got {state}",
                screen.text, screen.button_labels,
            )

        result["stage"] = "otp_sent"
        result["message"] = screen.text
        return result

    async def _settle_offer(self, screen):
        """
        After a click that should lead to an offer, wait for UPI text, dealing
        with a referral prompt if the bot re-offers it before the number is
        entered (Cancel/Change Number reroutes can land back on it).
        """
        return await self._settle(
            screen,
            (S_OFFER, S_BLOCKED, S_LINKED, S_OTP_WAIT, S_LOGIN_MODE, S_LINK_CHOICE),
        )

    async def _a_submit_otp(self, code):
        # The bot can re-ask for a referral link after the number was sent
        # (observed: it reposts the prompt). Answer it, but never type the code
        # into a referral field - if the bot is no longer waiting for the code,
        # that has to be reported, not papered over.
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

        screen = await self._send_text(str(code))
        state = screen.classify()
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
        """
        screen = await self._latest_screen()
        state = screen.classify()

        if state == S_OFFER:
            pass  # already at the number prompt
        elif screen.has_button("change number"):
            screen = await self._click(screen, "change number")
            screen = await self._settle_offer(screen)
        else:
            # Unexpected place: rebuild a full flow instead.
            screen = await self._cancel_to_menu()
            if new_number is None:
                return {"stage": "needs_full_flow"}
            return await self._a_prepare_login(new_number)

        if screen.classify() != S_OFFER:
            raise MeeshoBotUnknownScreen(
                f"Expected number prompt after Change Number, got {screen.classify()}",
                screen.text, screen.button_labels,
            )

        if new_number is None:
            return {"stage": "prompt", "screen": screen.text, "upi": screen.upi_price}

        screen = await self._send_text(str(new_number))
        # A re-offered referral prompt here must never swallow the replacement
        # number: answer it and wait for the OTP-wait screen instead.
        screen = await self._settle(
            screen, (S_OTP_WAIT, S_BLOCKED, S_LINKED, S_WRONG_OTP, S_EXPIRED),
        )
        if screen.classify() == S_BLOCKED:
            return {"stage": "blocked", "message": screen.text}
        if screen.classify() != S_OTP_WAIT:
            raise MeeshoBotUnknownScreen(
                "Expected OTP-on-its-way after new number",
                screen.text, screen.button_labels,
            )
        return {"stage": "otp_sent", "screen": screen.text, "upi": screen.upi_price}

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

    def prepare_login(self, number):
        return self._run(self._a_prepare_login(number, continue_from_prompt=False))

    def continue_with_number(self, number):
        return self._run(self._a_prepare_login(number, continue_from_prompt=True))

    def submit_otp(self, code):
        return self._run(self._a_submit_otp(code))

    def change_number(self, new_number=None):
        return self._run(self._a_change_number(new_number))

    def return_to_menu(self):
        return self._run(self._a_return_to_menu())

    def cancel_flow(self):
        return self._run(self._a_cancel_flow())


# ---------------------------------------------------------------------------
# Live diagnostic (no taps, no numbers):  python meesho_bot_client.py
# ---------------------------------------------------------------------------

def _dump_screen(screen, title):
    print(f"\n--- {title} ---")
    print(f"classified : {screen.classify()}")
    print(f"referral   : {screen.is_referral} "
          f"(prompt={screen.referral_prompt}, rejected={screen.referral_rejected})")
    skip = screen.referral_skip_button()
    yes = screen.referral_yes_button()
    print(f"skip option: {skip[2] if skip else None}")
    print(f"yes option : {yes[2] if yes else None}")
    print(f"buttons    : {screen.button_summary}")
    print(f"upi price  : {screen.upi_price}")
    print("text       :")
    for line in (screen.text or "").splitlines():
        print(f"  | {line}")


def main():
    """
    Print how the userbot currently sees the PRIMES bot screen. Read-only, so
    it is safe to run while automation is stopped; a referral screen showing
    up here is classified and resolved by the flow automatically.
    """
    import json
    import os

    config = {}
    for name in ("config.json", "config.jon"):
        if os.path.exists(name):
            with open(name, "r", encoding="utf-8") as f:
                config = json.load(f)
            break

    client = MeeshoBotClient(config, log_fn=print)
    print(f"Referral step: {client.referral_summary}")
    print(f"Target UPI price: ₹{client.target_upi_price}")

    if not client.enabled:
        print("\nmeesho_bot.enabled is false - the flow will not run. "
              "Set it to true in config.json first.")
        return 1

    if not client.start():
        print(f"\nCould not start the userbot: {client.start_error}")
        return 1

    try:
        screen = client._run(client._latest_screen(), timeout=30)
        _dump_screen(screen, f"Current screen ({client.bot_username})")
        return 0
    finally:
        client.stop()


if __name__ == "__main__":
    raise SystemExit(main())
