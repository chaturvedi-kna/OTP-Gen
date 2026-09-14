"""
Telegram USER-account automation ("userbot") for the PRIMES Meesho concierge bot.

Why a userbot: normal Telegram bots (Bot API) cannot tap another bot's inline
buttons or even see its traffic. Logged in as a regular Telegram USER account
via Telethon (MTProto), inline buttons are first-class API objects: "clicking"
is a direct callback call - there is no screen, no coordinates and no UI
automation involved.

Flow automated (from the recorded screens):
  /start -> [Add Account] -> [Login with Number] -> [Normal]
         -> offer screen: reroll [Try Another Offer] until UPI <= target price
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
    def __init__(self, message, screen_text=""):
        super().__init__(message)
        self.screen_text = screen_text


# ---------------------------------------------------------------------------
# Screen model / parsing (pure - no Telethon dependency, fully unit-testable)
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # symbols, emoji, transport, supplemental
    "\U00002600-\U000027BF"   # misc symbols / dingbats
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
S_OFFER = "offer"
S_OTP_WAIT = "otp_wait"
S_LINKED = "linked"
S_WRONG_OTP = "wrong_otp"
S_EXPIRED = "otp_expired"
S_BLOCKED = "blocked"
S_UNKNOWN = "unknown"


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
            f"Timed out waiting for bot screen (expected {expect})", last_screen.text
        )

    async def _click(self, screen, *needles):
        found = screen.find_button(*needles)
        if not found:
            raise MeeshoBotUnknownScreen(
                f"Button {needles} not found on screen", screen.text
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
            raise MeeshoBotUnknownScreen(f"Button {needles} vanished before click", screen.text)

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
        """Best-effort: tap Cancel / Main Menu until the main menu shows."""
        for _ in range(6):
            screen = await self._latest_screen()
            state = screen.classify()
            if state == S_MENU:
                return screen
            if screen.has_button("cancel"):
                try:
                    screen = await self._click(screen, "cancel")
                    continue
                except MeeshoBotError:
                    pass
            if screen.has_button("main menu"):
                try:
                    screen = await self._click(screen, "main menu")
                    continue
                except MeeshoBotError:
                    pass
            break
        # Hard reset.
        before = await self._signatures()
        await self._client.send_message(self._bot_entity, "/start")
        self._human_delay()
        return await self._wait_new_screen(before)

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

        screen = await self._latest_screen()

        if not continue_from_prompt:
            state = screen.classify()
            if state != S_MENU:
                screen = await self._cancel_to_menu()

            # Add Account
            if screen.classify() != S_LINK_CHOICE:
                screen = await self._click(screen, "add account")
            if screen.classify() != S_LINK_CHOICE:
                raise MeeshoBotUnknownScreen("Expected 'How would you like to link'", screen.text)

            # Login with Number
            screen = await self._click(screen, "login with numb")
            if screen.classify() != S_LOGIN_MODE:
                raise MeeshoBotUnknownScreen("Expected 'Choose login mode'", screen.text)

            # Normal mode
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
                    screen.text,
                )
            screen = await self._click(screen, "try another offer")
            screen = await self._settle_offer(screen)
            rerolls += 1

        result["rerolls"] = rerolls
        result["upi"] = screen.upi_price

        state = screen.classify()
        if state == S_BLOCKED:
            result["stage"] = "blocked"
            result["message"] = screen.text
            return result

        if state != S_OFFER:
            raise MeeshoBotUnknownScreen("Expected offer/number-prompt screen", screen.text)

        # Send the 10-digit number.
        screen = await self._send_text(str(number))
        state = screen.classify()
        if state == S_BLOCKED:
            result["stage"] = "blocked"
            result["message"] = screen.text
            return result
        if state != S_OTP_WAIT:
            raise MeeshoBotUnknownScreen(
                f"Expected 'OTP on its way' after sending number, got {state}", screen.text
            )

        result["stage"] = "otp_sent"
        result["message"] = screen.text
        return result

    async def _settle_offer(self, screen):
        """After a click that should lead to an offer, wait for UPI text."""
        deadline = time.time() + self.step_timeout
        current = screen
        while time.time() < deadline:
            state = current.classify()
            if state in (S_OFFER, S_BLOCKED, S_LINKED, S_OTP_WAIT, S_LOGIN_MODE, S_LINK_CHOICE):
                return current
            await asyncio.sleep(self.poll_interval)
            current = await self._latest_screen()
        return current

    async def _a_submit_otp(self, code):
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
                screen.text,
            )

        if new_number is None:
            return {"stage": "prompt", "screen": screen.text, "upi": screen.upi_price}

        screen = await self._send_text(str(new_number))
        if screen.classify() == S_BLOCKED:
            return {"stage": "blocked", "message": screen.text}
        if screen.classify() != S_OTP_WAIT:
            raise MeeshoBotUnknownScreen("Expected OTP-on-its-way after new number", screen.text)
        return {"stage": "otp_sent", "screen": screen.text, "upi": screen.upi_price}

    async def _a_return_to_menu(self):
        screen = await self._latest_screen()
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
