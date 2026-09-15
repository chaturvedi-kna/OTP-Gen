"""
Notification layer for the Meesho OTP automation.

Two independent backends:
  * Termux  -- native Android notifications via termux-notification
  * Telegram -- interactive messages, copy button, button feedback, and bot commands (/run, /status, /balance, /stop)
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
import re


BASE_DIR = Path(__file__).resolve().parent
SIGNAL_DIR = BASE_DIR / ".signals"

# Stable notification ids so updates replace instead of stacking.
NOTIF_ID_STATUS = "meesho-status"
NOTIF_ID_ACTION = "meesho-action"
NOTIF_ID_OTP = "meesho-otp"


def _log(message):
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"), flush=True)


# ---------------------------------------------------------------------------
# Termux backend
# ---------------------------------------------------------------------------

class TermuxBackend:
    """Android notifications through the Termux:API bridge."""

    def __init__(self, enabled=True, command_timeout=20):
        self.enabled = enabled
        self.command_timeout = command_timeout
        self.binary = shutil.which("termux-notification")
        self.remove_binary = shutil.which("termux-notification-remove")
        self.clipboard_binary = shutil.which("termux-clipboard-set")
        self.last_error = None

    @staticmethod
    def looks_like_termux():
        prefix = os.environ.get("PREFIX", "")
        return "com.termux" in prefix or Path("/data/data/com.termux").exists()

    @property
    def available(self):
        return bool(self.enabled and self.binary)

    def unavailable_reason(self):
        if not self.enabled:
            return "Termux notifications are disabled in config.json."
        if self.binary:
            return None
        if self.looks_like_termux():
            return (
                "Running inside Termux but 'termux-notification' was not found.\n"
                "  Fix: pkg install termux-api\n"
                "  AND install the Termux:API app (F-Droid or GitHub releases).\n"
                "  The pkg and the app are two separate things - you need both."
            )
        return "Not running inside Termux, so Android notifications are unavailable."

    def api_app_responds(self):
        probe = shutil.which("termux-battery-status")
        if not probe:
            return False, "termux-battery-status not found (termux-api package missing)."
        try:
            result = subprocess.run(
                [probe],
                capture_output=True,
                timeout=10,
                text=True
            )
        except subprocess.TimeoutExpired:
            return False, (
                "Termux:API command timed out. The Termux:API app is almost "
                "certainly not installed (or was force-stopped / battery-killed)."
            )
        except Exception as exc:
            return False, f"Termux:API probe failed: {exc}"

        if result.returncode != 0:
            return False, f"Termux:API probe exited {result.returncode}: {result.stderr.strip()}"
        return True, "Termux:API app is responding."

    def send(self, title, message, priority="default", notif_id=NOTIF_ID_STATUS,
             buttons=None, ongoing=False, alert_once=False):
        if not self.available:
            self.last_error = self.unavailable_reason()
            return False

        argv = [
            self.binary,
            "--id", str(notif_id),
            "--title", str(title),
            "--priority", priority,
        ]

        if priority in ("high", "max"):
            argv += ["--sound", "--vibrate", "800,400,800"]
            argv += ["--led-color", "FF0000", "--led-on", "800", "--led-off", "800"]

        if ongoing:
            argv += ["--ongoing"]

        if alert_once:
            argv += ["--alert-once"]

        for index, (label, action) in enumerate((buttons or [])[:3], start=1):
            argv += [f"--button{index}", str(label), f"--button{index}-action", str(action)]

        try:
            result = subprocess.run(
                argv,
                input=str(message),
                capture_output=True,
                text=True,
                timeout=self.command_timeout
            )
        except subprocess.TimeoutExpired:
            self.last_error = "termux-notification timed out."
            return False
        except Exception as exc:
            self.last_error = f"termux-notification failed: {exc}"
            return False

        if result.returncode != 0:
            self.last_error = f"termux-notification exited {result.returncode}: {(result.stderr or '').strip()}"
            return False

        self.last_error = None
        return True

    def remove(self, notif_id):
        if not self.remove_binary:
            return
        try:
            subprocess.run(
                [self.remove_binary, str(notif_id)],
                capture_output=True,
                timeout=10
            )
        except Exception:
            pass

    def clipboard_command(self, text):
        binary = self.clipboard_binary or "termux-clipboard-set"
        safe = str(text).replace("'", "'\\''")
        return f"{binary} '{safe}'"


# ---------------------------------------------------------------------------
# Telegram backend
# ---------------------------------------------------------------------------

class TelegramBackend:
    """Telegram bot notifications with interactive copy buttons, instant feedback, and commands."""

    API_ROOT = "https://api.telegram.org"

    def __init__(self, token="", chat_id="", enabled=True, timeout=15):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.enabled = enabled
        self.timeout = timeout
        self.last_error = None
        self._update_offset = None
        self.status_callback = None
        self.balance_callback = None
        self.run_callback = None
        self.stop_callback = None
        self.referral_callback = None

    @property
    def configured(self):
        return bool(self.enabled and self.token and self.chat_id)

    def unavailable_reason(self):
        if not self.enabled:
            return "Telegram is disabled in config.json."
        if not self.token:
            return "No Telegram bot token in config.json under telegram.bot_token."
        if not self.chat_id:
            return "No Telegram chat_id in config.json under telegram.chat_id."
        return None

    def _call(self, method, payload=None, timeout=None):
        if not self.token:
            self.last_error = "Telegram bot token is empty."
            return None

        url = f"{self.API_ROOT}/bot{self.token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"}
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            detail = ""
            reader = getattr(exc, "read", None)
            if callable(reader):
                try:
                    detail = " | " + reader().decode("utf-8", "replace")[:300]
                except Exception:
                    detail = ""
            self.last_error = f"Telegram {method} failed: {exc}{detail}"
            return None

        if not body.get("ok"):
            self.last_error = f"Telegram {method} error: {body}"
            return None

        self.last_error = None
        return body.get("result")

    def get_me(self):
        return self._call("getMe")

    def discover_chat_id(self):
        updates = self._call("getUpdates", {"timeout": 0, "limit": 100})
        if not updates:
            return None
        for update in reversed(updates):
            message = (
                update.get("message")
                or update.get("channel_post")
                or (update.get("callback_query") or {}).get("message")
            )
            chat = (message or {}).get("chat") or {}
            if chat.get("id") is not None:
                return str(chat["id"])
        return None

    @staticmethod
    def _normalize_buttons(buttons):
        """
        Converts buttons (flat list or list of rows, containing tuples or dicts)
        into standard Telegram inline_keyboard structure.
        Supports copy_text buttons: {"text": "...", "copy_text": {"text": "..."}}
        """
        if not buttons:
            return None

        # Check if already a list of rows
        first = buttons[0] if buttons else None
        if isinstance(first, (list, tuple)) and first and isinstance(first[0], (dict, tuple, list)):
            raw_rows = buttons
        else:
            raw_rows = [buttons]

        rows = []
        for raw_row in raw_rows:
            row = []
            for item in raw_row:
                if isinstance(item, dict):
                    row.append(item)
                elif isinstance(item, (tuple, list)):
                    label = str(item[0])
                    val = str(item[1]) if len(item) > 1 else ""
                    if val.startswith("copy:"):
                        copy_num = val.split(":", 1)[1].strip()
                        row.append({
                            "text": label,
                            "copy_text": {"text": copy_num}
                        })
                    else:
                        row.append({
                            "text": label,
                            "callback_data": val
                        })
            if row:
                rows.append(row)
        return rows

    @staticmethod
    def _escape_preserving_code(text):
        """
        Escapes text for Telegram MarkdownV2 while preserving `code` spans.
        Inside `code` spans, only ` and \ need escaping.
        """
        parts = str(text).split("`")
        out = []
        for i, part in enumerate(parts):
            if i % 2 == 0:
                out.append(TelegramBackend._escape(part))
            else:
                code_safe = re.sub(r'([\\`])', r'\\\1', part)
                out.append(f"`{code_safe}`")
        return "".join(out)

    def send(self, title, message, buttons=None, silent=False):
        if not self.configured:
            self.last_error = self.unavailable_reason()
            return False

        text = f"*{self._escape(title)}*\n{self._escape_preserving_code(message)}"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_notification": bool(silent),
        }

        norm_buttons = self._normalize_buttons(buttons)
        if norm_buttons:
            payload["reply_markup"] = {"inline_keyboard": norm_buttons}

        result = self._call("sendMessage", payload)
        if result is None:
            # If failed (e.g. copy_text not supported or markdown issue), fallback
            if norm_buttons:
                # Replace copy_text with callback fallback
                fb_buttons = []
                for row in norm_buttons:
                    fb_row = []
                    for b in row:
                        if "copy_text" in b:
                            fb_row.append({"text": b["text"], "callback_data": f"copy:{b['copy_text']['text']}"})
                        else:
                            fb_row.append(b)
                    fb_buttons.append(fb_row)
                payload["reply_markup"] = {"inline_keyboard": fb_buttons}

            payload.pop("parse_mode", None)
            payload["text"] = f"{title}\n{message}"
            result = self._call("sendMessage", payload)

        return result is not None

    def edit_reply_markup(self, chat_id, message_id, buttons):
        """Update inline buttons on an existing message to give instant visual feedback."""
        norm_buttons = self._normalize_buttons(buttons) or []
        return self._call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": norm_buttons}
        })

    def answer_callback_query(self, callback_query_id, text, show_alert=False):
        """Send feedback toast/popup to user upon tapping an inline button."""
        return self._call("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert
        })

    def poll_signal(self, accept):
        """
        Poll updates for button presses, command replies, or custom actions.
        Provides instant visual feedback on Telegram when buttons are pressed.
        """
        if not self.configured:
            return None

        payload = {"timeout": 0, "limit": 20}
        if self._update_offset is not None:
            payload["offset"] = self._update_offset

        updates = self._call("getUpdates", payload, timeout=10)
        if not updates:
            return None

        signal = None
        for update in updates:
            self._update_offset = update["update_id"] + 1

            callback = update.get("callback_query")
            if callback:
                cid = callback["id"]
                token = (callback.get("data") or "").strip().lower()
                msg = callback.get("message")
                msg_id = msg.get("message_id") if msg else None
                chat_id = msg.get("chat", {}).get("id") if msg else None

                # Provide immediate button feedback
                if token in accept:
                    if token == "go":
                        self.answer_callback_query(cid, "✅ OTP Triggered! Waiting for SMS...")
                        if msg_id and chat_id:
                            self.edit_reply_markup(chat_id, msg_id, [
                                [("✅ OTP Triggered (Confirmed)", "done")]
                            ])
                        _log("Telegram feedback: 'OTP Triggered' button pressed.")
                    elif token == "skip":
                        self.answer_callback_query(cid, "⏭ Number skipped.")
                        if msg_id and chat_id:
                            self.edit_reply_markup(chat_id, msg_id, [
                                [("⏭ Number Skipped", "done")]
                            ])
                        _log("Telegram feedback: 'Skip Number' button pressed.")

                    if signal is None:
                        signal = accept[token]
                    continue

                elif token.startswith("copy:"):
                    num = token.split(":", 1)[1]
                    self.answer_callback_query(cid, f"📋 Number: {num}\n(Tap monospace number in message to copy)", show_alert=True)
                    continue

                elif token == "done":
                    self.answer_callback_query(cid, "Already confirmed.")
                    continue

                self.answer_callback_query(cid, "Received.")
                continue

            msg = update.get("message") or {}
            raw_text = (msg.get("text") or "").strip()
            token = raw_text.lstrip("/").lower()

            # Handle bot commands (/status, /run, /stop, /balance, /start)
            if raw_text.startswith("/"):
                cmd = token.split()[0].split("@", 1)[0]
                if cmd == "status" and self.status_callback:
                    status_text = self.status_callback()
                    self.send("Tool Status", status_text)
                    continue
                elif cmd == "balance" and self.balance_callback:
                    balance_text = self.balance_callback()
                    self.send("Live Balances", balance_text)
                    continue
                elif cmd == "run" and self.run_callback:
                    self.run_callback()
                    self.send("Automation Started", "▶️ Started search for target number.")
                    continue
                elif cmd == "stop" and self.stop_callback:
                    self.stop_callback()
                    self.send("Automation Stopping", "⏹ Stopping active search.")
                    continue
                elif cmd == "referral" and self.referral_callback:
                    # /referral <link> - save; /referral off - clear;
                    # /referral alone - show the current setting.
                    parts = raw_text.split(None, 1)
                    argument = parts[1].strip() if len(parts) > 1 else ""
                    try:
                        reply = self.referral_callback(argument)
                    except Exception as exc:
                        reply = f"❌ Referral command failed: {exc}"
                    self.send("Referral Link", reply or "Referral command handled.")
                    continue
                elif cmd == "start":
                    self.send(
                        "Meesho Automation Bot",
                        "Commands available:\n"
                        "▶️ /run - Start searching for fresh numbers\n"
                        "ℹ️ /status - Check current tool status & progress\n"
                        "💰 /balance - Check live balances on all providers\n"
                        "🎁 /referral <link> - Save the Meesho referral link "
                        "(/referral off to clear, /referral to show)\n"
                        "⏹ /stop - Stop running search"
                    )
                    continue

            if token in accept and signal is None:
                signal = accept[token]

        return signal

    def drain(self):
        if not self.configured:
            return
        payload = {"timeout": 0, "limit": 100}
        if self._update_offset is not None:
            payload["offset"] = self._update_offset
        updates = self._call("getUpdates", payload, timeout=10)
        for update in updates or []:
            self._update_offset = update["update_id"] + 1

    @staticmethod
    def _escape(text):
        """Escape MarkdownV2 reserved characters with a single regex pass."""
        return re.sub(r'([_*\[\]()~>#+=|{}.!\\-])', r'\\\1', str(text))


# ---------------------------------------------------------------------------
# Notifier facade
# ---------------------------------------------------------------------------

class Notifier:

    def __init__(self, config=None):
        config = config or {}
        termux_conf = config.get("termux", {}) if isinstance(config, dict) else {}
        telegram_conf = config.get("telegram", {}) if isinstance(config, dict) else {}

        self.termux = TermuxBackend(
            enabled=termux_conf.get("enabled", True)
        )
        self.telegram = TelegramBackend(
            token=telegram_conf.get("bot_token", ""),
            chat_id=telegram_conf.get("chat_id", ""),
            enabled=telegram_conf.get("enabled", True)
        )

        self.warned_termux = False
        self.warned_telegram = False

        SIGNAL_DIR.mkdir(exist_ok=True)

    def set_command_callbacks(self, status_cb=None, balance_cb=None, run_cb=None,
                              stop_cb=None, referral_cb=None):
        """
        Attach callbacks for interactive Telegram commands (/status, /balance,
        /run, /stop, /referral <link>).
        """
        self.telegram.status_callback = status_cb
        self.telegram.balance_callback = balance_cb
        self.telegram.run_callback = run_cb
        self.telegram.stop_callback = stop_cb
        self.telegram.referral_callback = referral_cb

    def send(self, title, message, priority="default", notif_id=NOTIF_ID_STATUS,
             termux_buttons=None, telegram_buttons=None, ongoing=False, silent=False):
        _log(f"\n[NOTIFICATION: {title}]\n{message}\n")

        delivered = []

        if self.termux.send(
            title, message,
            priority=priority,
            notif_id=notif_id,
            buttons=termux_buttons,
            ongoing=ongoing
        ):
            delivered.append("termux")
        elif self.termux.last_error and not self.warned_termux:
            _log(f"  [android notification not sent] {self.termux.last_error}")
            self.warned_termux = True

        if self.telegram.send(title, message, buttons=telegram_buttons, silent=silent):
            delivered.append("telegram")
            _log("  [telegram delivered successfully]")
        elif self.telegram.last_error:
            _log(f"  [telegram not sent] {self.telegram.last_error}")

        return delivered

    def alert(self, title, message, **kwargs):
        kwargs.setdefault("priority", "max")
        return self.send(title, message, **kwargs)

    def otp_result(self, code, number, sms, provider_name=""):
        p_tag = f"[{provider_name.upper()}] " if provider_name else ""
        clean_10 = re.sub(r"\D", "", str(number))
        if len(clean_10) == 12 and clean_10.startswith("91"):
            clean_10 = clean_10[2:]
        elif len(clean_10) == 11 and clean_10.startswith("0"):
            clean_10 = clean_10[1:]
        elif len(clean_10) >= 10:
            clean_10 = clean_10[-10:]

        return self.alert(
            f"{p_tag}OTP: {code}",
            f"Number: `{clean_10}`\nCode: `{code}`\nSMS: {sms}",
            notif_id=NOTIF_ID_OTP,
            termux_buttons=[
                ("Copy OTP", self.termux.clipboard_command(code)),
                ("Dismiss", f"termux-notification-remove {NOTIF_ID_OTP}"),
            ],
            telegram_buttons=[
                [({"text": f"📋 Copy OTP: {code}", "copy_text": {"text": str(code)}})],
                [("Dismiss", "done")]
            ]
        )

    def _signal_path(self, name):
        return SIGNAL_DIR / f"{name}.signal"

    def _clear_signals(self, names):
        for name in names:
            path = self._signal_path(name)
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass

    def _read_file_signal(self, names):
        for name in names:
            if self._signal_path(name).exists():
                return name
        return None

    @staticmethod
    def _read_stdin_signal():
        try:
            import select
            if not sys.stdin or not sys.stdin.isatty():
                return None
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                return None
            line = sys.stdin.readline().strip().lower()
        except Exception:
            return None

        if line in ("", "g", "go", "y", "yes", "ok", "done", "triggered"):
            return "go"
        if line in ("s", "skip", "n", "no", "next"):
            return "skip"
        return None

    def wait_for_trigger(self, number, activation_id, provider_name="OTP", timeout=300,
                         poll_interval=2, on_tick=None):
        """
        Blocks until the user confirms they manually requested OTP on Meesho.
        Provides a dedicated Telegram 'Copy Number' button and instant click feedback.
        """
        names = ["go", "skip"]
        self._clear_signals(names)
        self.telegram.drain()

        go_action = f"touch {self._signal_path('go')}"
        skip_action = f"touch {self._signal_path('skip')}"

        # Strictly extract 10-digit number (stripping +91, 91, or leading 0)
        clean_10 = re.sub(r"\D", "", str(number))
        if len(clean_10) == 12 and clean_10.startswith("91"):
            clean_10 = clean_10[2:]
        elif len(clean_10) == 11 and clean_10.startswith("0"):
            clean_10 = clean_10[1:]
        elif len(clean_10) >= 10:
            clean_10 = clean_10[-10:]

        title = f"ACTION NEEDED - Number Found ({provider_name.upper()})"
        # Enclose 10-digit number in backticks so single-tap on mobile Telegram copies it!
        message = (
            f"Provider: {provider_name.upper()}\n"
            f"Number: `{clean_10}`\n"
            f"Activation: `{activation_id}`\n\n"
            f"1. Tap 'Copy {clean_10}' (or tap the number above) & request OTP on Meesho.\n"
            f"2. Then tap 'OTP Triggered' below.\n\n"
            f"The {timeout}s OTP wait starts only after you confirm."
        )

        # Telegram buttons layout:
        # Row 1: Copy button (using Telegram copy_text)
        # Row 2: Action buttons (OTP Triggered, Skip Number)
        telegram_buttons = [
            [{"text": f"📋 Copy {clean_10}", "copy_text": {"text": str(clean_10)}}],
            [
                {"text": "✅ OTP Triggered", "callback_data": "go"},
                {"text": "⏭ Skip Number", "callback_data": "skip"}
            ]
        ]

        termux_buttons = [
            ("OTP Triggered", go_action),
            ("Copy Number", self.termux.clipboard_command(clean_10)),
            ("Skip Number", skip_action),
        ]

        self.send(
            title, message,
            priority="max",
            notif_id=NOTIF_ID_ACTION,
            ongoing=True,
            termux_buttons=termux_buttons,
            telegram_buttons=telegram_buttons
        )

        _log(
            f"Waiting for you to trigger OTP on {clean_10} ({provider_name}).\n"
            "Confirm by: Telegram 'OTP Triggered' button, notification button, "
            "Telegram /go, or pressing Enter here. Type 's' + Enter to skip."
        )

        deadline = time.time() + timeout
        last_reminder = time.time()
        result = "timeout"

        while time.time() < deadline:
            signal = (
                self._read_file_signal(names)
                or self._read_stdin_signal()
                or self.telegram.poll_signal({"go": "go", "skip": "skip"})
            )

            if signal:
                result = signal
                break

            if on_tick:
                try:
                    early = on_tick()
                except Exception:
                    early = None
                if early:
                    result = early
                    break

            remaining = int(deadline - time.time())
            if time.time() - last_reminder >= 60 and remaining > 0:
                last_reminder = time.time()
                _log(f"Still waiting for OTP trigger confirmation ({remaining}s left)...")
                self.send(
                    f"Still waiting - Number Found ({provider_name.upper()})",
                    f"Number: `{clean_10}`\n{remaining}s left to confirm you triggered the OTP.",
                    priority="max",
                    notif_id=NOTIF_ID_ACTION,
                    ongoing=True,
                    termux_buttons=termux_buttons,
                    telegram_buttons=telegram_buttons
                )

            time.sleep(poll_interval)

        self.termux.remove(NOTIF_ID_ACTION)
        self._clear_signals(names)

        _log(f"Trigger gate result: {result.upper()}")
        return result


# ---------------------------------------------------------------------------
# Diagnostics / CLI
# ---------------------------------------------------------------------------

def _load_config():
    for name in ("config.json", "config.jon"):
        path = BASE_DIR / name
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                _log(f"Could not parse {name}: {exc}")
    return {}


def doctor():
    _log("=" * 60)
    _log("NOTIFICATION DOCTOR")
    _log("=" * 60)

    config = _load_config()
    notifier = Notifier(config)

    # --- Termux ---
    _log("\n-- Android / Termux --")
    in_termux = TermuxBackend.looks_like_termux()
    _log(f"Running inside Termux: {in_termux}")
    _log(f"termux-notification found: {bool(notifier.termux.binary)}")

    # --- Telegram ---
    _log("\n-- Telegram --")
    token_ok = bool(notifier.telegram.token)
    _log(f"Bot token present: {token_ok}")
    if token_ok:
        me = notifier.telegram.get_me()
        _log(f"getMe: {me.get('username') if me else notifier.telegram.last_error}")
        _log(f"chat_id present: {bool(notifier.telegram.chat_id)}")
        if notifier.telegram.chat_id:
            sent = notifier.telegram.send(
                "Meesho Notifier Test",
                "Testing Telegram notifications with interactive buttons.",
                buttons=[
                    [{"text": "📋 Copy Test: 9876543210", "copy_text": {"text": "9876543210"}}],
                    [("✅ OTP Triggered", "go"), ("⏭ Skip Number", "skip")]
                ]
            )
            _log(f"Test message sent: {sent}")
        else:
            disc = notifier.telegram.discover_chat_id()
            if disc:
                _log(f"Recent chat_id discovered: {disc}")

    _log("=" * 60)


if __name__ == "__main__":
    doctor()
