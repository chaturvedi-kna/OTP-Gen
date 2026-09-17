"""
Meesho OTP Automation Orchestrator.

Parallel worker execution for SMS providers (TemporaSMS, VSImpro, OtpDoctor,
OTPCart) with checker validation, refund/balance safety gating, interactive
Telegram commands (/run, /status, /balance, /stop), and optional end-to-end
automation of the PRIMES Meesho concierge Telegram bot via a Telethon userbot.
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

# Ensure UTF-8 output when possible on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from base_otp import (
    BaseOTPClient,
    OTPError,
    OTPProviderUnavailable,
    OTPNoBalance,
    OTPNoNumbers
)
from otp_client import (
    OTPClient,
    build_tempora_client,
    build_otpdoctor_client,
    create_otp_clients
)
from checker_client import (
    CheckerClient,
    CheckerError,
    CheckerUnavailable
)
from checker_router import (
    CheckerRouter,
    MODE_BOT,
    MODE_AUTO,
    normalize_mode
)
from state import StateStore
from stats import StatsStore
from balance_guard import BalanceGuard
from meesho_bot_client import (
    MeeshoBotClient,
    MeeshoBotError,
    MeeshoBotReferralError,
    MeeshoBotTimeout,
    MeeshoBotUnknownScreen,
    S_OTP_WAIT,
)
from notifier import Notifier


CONFIG_FILES = ["config.json", "config.jon"]


def now():
    return datetime.now(timezone.utc).isoformat()


def log(message, prefix=""):
    tag = f"[{prefix}] " if prefix else ""
    msg = f"[{now()}] {tag}{message}"
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


class NumberContext:
    """Encapsulates acquired number and its specific provider client."""
    def __init__(self, client, activation_id, raw_number, clean_number):
        self.client = client
        self.provider_name = client.name
        self.activation_id = activation_id
        self.raw_number = raw_number
        self.clean_number = clean_number
        self.acquired_at = now()


class ParallelAutomationCoordinator:

    def __init__(self, config, provider_override=None):
        self.config = config
        self.provider_override = provider_override

        self.checker_conf = config.get("checker", {})
        self.settings = config.get("automation", {})

        self.state = StateStore()
        self.stats = StatsStore()
        self.guard = BalanceGuard(config.get("balance_guard", {}), log_fn=log)
        self.notify = Notifier(config)
        self.bot = MeeshoBotClient(config, log_fn=log)

        # Number checking strategy: API only, PRIMES bot only, or API with an
        # automatic bot fallback (checker.mode in config.json - see
        # SETUP_CHECKER.md). The bot is looked up lazily so replacing
        # self.bot at runtime (tests) is picked up.
        self.checker = CheckerRouter(
            config,
            bot_getter=lambda: self.bot,
            log_fn=log,
            stats=self.stats
        )
        self.checker_service = self.checker_conf.get("service", "meesho")
        self.target_registered = self.settings.get("target_registered", False)
        log(
            f"Checker: {self.checker.describe()}, service '{self.checker_service}', "
            f"API retry budget {self.checker.max_retry_wait_seconds:.0f}s"
        )

        # Thread synchronization primitives
        self.stop_requested = threading.Event()
        self.target_found_event = threading.Event()
        self.active_target = None
        self.active_target_lock = threading.Lock()

        # Metrics & Worker Status
        self.worker_statuses = {}
        self.worker_attempts = {}
        self.worker_max_attempts = {}
        self.total_attempts = 0
        # Consecutive checker failures (bot mode / bot fallback): a broken
        # checker must not keep buying numbers only to cancel them.
        self.checker_failure_streak = 0
        self.attempts_lock = threading.Lock()
        self.is_running = False

        # Per-provider balance ledger (expected balance after refund, etc.)
        self.ledger = {}
        self.ledger_lock = threading.Lock()

        # PRIMES bot flow state
        self.bot_at_number_prompt = False  # bot sitting on offer/number prompt after Change Number
        self.bot_change_attempts = 0
        self._bot_warned = False

        # Build active clients
        self.clients = create_otp_clients(config, provider_override=provider_override)
        for c in self.clients:
            c_conf = self.config.get(c.name, {}) or (self.config.get("otp", {}) if c.name == "otpdoctor" else {})
            self.worker_max_attempts[c.name] = c_conf.get("max_attempts") or self.settings.get("max_attempts", 200)
            self.worker_attempts[c.name] = 0

        # Attach Telegram commands
        self.notify.set_command_callbacks(
            status_cb=self.get_status_summary,
            balance_cb=self.get_balances_summary,
            run_cb=self.request_run,
            stop_cb=self.request_stop,
            referral_cb=self.command_referral_link,
            checker_cb=self.command_checker_mode
        )

    # -- Ledger helpers ------------------------------------------------------

    def _ledger(self, name):
        with self.ledger_lock:
            return self.ledger.setdefault(name, {
                "expected_balance": None,
                "activation_id": None,
                "number": None,
            })

    def _note_balance(self, name, balance):
        if balance is None:
            return
        with self.ledger_lock:
            entry = self.ledger.setdefault(name, {})
            entry["expected_balance"] = float(balance)

    def _note_activation(self, name, activation_id, number):
        with self.ledger_lock:
            entry = self.ledger.setdefault(name, {"expected_balance": None})
            entry["activation_id"] = activation_id
            entry["number"] = number

    def _expected_balance(self, name, activation_id):
        with self.ledger_lock:
            entry = self.ledger.get(name, {})
            return entry.get("expected_balance")

    def _note_checker_failure(self):
        """Count one more consecutive failed check; returns the streak."""
        with self.attempts_lock:
            self.checker_failure_streak += 1
            return self.checker_failure_streak

    def _reset_checker_failures(self):
        with self.attempts_lock:
            self.checker_failure_streak = 0

    def _critical_stop(self, title, message):
        """Halt ALL workers and send a max-priority alert."""
        log(f"CRITICAL STOP: {title} - {message}")
        self.stop_requested.set()
        self.stats.increment("critical_stops")
        self.notify.alert(f"🛑 {title}", message + "\n\nAutomation STOPPED. Manual intervention required.")

    # -- Status and Balances for Telegram & CLI ------------------------------

    def get_balances_summary(self):
        """Fetches live balance from all configured clients."""
        lines = ["💰 Current OTP Balances:"]
        for client in self.clients:
            try:
                bal = client.get_balance()
                lines.append(f"• {client.name.upper()}: {bal:.4f}")
            except Exception as exc:
                lines.append(f"• {client.name.upper()}: Error ({exc})")
        return "\n".join(lines)

    def get_status_summary(self):
        """Returns live tool status."""
        state_str = "🟢 RUNNING" if self.is_running else "⚪ IDLE / STOPPED"
        lines = [
            f"🤖 Meesho Automation: {state_str}",
            f"Mode: {self.config.get('active_otp_provider', 'both').upper()}",
            f"PRIMES bot automation: {'🟢 ON' if self.bot.ready else '⚪ manual'}",
            f"Checker: {self.checker.describe()}",
        ]
        cooldown = self.checker.cooldown_remaining()
        if cooldown > 0:
            lines.append(f"  ⚠️ API cooling down, bot checker in use for ~{cooldown:.0f}s")

        if self.worker_max_attempts:
            att_parts = []
            for name, m_att in self.worker_max_attempts.items():
                curr = self.worker_attempts.get(name, 0)
                att_parts.append(f"{name.upper()}: {curr}/{m_att}")
            lines.append(f"Attempts: {', '.join(att_parts)}")

        with self.active_target_lock:
            if self.active_target:
                lines.append(
                    f"🎯 Active Target: `{self.active_target.clean_number}` "
                    f"({self.active_target.provider_name.upper()})"
                )

        if self.bot.ready or self.bot.referral_link:
            link = self.bot.referral_link
            shown = f"{link[:48]}..." if len(link) > 51 else (link or "(not set)")
            lines.append(f"Referral: {shown} | on failure: {self.bot.referral_failure_action}")

        if self.worker_statuses:
            lines.append("\nWorkers:")
            for name, st in self.worker_statuses.items():
                lines.append(f"  • {name.upper()}: {st}")

        s = self.stats.snapshot()
        lines.append(
            "\n📊 Totals\n"
            f"  Accounts linked: {s['accounts_linked']}\n"
            f"  Targets found: {s['targets_found']} | OTPs received: {s['otp_received']}\n"
            f"  Wrong OTP: {s['otp_wrong']} | Expired: {s['otp_expired']} | Blocked: {s['user_blocked']}\n"
            f"  OTP timeouts: {s['otp_timeout']} | Change-number: {s['change_number']}\n"
            f"  Referral step: {s.get('referral_pasted', 0)} pasted | "
            f"{s.get('referral_skipped', 0)} skipped | {s.get('offer_rerolls', 0)} offer rerolls\n"
            f"  Checks: {s.get('checker_api_checks', 0)} via API | "
            f"{s.get('checker_bot_checks', 0)} via PRIMES bot | "
            f"{s.get('checker_fallbacks', 0)} API→bot fallbacks\n"
            f"  Numbers consumed (charged): {s['numbers_consumed']}\n"
            f"  Refunds verified: {s['refunds_verified']} | Refunds missing: {s['refunds_missing']} | "
            f"Late OTP salvaged: {s['late_otp_salvaged']}"
            + (f"\n  🛑 Critical stops: {s['critical_stops']}" if s.get('critical_stops') else "")
        )

        lines.append("\n" + self.get_balances_summary())
        return "\n".join(lines)

    # -- Referral link (config + Telegram /referral command) -----------------

    def referral_status_text(self):
        link = self.bot.referral_link or "(not set)"
        mode = self.bot.referral_failure_action
        if mode == "stop":
            behaviour = ("if the bot asks for a referral link and this link cannot be "
                         "used, the automation stops, reports it and cancels the number "
                         "with a refund check")
        else:
            behaviour = ("if the bot asks for a referral link and this link cannot be "
                         "used, the bot's own \u201cI don't have a refer code\u201d button "
                         "is tapped and the login continues")
        return (f"Referral link: {link}\n"
                f"Failure action: {mode}\n\n{behaviour}\n\n"
                "Commands:\n"
                "• /referral <link> - save a link\n"
                "• /referral off - clear it\n"
                "• /referral - show this")

    def command_referral_link(self, argument):
        """
        Telegram /referral handler. Saves the link to config.json AND applies it
        to the running userbot. Returns the reply text for the chat.
        """
        argument = (argument or "").strip()
        lowered = argument.lower()

        if lowered in ("", "status", "show", "?"):
            return self.referral_status_text()

        if lowered in ("off", "none", "clear", "remove", "delete", "reset"):
            link = ""
        else:
            link = argument
            if not (link.lower().startswith("http") and "meesho" in link.lower()):
                return ("❌ That does not look like a Meesho referral link (it should "
                        "start with http and contain app.meesho.com).\n"
                        "Nothing was changed.\n\n" + self.referral_status_text())

        config_path = next((name for name in CONFIG_FILES if os.path.exists(name)), None)
        if config_path is None:
            return ("❌ config.json was not found, so the link could not be saved.\n"
                    "Use the CLI instead: python main.py --set-referral-link <link>")

        try:
            set_referral_link(config_path, link)
        except Exception as exc:
            return f"❌ Could not update {config_path}: {exc}"

        previous = self.bot.set_referral_link(link)
        log(f"Referral link {'set' if link else 'cleared'} via Telegram command "
            f"(was: {previous or 'not set'}).")

        if link:
            return (f"✅ Referral link saved to {config_path} and applied now.\n"
                    f"{link}\n\n"
                    f"It is pasted once per login when the bot asks for it "
                    f"(failure action: {self.bot.referral_failure_action}).")
        return ("✅ Referral link cleared. If the bot now asks for a referral link, "
                "the automation stops, reports it and cancels the number with a "
                'refund check (change meesho_bot.referral_failure_action to "skip" '
                "to continue without a link instead).")

    # -- Checker mode (config + Telegram /checker command) -------------------

    def checker_status_text(self):
        lines = [
            f"Checker mode: {self.checker.mode.upper()}",
            f"{self.checker.describe()}",
            f"Service: {self.checker_service}",
        ]
        if self.checker.mode == MODE_AUTO:
            triggers = [name for name in
                        ("is_down", "timeout", "network", "http_5xx", "auth",
                         "rate_limit", "unknown")
                        if self.checker.fallback.get(name)]
            lines.append(f"API failures that switch to the bot: "
                         f"{', '.join(triggers) if triggers else '(none - auto behaves like api)'}")
            lines.append(f"API cooldown after a fallback: "
                         f"{self.checker.fallback['cooldown_seconds']:.0f}s "
                         f"(doubling, max {self.checker.fallback['max_cooldown_seconds']:.0f}s)")
            remaining = self.checker.cooldown_remaining()
            if remaining > 0:
                lines.append(f"⚠️ API cooling down right now - bot checks for ~{remaining:.0f}s")
        if self.checker.mode_wants_bot:
            if self.checker.bot_ready:
                lines.append("PRIMES bot checker: 🟢 ready")
            else:
                lines.append(f"PRIMES bot checker: ⚪ not ready "
                             f"({self.checker.bot.unavailable_reason or 'unknown reason'})")
        s = self.stats.snapshot()
        lines.append(
            f"Checks so far: {s.get('checker_api_checks', 0)} via API | "
            f"{s.get('checker_bot_checks', 0)} via PRIMES bot | "
            f"{s.get('checker_fallbacks', 0)} fallbacks"
        )
        lines.append(
            "\nCommands:\n"
            "• /checker api - checker API only\n"
            "• /checker bot - PRIMES bot checker only\n"
            "• /checker auto - API first, PRIMES bot when the API fails\n"
            "• /checker - show this"
        )
        return "\n".join(lines)

    def command_checker_mode(self, argument):
        """
        Telegram /checker handler: show the current strategy, or switch between
        api / bot / auto. The choice is saved to config.json and applied to the
        running tool immediately (no restart).
        """
        argument = (argument or "").strip()
        if argument.lower() in ("", "status", "show", "?"):
            return self.checker_status_text()

        mode = normalize_mode(argument, default=None)
        if mode is None:
            return (f"❌ Unknown checker mode '{argument}'. Use api, bot or auto.\n\n"
                    + self.checker_status_text())

        config_path = next((name for name in CONFIG_FILES if os.path.exists(name)), None)
        saved = ""
        if config_path is None:
            saved = "\n⚠️ config.json not found - the change applies to this run only."
        else:
            try:
                set_checker_mode(config_path, mode)
                saved = f"\nSaved to {config_path}."
            except Exception as exc:
                saved = f"\n⚠️ Could not save to {config_path}: {exc}"

        previous = self.checker.mode
        self.checker.mode = mode
        log(f"Checker mode changed via Telegram: {previous} -> {mode}.")
        return f"✅ Checker mode set to {mode.upper()}.{saved}\n\n" + self.checker_status_text()

    def request_run(self):
        if self.is_running:
            log("Restart requested while running. Stopping current workers first...")
            self.stop_requested.set()
            time.sleep(1.5)
        threading.Thread(target=self.run, daemon=True).start()

    def request_stop(self):
        self.stop_requested.set()
        log("Stop requested via command.")

    # -- Cancellation, salvage & refund tally --------------------------------

    def handle_cancellation(self, client, activation_id, number, reason,
                            expected_balance=None, expect_refund=True):
        """
        Cancel an activation and VERIFY the refund tallies before any new number
        may be purchased. Handles provider cooldowns and the race in which the
        OTP lands while the cancellation is in flight (late-OTP salvage).

        expect_refund:
          - True  (no SMS delivered): refund must restore the pre-purchase
                  balance; mismatch triggers a global critical stop.
          - False (SMS already delivered, e.g. bot rejected the OTP): the charge
                  legitimately stands; the new lower balance becomes the baseline.

        Returns {"tally_ok", "salvaged", "balance"}.
        """
        pname = client.name.upper()
        if expected_balance is None:
            expected_balance = self._expected_balance(client.name, activation_id)

        log(f"Cancelling activation {activation_id} for number {number} (Reason: {reason})...", prefix=pname)

        cancel_res = None
        try:
            cancel_res = client.cancel(activation_id)
            log(f"Cancellation response: {cancel_res}", prefix=pname)
        except Exception as exc:
            log(f"Error while cancelling activation {activation_id}: {exc}", prefix=pname)

        # Handle provider cooldowns (e.g. WAIT_CANCEL:120 on OtpDoctor)
        if cancel_res and cancel_res.get("type") == "WAIT_CANCEL":
            wait_seconds = cancel_res.get("seconds", 120)
            log(f"[COOLDOWN] Waiting {wait_seconds}s before retrying cancellation...", prefix=pname)
            self.worker_statuses[client.name] = f"Waiting cooldown ({wait_seconds}s)"
            time.sleep(wait_seconds + 1)
            try:
                cancel_res = client.cancel(activation_id)
                log(f"Cancellation response after cooldown: {cancel_res}", prefix=pname)
            except Exception as exc:
                log(f"Error while retrying cancellation {activation_id}: {exc}", prefix=pname)

        elif cancel_res and cancel_res.get("type") == "EARLY_CANCEL_DENIED":
            cooldown = max(self.settings.get("cancel_cooldown_seconds", 120), 120)
            log(f"[COOLDOWN] Early cancel denied. Waiting {cooldown}s before retrying...", prefix=pname)
            self.worker_statuses[client.name] = f"Waiting cooldown ({cooldown}s)"
            time.sleep(cooldown)
            try:
                cancel_res = client.cancel(activation_id)
                log(f"Cancellation response after cooldown: {cancel_res}", prefix=pname)
            except Exception as exc:
                log(f"Error while retrying cancellation {activation_id}: {exc}", prefix=pname)

        self.stats.increment("numbers_cancelled")

        salvaged = None
        tally_ok = True
        actual_balance = None

        if not expect_refund:
            # SMS was already delivered (e.g. the bot rejected a wrong/expired
            # code): the charge legitimately stands. The new (lower) balance is
            # the correct baseline for the next number.
            refund_delay = self.settings.get("refund_check_delay_seconds", 2)
            if refund_delay > 0:
                time.sleep(refund_delay)
            try:
                actual_balance = client.get_balance()
                self._note_balance(client.name, actual_balance)
            except Exception:
                pass
            self.stats.increment("numbers_consumed")
            log(f"Number consumed (SMS delivered); new balance baseline: {actual_balance}", prefix=pname)
        else:
            # Salvage race: an OTP may have landed as the cancel was processed.
            try:
                salvaged = self.guard.salvage_late_otp(
                    client, activation_id,
                    probes=self.settings.get("cancel_salvage_probes", 2),
                    delay=self.settings.get("cancel_salvage_delay", 1.5),
                    prefix=pname,
                )
            except Exception:
                salvaged = None
            if salvaged:
                self.stats.increment("late_otp_salvaged")
                code = salvaged.get("code")
                sms = salvaged.get("sms", "")
                log(f"🚨 OTP arrived during cancellation race! Code: {code}", prefix=pname)
                # Immediate alert (before anything else touches the PRIMES
                # bot): the code is in hand while the bot still sits on its
                # OTP screen, so it can be used automatically - and manually
                # if the auto-submit fails.
                self.notify.alert(
                    f"🚨 [{pname}] OTP arrived during cancellation",
                    f"Number: {number}\nActivation: {activation_id}\nReason: {reason}\n\n"
                    f"Code: `{code}`\nSMS: {sms}\n\n"
                    "The SMS was delivered, so the charge stands (no refund). "
                    "The PRIMES bot is still on its OTP screen - the automation "
                    "will submit this code itself; if that fails, enter it "
                    "manually while it is still valid."
                )
                # The charge legitimately stands: the SMS this number was paid
                # for was delivered. The current balance becomes the new
                # baseline - running the refund tally here would always fail
                # (no refund is owed) and critical-stop the automation for a
                # charge that is correct.
                refund_delay = self.settings.get("refund_check_delay_seconds", 2)
                if refund_delay > 0:
                    time.sleep(refund_delay)
                try:
                    actual_balance = client.get_balance()
                    self._note_balance(client.name, actual_balance)
                except Exception:
                    pass
                self.stats.increment("numbers_consumed")
                log(f"Number consumed (late OTP delivered); new balance "
                    f"baseline: {actual_balance}", prefix=pname)
                tally_ok = True
            else:
                refund_delay = self.settings.get("refund_check_delay_seconds", 2)
                if refund_delay > 0:
                    time.sleep(refund_delay)

                tally_ok, actual_balance = self.guard.verify_refund(
                    client, expected_balance,
                    activation_id=activation_id, number=number,
                    stop_event=self.stop_requested, prefix=pname,
                )

                if expected_balance is not None and tally_ok:
                    self.stats.increment("refunds_verified")
                    self._note_balance(client.name, actual_balance)
                elif expected_balance is not None and not tally_ok:
                    self.stats.increment("refunds_missing")
                    salvage_note = (
                        f"\nLate OTP code: `{salvaged.get('code')}`" if salvaged else
                        "\nNo OTP was seen - check the provider panel for this activation."
                    )
                    self._critical_stop(
                        f"[{pname}] REFUND DID NOT TALLY",
                        f"Number: {number}\nActivation: {activation_id}\n"
                        f"Expected balance: ~{expected_balance:.4f}\nActual balance: {actual_balance}\n"
                        f"Reason for cancel: {reason}{salvage_note}\n\n"
                        "No new numbers will be purchased until you verify this activation "
                        "in the provider panel. The PRIMES bot was left on its OTP "
                        "screen - if the OTP shows up, enter it manually."
                    )

        result = {"tally_ok": bool(tally_ok), "salvaged": salvaged, "balance": actual_balance}

        self.state.save({
            "status": "CANCELLED",
            "provider": client.name,
            "activation_id": activation_id,
            "number": number,
            "reason": reason,
            "expect_refund": expect_refund,
            "refund_tallied": bool(tally_ok) if expect_refund else None,
            "expected_balance": expected_balance,
            "actual_balance": actual_balance,
            "salvaged_otp": (salvaged or {}).get("code"),
            "cancelled_at": now()
        })
        return result

    # -- Parallel Worker Loop ------------------------------------------------

    def worker_loop(self, client):
        """
        Independent thread loop for a single provider.
        Fetches numbers, validates with checker, cancels if used (verifying the
        refund tally), and yields to the coordinator on an unregistered match.
        """
        pname = client.name.upper()
        log(f"Worker started.", prefix=pname)

        client_conf = self.config.get(client.name, {}) or (self.config.get("otp", {}) if client.name == "otpdoctor" else {})
        client_max_attempts = client_conf.get("max_attempts") or self.settings.get("max_attempts", 200)
        self.worker_max_attempts[client.name] = client_max_attempts
        self.worker_attempts[client.name] = 0
        retry_delay = self.settings.get("retry_delay_seconds", 1.0)

        # Initial provider balance seeds the refund-tally ledger.
        try:
            bal = client.get_balance()
            self._note_balance(client.name, bal)
            log(f"Initial balance: {bal:.4f} (Max Attempts: {client_max_attempts})", prefix=pname)
        except Exception as exc:
            log(f"Warning: Could not fetch initial balance: {exc}", prefix=pname)

        while not self.stop_requested.is_set():
            # If a target is being processed, pause fetching
            if self.target_found_event.is_set():
                self.worker_statuses[client.name] = "Paused (target being processed)"
                time.sleep(1)
                continue

            with self.attempts_lock:
                if self.worker_attempts[client.name] >= client_max_attempts:
                    log(f"Reached configured max attempts ({client_max_attempts}). Worker completed.", prefix=pname)
                    self.worker_statuses[client.name] = f"Completed ({client_max_attempts}/{client_max_attempts} attempts)"
                    break
                self.worker_attempts[client.name] += 1
                current_attempt = self.worker_attempts[client.name]
                self.total_attempts += 1

            # SAFETY GATE: never buy a number while a refund discrepancy is open.
            if self.stop_requested.is_set():
                break

            self.worker_statuses[client.name] = f"Attempt {current_attempt}/{client_max_attempts}: Requesting number"
            log(f"Requesting number (Attempt {current_attempt}/{client_max_attempts})...", prefix=pname)

            # Request number based on provider type
            try:
                if client.name == "tempora":
                    res = client.get_number()
                elif client.name == "vsimpro":
                    vsi_conf = self.config.get("vsimpro", {})
                    res = client.get_number(
                        service=vsi_conf.get("service", "meesho"),
                        country=vsi_conf.get("country", "22"),
                        operator=vsi_conf.get("operator", "smart"),
                        max_price=vsi_conf.get("max_price")
                    )
                elif client.name == "otpcart":
                    res = client.get_number()
                else:
                    otp_conf = self.config.get("otp", {})
                    res = client.get_number(
                        service=otp_conf.get("service", "12843"),
                        country=otp_conf.get("country", "in"),
                        max_price=otp_conf.get("max_price", 9.5)
                    )
            except OTPNoBalance as exc:
                balance_wait = getattr(client, "balance_wait_seconds", 0) or client_conf.get("balance_update_delay_seconds", 0)
                if balance_wait > 0:
                    log(f"Provider reported OTPNoBalance. Waiting up to {balance_wait}s for balance/refund update...", prefix=pname)
                    self.worker_statuses[client.name] = f"Waiting balance update (up to {balance_wait}s)"
                    deadline = time.time() + balance_wait
                    restored = False
                    while time.time() < deadline and not self.stop_requested.is_set():
                        time.sleep(3.0)
                        try:
                            bal = client.get_balance()
                            if bal >= 5.0:
                                log(f"Balance updated to {bal:.4f}. Resuming search...", prefix=pname)
                                self._note_balance(client.name, bal)
                                restored = True
                                break
                        except Exception:
                            pass
                    if restored:
                        continue

                log(f"Provider out of balance: {exc}", prefix=pname)
                self.worker_statuses[client.name] = "Stopped (NO_BALANCE)"
                self.notify.alert(
                    f"⚠️ [{pname}] Insufficient Balance",
                    f"Provider {pname} reported insufficient balance (NO_BALANCE).\n"
                    f"Please recharge your account.\n\n"
                    f"Once recharged:\n• Send /balance\n• Send /run"
                )
                return
            except (OTPProviderUnavailable, OTPNoNumbers) as exc:
                log(f"Transient error: {exc}. Retrying in {retry_delay * 3}s...", prefix=pname)
                self.worker_statuses[client.name] = f"Transient error: {exc}"
                time.sleep(retry_delay * 3)
                continue
            except OTPError as exc:
                log(f"API error: {exc}", prefix=pname)
                self.worker_statuses[client.name] = f"Error: {exc}"
                time.sleep(retry_delay * 2)
                continue
            except Exception as exc:
                # Never let an unexpected error silently kill an unattended worker.
                log(f"Unexpected error while requesting number: {exc!r}. Retrying...", prefix=pname)
                self.worker_statuses[client.name] = f"Unexpected error: {exc}"
                time.sleep(retry_delay * 3)
                continue

            res_type = res.get("type")

            if res_type in ("TRY_AGAIN", "NO_NUMBERS"):
                log(f"Provider reported {res_type}. Waiting {retry_delay}s...", prefix=pname)
                time.sleep(retry_delay)
                continue

            if res_type == "NO_BALANCE":
                log(f"Provider reported fatal error: NO_BALANCE (Insufficient Balance)", prefix=pname)
                self.worker_statuses[client.name] = "Stopped (NO_BALANCE)"
                self.notify.alert(
                    f"⚠️ [{pname}] Insufficient Balance",
                    f"Provider {pname} reported NO_BALANCE.\n"
                    f"Please recharge.\nOnce recharged:\n• Send /balance\n• Send /run"
                )
                return

            if res_type == "PRICE_TOO_HIGH":
                price = res.get("price")
                max_price = res.get("max_price")
                log(f"Price too high ({price} > max {max_price}). Worker killed.", prefix=pname)
                self.worker_statuses[client.name] = f"Stopped (PRICE_TOO_HIGH: {price} > {max_price})"
                self.notify.alert(
                    f"⚠️ [{pname}] Price Exceeded",
                    f"Provider {pname} price ({price}) exceeded configured max price ({max_price}).\nWorker stopped."
                )
                return

            if res_type in ("BAD_KEY", "BAD_SERVICE"):
                log(f"Provider fatal error: {res_type}", prefix=pname)
                self.worker_statuses[client.name] = f"Stopped ({res_type})"
                self.notify.alert(
                    f"❌ [{pname}] Provider Error",
                    f"Provider {pname} returned fatal error: {res_type}.\nWorker stopped."
                )
                return

            if res_type != "ACCESS_NUMBER":
                log(f"Unexpected response: {res}", prefix=pname)
                time.sleep(retry_delay)
                continue

            # Number acquired!
            activation_id = res["activation_id"]
            raw_number = res["number"]
            clean_number = CheckerClient.format_number(raw_number)
            self._note_activation(client.name, activation_id, clean_number)

            context = NumberContext(
                client=client,
                activation_id=activation_id,
                raw_number=raw_number,
                clean_number=clean_number
            )

            log(f"Acquired number: {raw_number} (Clean: {clean_number}, Activation: {activation_id})", prefix=pname)
            self.state.save({
                "status": "ACQUIRED",
                "provider": client.name,
                "activation_id": activation_id,
                "raw_number": raw_number,
                "clean_number": clean_number,
                "acquired_at": now()
            })

            # Check registration on the Meesho checker (API and/or PRIMES bot,
            # depending on checker.mode).
            self.worker_statuses[client.name] = f"Checking registration for {clean_number}"
            log(f"Checking {clean_number} on {self.checker_service} checker "
                f"(mode: {self.checker.mode})...", prefix=pname)
            try:
                check = self.checker.check(self.checker_service, clean_number)
            except (CheckerUnavailable, CheckerError) as exc:
                log(f"Checker error (mode {self.checker.mode}): {exc}. "
                    f"Cancelling number...", prefix=pname)
                self.handle_cancellation(client, activation_id, clean_number, f"Checker error: {exc}")
                if self.stop_requested.is_set():
                    return
                # With the bot (or the bot fallback) in the checking path, a run
                # of failures means the checker itself is broken: stop instead
                # of buying numbers only to cancel them.
                if isinstance(exc, CheckerUnavailable) and self.checker.mode_wants_bot:
                    streak = self._note_checker_failure()
                    limit = self.checker.stop_after_failures
                    if limit and streak >= limit:
                        self._critical_stop(
                            "Checker unavailable",
                            f"{streak} checks in a row failed (checker mode "
                            f"{self.checker.mode}): {exc}\n\n"
                            "Stopping so no more numbers are bought only to be "
                            "cancelled. Fix the checker/userbot and send /run, or "
                            "switch with /checker api."
                        )
                        return
                continue

            self._reset_checker_failures()
            is_registered = check.get("is_registered", False)
            checker_source = check.get("source", "api")
            log(f"Checker result via {checker_source}: is_registered={is_registered} "
                f"(Target: {self.target_registered})", prefix=pname)

            if is_registered != self.target_registered:
                reason = "Already registered on Meesho" if is_registered else "Not registered on Meesho"
                log(f"Number {clean_number} does not match target. Cancelling on {pname}...", prefix=pname)
                self.handle_cancellation(client, activation_id, clean_number, reason)
                if self.stop_requested.is_set():
                    return
                continue

            # MATCH FOUND!
            log(f"🎯 TARGET MATCH FOUND! Number: {clean_number} (is_registered={is_registered})", prefix=pname)

            claimed = False
            with self.active_target_lock:
                if not self.target_found_event.is_set():
                    self.active_target = context
                    self.target_found_event.set()
                    self.worker_statuses[client.name] = f"Target Found: {clean_number}"
                    claimed = True

            if claimed:
                self.state.save({
                    "status": "TARGET_FOUND",
                    "provider": context.provider_name,
                    "activation_id": context.activation_id,
                    "raw_number": context.raw_number,
                    "number": context.clean_number,
                    "is_registered": is_registered,
                    "checker_source": checker_source,
                    "found_at": now()
                })
                # This worker parks here while the coordinator drives the bot/OTP flow.
                while self.target_found_event.is_set() and not self.stop_requested.is_set():
                    time.sleep(0.5)
            else:
                log(f"Another worker already claimed target. Cancelling duplicate...", prefix=pname)
                self.handle_cancellation(client, activation_id, clean_number, "Duplicate target match")
                if self.stop_requested.is_set():
                    return

        log(f"Worker stopped.", prefix=pname)
        self.worker_statuses[client.name] = "Stopped"

    # -- PRIMES bot flow + manual trigger ------------------------------------

    def _bot_send_number(self, context, from_prompt):
        """Drive the PRIMES bot up to its 'OTP on its way' screen. Returns result dict or None."""
        number = context.clean_number
        pname = context.provider_name.upper()
        self.worker_statuses[context.provider_name] = f"Bot: entering {number}"
        log(f"Driving PRIMES bot for {number} (from_prompt={from_prompt})...", prefix=pname)
        try:
            if from_prompt:
                res = self.bot.continue_with_number(number)
            else:
                res = self.bot.prepare_login(number)
        except MeeshoBotReferralError as exc:
            # referral_failure_action = "stop": the login must not continue
            # without the configured referral link. Cancel the number (nothing
            # was submitted to Meesho, so the refund must tally), report why,
            # and stop the automation until the link is fixed.
            log(f"PRIMES bot referral step failed: {exc}", prefix=pname)
            buttons = getattr(exc, "buttons", None)
            buttons_line = f"Buttons: {' / '.join(buttons)}\n\n" if buttons else ""
            try:
                self.bot.cancel_flow()
            except Exception as exc2:
                log(f"Reset of the bot flow failed ({exc2}); a manual /start may be needed.",
                    prefix=pname)
            self.bot_at_number_prompt = False
            self.bot_change_attempts = 0
            refund = self.handle_cancellation(
                context.client, context.activation_id, number,
                "PRIMES referral step failed", expect_refund=True
            )
            refund_line = (
                "Refund tally: ✅ balanced (no money lost)." if refund.get("tally_ok") is not False
                else "Refund tally: ❌ NOT verified - check the provider panel."
            )
            self._critical_stop(
                f"[{pname}] Referral step failed - automation stopped",
                f"Number: {number} was cancelled before it reached Meesho.\n"
                f"Reason: {exc}\n\n"
                f"Screen:\n{exc.screen_text[:600]}\n\n{buttons_line}"
                f"{refund_line}\n\n"
                "Fix the referral link, then start again:\n"
                "• Telegram: /referral <your Meesho referral link>\n"
                "• CLI: python main.py --set-referral-link <link>\n"
                "• Or allow logins without it: set meesho_bot.referral_failure_action "
                'to "skip".'
            )
            return None
        except MeeshoBotUnknownScreen as exc:
            log(f"PRIMES bot unexpected screen: {exc}; screen: {exc.screen_text[:300]}", prefix=pname)
            buttons = getattr(exc, "buttons", None)
            buttons_line = f"Buttons: {' / '.join(buttons)}\n\n" if buttons else ""
            self.notify.alert(
                f"⚠️ [{pname}] PRIMES bot needs attention",
                f"Unexpected bot screen while processing {number}:\n\n{exc.screen_text[:600]}\n\n"
                f"{buttons_line}"
                "Cancelling this number and resetting the bot flow. The refund is "
                "checked against the balance - if the number had already reached "
                "the bot (SMS delivered), the tally will flag it.\n"
                f"Referral step: {self.bot.referral_summary}"
            )
            try:
                self.bot.cancel_flow()
            except Exception as exc2:
                log(f"Reset of the bot flow failed ({exc2}); a manual /start may be needed.",
                    prefix=pname)
            self.bot_at_number_prompt = False
            self.bot_change_attempts = 0
            # The number was never submitted to Meesho (we stopped before the
            # number prompt/OTP screen), so the provider refund must be credited.
            self.handle_cancellation(context.client, context.activation_id, number,
                                     "PRIMES bot unexpected screen", expect_refund=True)
            return None
        except MeeshoBotTimeout as exc:
            # The Telegram side stopped responding mid-flow (hang watchdog /
            # hard cap): the number may or may not have been submitted,
            # depending on where it hung - the refund tally will flag it if
            # the SMS had already gone out. Never crash the automation.
            log(f"PRIMES bot flow timed out: {exc}", prefix=pname)
            self.notify.alert(
                f"⏱️ [{pname}] PRIMES bot flow timed out",
                f"Number: {number}\n{exc}\n\n"
                "Cancelling this number and resetting the bot flow. If the "
                "number had already reached Meesho (SMS delivered), the refund "
                "tally will flag it.\n"
                f"Referral step: {self.bot.referral_summary}"
            )
            try:
                self.bot.cancel_flow()
            except Exception as exc2:
                log(f"Reset of the bot flow failed ({exc2}); a manual /start may be needed.",
                    prefix=pname)
            self.bot_at_number_prompt = False
            self.bot_change_attempts = 0
            self.handle_cancellation(context.client, context.activation_id, number,
                                     "PRIMES bot flow timed out", expect_refund=True)
            return None
        except MeeshoBotError as exc:
            log(f"PRIMES bot error: {exc}", prefix=pname)
            self.notify.alert(f"⚠️ [{pname}] PRIMES bot error", f"{exc}\nCancelling this number.")
            self.handle_cancellation(context.client, context.activation_id, number, f"Bot error: {exc}")
            return None

        if res.get("stage") == "blocked":
            self.stats.increment("user_blocked")
            totals = self.stats.summary()
            log(f"Number {number} blocked by Meesho. Changing number.", prefix=pname)
            self.notify.alert(
                f"🚫 [{pname}] Number blocked by Meesho",
                f"Number: {number}\n{res.get('message', '')[:300]}\n\n{totals}"
            )
            try:
                self.bot.cancel_flow()
            except Exception:
                pass
            self.bot_at_number_prompt = False
            self.bot_change_attempts = 0
            self.handle_cancellation(context.client, context.activation_id, number, "Meesho blocked number")
            return None

        if res.get("stage") != "otp_sent":
            log(f"PRIMES bot did not reach OTP screen: {res}", prefix=pname)
            self.handle_cancellation(context.client, context.activation_id, number, "Bot flow failed")
            return None

        rerolls = res.get("rerolls", 0)
        if rerolls:
            self.stats.increment("offer_rerolls", rerolls)
        self.state.save({
            "status": "BOT_OTP_REQUESTED",
            "provider": context.provider_name,
            "activation_id": context.activation_id,
            "number": number,
            "upi_price": res.get("upi"),
            "offer_rerolls": rerolls,
            "requested_at": now()
        })
        referral_action = res.get("referral_action")
        referral_line = f"Referral: {referral_action}\n" if referral_action else ""
        if isinstance(referral_action, str):
            if referral_action.startswith("pasted"):
                self.stats.increment("referral_pasted")
            elif referral_action.startswith(("tapped", "answered")):
                self.stats.increment("referral_skipped")
        self.notify.send(
            "📲 OTP requested via PRIMES bot",
            f"Number: `{number}` ({pname})\nUPI price: ₹{res.get('upi')}\n"
            f"Offer rerolls: {rerolls}\n{referral_line}Waiting for SMS code..."
        )
        return res

    def _bot_submit_code(self, context, code, sms, late=False):
        """Submit the OTP into the PRIMES bot and classify the result. Returns status string."""
        pname = context.provider_name.upper()
        log(f"Submitting OTP {code} to PRIMES bot...", prefix=pname)
        try:
            res = self.bot.submit_otp(code)
        except MeeshoBotReferralError as exc:
            # The referral step broke the login after the SMS was already
            # delivered: surface it, stop the automation, and let the caller's
            # recovery finish the activation off (the charge legitimately stands).
            self.notify.alert(
                f"🛑 [{pname}] Referral step failed after OTP - stopping",
                f"Number: {context.clean_number}\nCode: `{code}`\n\n{exc}\n\n"
                f"Screen:\n{exc.screen_text[:600]}\n\n"
                "The code was NOT submitted - use it manually if the account is "
                "still pending.\n"
                "Fix the link (/referral <link>) or set "
                'meesho_bot.referral_failure_action to "skip", then start again.'
            )
            self._critical_stop(
                f"[{pname}] Referral step failed after OTP",
                f"Number: {context.clean_number} could not complete the login and "
                f"the SMS was already consumed (charge stands). Automation stopped."
            )
            return "unknown"
        except MeeshoBotUnknownScreen as exc:
            buttons = getattr(exc, "buttons", None)
            buttons_line = f"Buttons: {' / '.join(buttons)}\n\n" if buttons else ""
            self.notify.alert(
                f"⚠️ [{pname}] PRIMES bot needs attention after OTP",
                f"Number: {context.clean_number}\nCode: `{code}`\n\n"
                f"{exc}\n\nUnexpected screen:\n{exc.screen_text[:600]}\n\n"
                f"{buttons_line}Submit/verify manually if needed."
            )
            return "unknown"
        except MeeshoBotTimeout as exc:
            # The bot stopped responding while the code was being submitted /
            # verified. The code may or may not have reached the bot - check
            # it manually; the SMS was delivered, so the charge stands.
            self.notify.alert(
                f"⏱️ [{pname}] PRIMES bot timed out after OTP",
                f"Number: {context.clean_number}\nCode: `{code}`\n\n{exc}\n\n"
                "The code may or may not have reached the bot - check it "
                "manually while it is still valid. Will change number and retry."
            )
            return "unknown"
        except MeeshoBotError as exc:
            self.notify.alert(
                f"⚠️ [{pname}] PRIMES bot error after OTP",
                f"Number: {context.clean_number}\nCode: `{code}`\n{exc}"
            )
            return "unknown"

        status = res.get("status", "unknown")
        totals = self.stats.summary()

        if status == "linked":
            n_linked = self.stats.increment("accounts_linked")
            # The account is created - accept the provider charge (status 6).
            try:
                context.client.finish(context.activation_id)
                log(f"Activation {context.activation_id} finished after link.", prefix=pname)
            except Exception as exc:
                log(f"Note: could not finish activation: {exc}", prefix=pname)
            try:
                bal = context.client.get_balance()
                self._note_balance(context.provider_name, bal)
            except Exception:
                pass
            try:
                self.bot.return_to_menu()
            except Exception:
                pass
            self.bot_at_number_prompt = False
            self.bot_change_attempts = 0

            self.state.save({
                "status": "ACCOUNT_LINKED",
                "provider": context.provider_name,
                "activation_id": context.activation_id,
                "number": context.clean_number,
                "otp_code": code,
                "meesho_user_id": res.get("user_id"),
                "meesho_account_number": res.get("account_number"),
                "linked_at": now()
            })
            self.notify.alert(
                f"🎉 [{pname}] Account linked! (#{n_linked})",
                f"Number: {context.clean_number}\n"
                f"Meesho User ID: {res.get('user_id') or 'n/a'}\n"
                f"Bot account #{res.get('account_number') or 'n/a'}\n"
                f"OTP: {code}{' (late salvage)' if late else ''}\n\n{totals}"
            )
            return "linked"

        if status == "wrong_otp":
            n = self.stats.increment("otp_wrong")
            self.notify.alert(
                f"❌ [{pname}] Wrong OTP (#{n})",
                f"Number: {context.clean_number}\nCode rejected: {code}\n"
                f"Will change number and retry.\n\n{totals}"
            )
        elif status == "otp_expired":
            n = self.stats.increment("otp_expired")
            self.notify.alert(
                f"⌛ [{pname}] OTP expired (#{n})",
                f"Number: {context.clean_number}\nWill change number and retry.\n\n{totals}"
            )
        elif status == "blocked":
            n = self.stats.increment("user_blocked")
            self.notify.alert(
                f"🚫 [{pname}] User blocked (#{n})",
                f"Number: {context.clean_number} was blocked during verification.\n"
                f"Will change number and retry.\n\n{totals}"
            )
        else:
            self.notify.alert(
                f"⚠️ [{pname}] Unconfirmed bot result: {status}",
                f"Number: {context.clean_number}\nCode: `{code}`\n"
                f"Screen:\n{res.get('screen', '')[:500]}\nWill change number and retry."
            )
        return status

    def _bot_prepare_change_number(self, context):
        """
        Tell the PRIMES bot to Change Number so the next found number is sent
        straight to the number prompt. Cancels/refunds the provider activation
        via the caller. Returns True if the bot now awaits a new number.
        """
        pname = context.provider_name.upper()
        self.bot_change_attempts += 1
        if self.bot_change_attempts > self.bot.max_change_number:
            log("Change Number attempt cap reached; resetting bot to main menu.", prefix=pname)
            try:
                self.bot.cancel_flow()
            except Exception:
                pass
            self.bot_at_number_prompt = False
            self.bot_change_attempts = 0
            return False

        try:
            res = self.bot.change_number(None)
            self.stats.increment("change_number")
            if res.get("stage") in ("prompt", "needs_full_flow"):
                self.bot_at_number_prompt = res.get("stage") == "prompt"
                log(f"Bot ready for replacement number (attempt {self.bot_change_attempts}).", prefix=pname)
                return self.bot_at_number_prompt
        except MeeshoBotError as exc:
            log(f"Change Number failed in bot: {exc}; full flow will restart.", prefix=pname)
            try:
                self.bot.cancel_flow()
            except Exception:
                pass
            self.bot_at_number_prompt = False
        return False

    def wait_for_manual_trigger(self, context):
        wait_seconds = self.settings.get("trigger_wait_seconds", 300)
        poll_interval = self.settings.get("trigger_poll_interval_seconds", 2)

        self.state.save({
            "status": "AWAITING_MANUAL_TRIGGER",
            "provider": context.provider_name,
            "activation_id": context.activation_id,
            "number": context.clean_number,
            "awaiting_since": now()
        })

        last_probe = [time.time()]

        def probe_provider():
            if time.time() - last_probe[0] < 10:
                return None
            last_probe[0] = time.time()
            try:
                st = context.client.get_status(context.activation_id)
                if st.get("type") == "STATUS_OK":
                    log("OTP arrived before confirmation - proceeding immediately.", prefix=context.provider_name.upper())
                    return "go"
                if st.get("type") == "STATUS_CANCEL":
                    log("Activation cancelled remotely while waiting.", prefix=context.provider_name.upper())
                    return "skip"
            except Exception:
                pass
            return None

        return self.notify.wait_for_trigger(
            number=context.clean_number,
            activation_id=context.activation_id,
            provider_name=context.provider_name,
            timeout=wait_seconds,
            poll_interval=poll_interval,
            on_tick=probe_provider
        )

    def wait_for_otp(self, context):
        """
        Poll the provider for the SMS.

        Returns (kind, status):
          ("ok", status)        - OTP received in time
          ("late", status)      - OTP found by the final salvage probes after timeout
          ("cancelled", None)   - provider cancelled the activation
          ("timeout", None)     - no OTP (and no late salvage)
        """
        timeout = self.settings.get("otp_timeout_seconds", 180)
        poll_interval = self.settings.get("otp_poll_interval_seconds", 3)
        start_time = time.time()
        pname = context.provider_name.upper()

        log(f"Waiting for OTP on {context.clean_number} ({pname}) (Timeout: {timeout}s)...", prefix=pname)
        last_log = 0

        while (time.time() - start_time) < timeout and not self.stop_requested.is_set():
            elapsed = int(time.time() - start_time)
            try:
                status_res = context.client.get_status(context.activation_id)
            except Exception as exc:
                log(f"Transient status error: {exc}", prefix=pname)
                time.sleep(poll_interval)
                continue

            status_type = status_res.get("type")

            if status_type == "STATUS_OK":
                code = status_res.get("code") or status_res.get("sms", "")
                log(f"🎉 OTP Received! Code: {code}", prefix=pname)
                log(f"Full SMS Content: {status_res.get('sms', '')}", prefix=pname)
                self.state.save({
                    "status": "OTP_RECEIVED",
                    "provider": context.provider_name,
                    "activation_id": context.activation_id,
                    "number": context.clean_number,
                    "otp_code": code,
                    "sms": status_res.get("sms", ""),
                    "received_at": now()
                })
                return "ok", status_res

            if status_type == "STATUS_CANCEL":
                log("Activation was cancelled remotely.", prefix=pname)
                return "cancelled", None

            if status_type == "STATUS_WAIT_CODE":
                if elapsed - last_log >= 15:
                    log(f"Waiting for OTP ({elapsed}s/{timeout}s)...", prefix=pname)
                    last_log = elapsed
            time.sleep(poll_interval)

        # FINAL SALVAGE: the SMS can land in the seconds between timeout and a
        # cancel call. Probe a few times before declaring the number dead.
        log(f"OTP wait window elapsed ({timeout}s). Running final salvage probes...", prefix=pname)
        late = self.guard.salvage_late_otp(
            context.client, context.activation_id,
            probes=self.settings.get("timeout_salvage_probes", 3),
            delay=self.settings.get("timeout_salvage_delay", 2.0),
            prefix=pname,
        )
        if late:
            self.stats.increment("late_otp_salvaged")
            code = late.get("code")
            self.notify.alert(
                f"🆘 [{pname}] Late OTP salvaged",
                f"Number: {context.clean_number}\nCode arrived right at timeout: `{code}`\n"
                f"Attempting to use it..."
            )
            return "late", late

        self.stats.increment("otp_timeout")
        log("OTP timed out; no salvageable code.", prefix=pname)
        return "timeout", None

    # -- Orchestration Run Method --------------------------------------------

    def run(self):
        self.is_running = True
        self.stop_requested.clear()
        self.target_found_event.clear()
        self.total_attempts = 0
        self.bot_at_number_prompt = False
        self.bot_change_attempts = 0
        self._reset_checker_failures()

        log("=" * 60)
        log("STARTING PARALLEL MEESHO OTP AUTOMATION")
        log(f"Active Providers: {', '.join(c.name.upper() for c in self.clients)}")

        # Start the PRIMES userbot (optional; falls back to manual trigger).
        if self.bot.enabled:
            started = self.bot.start()
            if started and self.bot.ready:
                log(f"PRIMES bot automation ENABLED for {self.bot.bot_username}")
                log(f"Referral step: {self.bot.referral_summary}")
                if self.bot.referral_required and not self.bot.referral_link:
                    log("WARNING: no referral link is configured. The referral screen "
                        "may not appear at all (the flow then runs normally), but if it "
                        "does appear the automation will stop, report it and cancel the "
                        "number. Set it with Telegram /referral <link> or "
                        "python main.py --set-referral-link <link>, or set "
                        "meesho_bot.referral_failure_action to \"skip\" to log in without "
                        "a link.")
            else:
                log(f"PRIMES bot automation unavailable ({self.bot.start_error}); using manual trigger flow.")
                if not self._bot_warned:
                    self._bot_warned = True
                    self.notify.alert(
                        "PRIMES bot automation inactive",
                        f"{self.bot.start_error}\n\nFalling back to the manual OTP trigger flow.\n"
                        "Run login_userbot.py and check meesho_bot config to enable full automation."
                    )
        use_bot = self.bot.ready
        log(f"PRIMES bot flow: {'AUTO' if use_bot else 'MANUAL TRIGGER'}")
        log(f"Checker: {self.checker.describe()}")

        # Checker mode sanity. "bot" cannot work without the userbot, and
        # starting the workers anyway would buy numbers only to cancel every
        # one of them - so don't start at all. "auto" just degrades to the old
        # API-only behaviour on API errors.
        if self.checker.mode_wants_bot and not self.bot.ready:
            if self.checker.mode == MODE_BOT:
                reason = self.bot.start_error or "meesho_bot is not enabled/configured"
                log(f"Checker mode is BOT but the PRIMES userbot is not ready ({reason}). "
                    f"Not starting: every number would be bought and immediately "
                    f"cancelled. Fix meesho_bot / run login_userbot.py, or set "
                    f"checker.mode to 'api' or 'auto'.")
                self.stop_requested.set()
                self.is_running = False
                self.notify.alert(
                    "🛑 Checker mode 'bot' cannot run",
                    f"checker.mode is \"bot\" but the PRIMES userbot is not ready:\n"
                    f"{reason}\n\n"
                    "No workers were started (each bought number would only be "
                    "cancelled). Fix meesho_bot (enabled, api_id/api_hash, "
                    "bot_username, userbot.session.txt) or set checker.mode to "
                    '"api" or "auto" and run again.'
                )
                return
            log("Checker mode AUTO: the PRIMES bot fallback is not ready, so an API "
                "error will cancel the number as before (enable meesho_bot for the "
                "fallback).")
        log("=" * 60)

        for client in self.clients:
            try:
                bal = client.get_balance()
                log(f"[{client.name.upper()}] Initial Balance: {bal:.4f}")
            except Exception as exc:
                log(f"[{client.name.upper()}] Warning: Balance fetch failed: {exc}")

        workers = []
        for client in self.clients:
            t = threading.Thread(target=self.worker_loop, args=(client,), name=f"Worker-{client.name}", daemon=True)
            workers.append(t)
            t.start()

        try:
            while not self.stop_requested.is_set():
                alive_workers = [t for t in workers if t.is_alive()]
                if not alive_workers and not self.target_found_event.is_set():
                    all_reached_max = all(
                        self.worker_attempts.get(c.name, 0) >= self.worker_max_attempts.get(c.name, 200)
                        for c in self.clients
                    )
                    if all_reached_max:
                        summary_att = ", ".join(
                            f"{c.name.upper()}: {self.worker_attempts.get(c.name, 0)}/{self.worker_max_attempts.get(c.name, 200)}"
                            for c in self.clients
                        )
                        log(f"All workers reached configured max attempts ({summary_att}).")
                        self.notify.alert("Automation Finished",
                                          f"All workers finished their max attempts ({summary_att}).\n\n"
                                          f"{self.stats.summary()}")
                    else:
                        log("All worker threads have stopped.")
                        self.notify.alert(
                            "⚠️ All OTP Workers Stopped",
                            "All worker threads have stopped (insufficient balance, refund mismatch, or fatal errors).\n\n"
                            "• Send /balance to check balances\n• Send /run to restart"
                        )
                    break

                if self.target_found_event.wait(timeout=1.0):
                    with self.active_target_lock:
                        target = self.active_target
                    if not target:
                        continue

                    self.stats.increment("targets_found")
                    log(f"\n>>> PROCESSING TARGET: {target.clean_number} ({target.provider_name.upper()}) <<<\n")
                    try:
                        cont = self._process_target(target, use_bot)
                    except Exception as exc:
                        # Last-resort net: nothing may ever crash the whole
                        # automation run again (a bare TimeoutError from the
                        # userbot once did). Report, cancel the activation
                        # with a refund tally, reset the bot, keep going.
                        pname = target.provider_name.upper()
                        log(f"Unexpected error while processing {target.clean_number}: {exc}",
                            prefix=pname)
                        log(traceback.format_exc(), prefix=pname)
                        self.notify.alert(
                            f"🛑 [{pname}] Unexpected error - target skipped",
                            f"Number: {target.clean_number}\nError: {exc}\n\n"
                            "The activation is cancelled and the refund tally checked; "
                            "the search continues with the next number."
                        )
                        if use_bot:
                            try:
                                self.bot.cancel_flow()
                            except Exception:
                                pass
                            self.bot_at_number_prompt = False
                            self.bot_change_attempts = 0
                        try:
                            self.handle_cancellation(
                                target.client, target.activation_id, target.clean_number,
                                f"Unexpected error: {exc}", expect_refund=True,
                            )
                        except Exception as exc2:
                            log(f"Cancellation after the unexpected error failed too: {exc2}",
                                prefix=pname)
                        self._clear_target()
                        cont = "continue"
                    if cont == "stop":
                        break

        finally:
            self.stop_requested.set()
            for t in workers:
                t.join(timeout=2.0)
            if self.bot.ready:
                self.bot.stop()
            self.is_running = False
            log("Automation run finished.")

    def _clear_target(self):
        with self.active_target_lock:
            self.active_target = None
        self.target_found_event.clear()

    def _process_target(self, target, use_bot):
        """
        Drive one found number through trigger -> OTP -> link/recovery.
        Returns "continue" (search again) or "stop".
        """
        # --- Step 1: trigger the OTP (auto via bot, or manual gate) --------
        if use_bot:
            res = self._bot_send_number(target, from_prompt=self.bot_at_number_prompt)
            if res is None:
                self._clear_target()
                return "continue" if not self.stop_requested.is_set() else "stop"
        else:
            if self.settings.get("require_manual_trigger", True):
                decision = self.wait_for_manual_trigger(target)
                if decision == "skip":
                    self.notify.send("Number Skipped",
                                     f"⏭ Number {target.clean_number} skipped; continuing search...")
                    self.handle_cancellation(target.client, target.activation_id, target.clean_number, "Skipped by user")
                    self._clear_target()
                    return "continue" if not self.stop_requested.is_set() else "stop"
                if decision == "timeout":
                    self.notify.alert("Trigger Timed Out",
                                      f"No trigger confirmation for {target.clean_number}; cancelling.")
                    self.handle_cancellation(target.client, target.activation_id, target.clean_number,
                                             "Manual trigger timed out")
                    self._clear_target()
                    return "continue" if not self.stop_requested.is_set() else "stop"
                self.notify.send("Trigger Confirmed",
                                 f"✅ OTP triggered for {target.clean_number}; waiting for SMS...")
            else:
                self.notify.alert("Target Number Found",
                                  f"Number: {target.clean_number}\nProvider: {target.provider_name}")

        # --- Step 2: wait for the OTP from the provider ---------------------
        kind, status_res = self.wait_for_otp(target)
        pname = target.provider_name.upper()

        if kind in ("ok", "late"):
            sms = status_res.get("sms", "")
            code = status_res.get("code") or sms
            self.stats.increment("otp_received")

            if use_bot:
                bot_status = self._bot_submit_code(target, code, sms, late=(kind == "late"))
                if bot_status == "linked":
                    if self.settings.get("stop_after_success", False):
                        log("Account linked and stop_after_success=true; stopping.")
                        self.stop_requested.set()
                        return "stop"
                    log("Account linked. Resuming search for next number...")
                    self._clear_target()
                    return "continue"
                # wrong / expired / blocked / unknown after code delivery: the
                # SMS was consumed, so the charge legitimately stands (no refund).
                log(f"Bot result '{bot_status}' for {target.clean_number}; recovering with new number.")
                self._recover_change_number(target, f"bot_{bot_status}", expect_refund=False)
                self._clear_target()
                return "continue" if not self.stop_requested.is_set() else "stop"

            # Manual mode: surface the OTP and finish (historical behavior).
            self.notify.otp_result(code, target.clean_number, sms, provider_name=target.provider_name)
            if self.settings.get("auto_finish_activation", True):
                try:
                    target.client.finish(target.activation_id)
                except Exception as exc:
                    log(f"Note: could not finish activation: {exc}")
            try:
                self._note_balance(target.provider_name, target.client.get_balance())
            except Exception:
                pass
            self.stop_requested.set()
            return "stop"

        if kind == "cancelled":
            self.notify.alert(
                f"⚠️ [{pname}] activation cancelled",
                f"Activation {target.activation_id} ({target.clean_number}) was cancelled remotely.\n"
                "Checking refund and continuing..."
            )
            self.handle_cancellation(target.client, target.activation_id, target.clean_number,
                                     "Activation cancelled remotely")
            self._clear_target()
            return "continue" if not self.stop_requested.is_set() else "stop"

        # timeout
        if use_bot:
            self._recover_change_number(target, "otp_timeout")
        else:
            self.notify.alert(
                f"⚠️ [{pname}] OTP Timed Out",
                f"No OTP for {target.clean_number}. Cancelling for refund and continuing..."
            )
            self.handle_cancellation(target.client, target.activation_id, target.clean_number, "OTP timeout")
        self._clear_target()
        return "continue" if not self.stop_requested.is_set() else "stop"

    def _recover_change_number(self, target, reason, expect_refund=True):
        """
        OTP missing/rejected recovery. The ORDER is safety-critical:

        1. Cancel the provider activation FIRST (with the late-OTP salvage
           race and the refund tally) while the PRIMES bot is still on its
           OTP-wait screen - nothing has moved it yet.
        2. If the cancellation salvages a late OTP (the SMS landed inside the
           cancel race), it is submitted to the bot immediately - the bot
           still waits for the code - and the alert with the code has already
           gone out, so it can also be entered manually if the auto-submit
           fails. The charge stands either way: the SMS was delivered.
        3. Only when the cancellation is clean (refund tallied / charge
           stands, no salvaged OTP, automation not stopping) does the bot tap
           Change Number and the workers resume hunting.

        The old order tapped Change Number first: when the OTP then arrived
        during the cancellation race, the screen to enter it was already
        gone - money spent, no refund, account not added, and even manual
        entry was impossible.
        """
        pname = target.provider_name.upper()

        # --- 1) cancel the provider activation while the bot is untouched --
        log(f"Cancelling {target.activation_id} for recovery ({reason}, "
            f"refund expected: {expect_refund})...", prefix=pname)
        result = self.handle_cancellation(
            target.client, target.activation_id, target.clean_number,
            f"Recovery: {reason}", expect_refund=expect_refund
        ) or {}
        salvaged = result.get("salvaged")

        # --- 2) salvaged late OTP: use it while the screen is still there ---
        if salvaged and salvaged.get("code") and self.bot.ready:
            code = salvaged.get("code")
            sms = salvaged.get("sms", "")
            self.stats.increment("otp_received")
            try:
                state = self.bot.screen_state()
            except Exception as exc:
                log(f"Could not check the bot screen for the salvaged OTP ({exc}).",
                    prefix=pname)
                state = None
            if state == S_OTP_WAIT:
                log(f"Salvaged OTP {code} for {target.clean_number}; the bot is "
                    f"still waiting for the code - submitting it automatically.",
                    prefix=pname)
                status = self._bot_submit_code(target, code, sms, late=True)
                if status == "linked":
                    if self.settings.get("stop_after_success", False):
                        log("Account linked from a salvaged OTP and "
                            "stop_after_success=true; stopping.")
                        self.stop_requested.set()
                    else:
                        log("Account linked from a salvaged OTP; resuming the search.",
                            prefix=pname)
                    return
                # Not linked (wrong/expired/unknown): _bot_submit_code already
                # alerted with the code; the charge stands - fall through to
                # Change Number and hunt the next number.
            else:
                # The bot moved on by itself (e.g. its code prompt expired):
                # the salvaged code cannot be auto-submitted. Say so loudly -
                # manual entry is only possible while a prompt still exists.
                self.notify.alert(
                    f"⚠️ [{pname}] Salvaged OTP could not be auto-submitted",
                    f"Number: {target.clean_number}\nCode: `{code}`\n"
                    f"Current bot screen: {state or 'unknown'}\n\n"
                    "The SMS was delivered (the charge stands), but the bot is "
                    "no longer waiting for a code. If the bot still shows a "
                    "code prompt anywhere, enter it manually NOW."
                )

        # --- 3) clean cancellation: now move the bot to the number prompt ---
        if self.stop_requested.is_set():
            # A refund mismatch (or another critical stop) halted the
            # automation. The bot is deliberately LEFT on its OTP screen so
            # the number can still be completed manually if the OTP turns up
            # in the provider panel - moving it away now would burn the paid
            # SMS for good.
            log("Automation is stopping after the cancellation; the bot is left "
                "on its OTP screen so a late OTP can still be entered manually.",
                prefix=pname)
            self.bot_at_number_prompt = False
            self.bot_change_attempts = 0
            return

        prompt_ready = False
        if self.bot.ready:
            prompt_ready = self._bot_prepare_change_number(target)
            self.notify.send(
                "🔄 Changing number",
                (f"{target.clean_number} ({reason}). The bot is waiting for the replacement number."
                 if prompt_ready else
                 f"{target.clean_number} ({reason}). The bot flow will restart from the menu.")
            )


def load_config():
    for config_name in CONFIG_FILES:
        if os.path.exists(config_name):
            try:
                with open(config_name, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                log(f"Error reading {config_name}: {exc}")
    raise FileNotFoundError(f"Neither {', '.join(CONFIG_FILES)} could be found.")


def set_referral_link(config_path, link):
    """
    Write meesho_bot.referral_link into config.json in place, keeping the rest
    of the file (comments cannot exist in JSON, but blank lines/ordering can)
    untouched. Returns True when the file was updated.
    """
    import re

    with open(config_path, "r", encoding="utf-8") as f:
        text = f.read()

    block = re.search(r'"meesho_bot"\s*:\s*\{', text)
    if not block:
        raise ValueError(f'no "meesho_bot" section found in {config_path}')

    # Find the end of the meesho_bot object by brace counting.
    depth = 1
    index = block.end()
    while index < len(text) and depth:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    if depth:
        raise ValueError(f'unbalanced braces in the "meesho_bot" section of {config_path}')

    inner_start, inner_end = block.end(), index - 1
    inner = text[inner_start:inner_end]
    quoted = json.dumps(link)

    existing = re.search(r'("referral_link"\s*:\s*)("(?:[^"\\]|\\.)*")', inner)
    if existing:
        new_inner = inner[:existing.start(2)] + quoted + inner[existing.end(2):]
    else:
        new_inner = '\n    "referral_link": ' + quoted + "," + inner

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(text[:inner_start] + new_inner + text[inner_end:])
    return True


def set_checker_mode(config_path, mode):
    """
    Write "mode" into the "checker" section of config.json in place, keeping
    the rest of the file (ordering, other keys) untouched. Returns True.
    """
    import re

    mode = normalize_mode(mode, default=None)
    if mode is None:
        raise ValueError(f"unknown checker mode: {mode!r} (use api, bot or auto)")

    with open(config_path, "r", encoding="utf-8") as f:
        text = f.read()

    block = re.search(r'"checker"\s*:\s*\{', text)
    if not block:
        raise ValueError(f'no "checker" section found in {config_path}')

    depth = 1
    index = block.end()
    while index < len(text) and depth:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    if depth:
        raise ValueError(f'unbalanced braces in the "checker" section of {config_path}')

    inner_start, inner_end = block.end(), index - 1
    inner = text[inner_start:inner_end]
    quoted = json.dumps(mode)

    existing = re.search(r'("mode"\s*:\s*)("(?:[^"\\]|\\.)*")', inner)
    if existing:
        new_inner = inner[:existing.start(2)] + quoted + inner[existing.end(2):]
    else:
        new_inner = '\n    "mode": ' + quoted + "," + inner

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(text[:inner_start] + new_inner + text[inner_end:])
    return True


def main():
    parser = argparse.ArgumentParser(description="Meesho OTP automation with parallel provider clients.")
    parser.add_argument("--provider",
                        help="Provider(s): tempora, vsimpro, otpdoctor, otpcart, 'all', or comma-combinations "
                             "e.g. 'tempora,vsimpro'")
    parser.add_argument("--balance", action="store_true", help="Print live balances for all providers and exit")
    parser.add_argument("--daemon", action="store_true", help="Keep Telegram command listener alive after runs")
    parser.add_argument("--set-referral-link",
                        help="Save this Meesho referral link into config.json "
                             "(meesho_bot.referral_link) and exit")
    parser.add_argument("--no-referral-link",
                        action="store_true",
                        help="Clear meesho_bot.referral_link in config.json and exit "
                             "(the bot's own 'I don't have a refer code' option is used)")
    parser.add_argument("--checker-mode",
                        choices=["api", "bot", "auto"],
                        help="Number checker to use: 'api' (checker API only), "
                             "'bot' (PRIMES bot checker only) or 'auto' (API first, "
                             "PRIMES bot when the API is down / too slow / rejects "
                             "every key). Saved into config.json and exit.")
    parser.add_argument("--checker-status",
                        action="store_true",
                        help="Print the configured checker mode and exit")

    args = parser.parse_args()

    if args.checker_mode:
        config_path = next((n for n in CONFIG_FILES if os.path.exists(n)), None)
        if not config_path:
            log(f"Fatal: neither {', '.join(CONFIG_FILES)} could be found.")
            return
        try:
            set_checker_mode(config_path, args.checker_mode)
        except Exception as exc:
            log(f"Fatal: could not update {config_path}: {exc}")
            return
        log(f"checker.mode set to \"{args.checker_mode}\" in {config_path}.")
        if args.checker_mode == "bot":
            log("The checker will use the PRIMES bot only; it needs meesho_bot "
                "enabled + configured, and every check fails until the userbot is ready.")
        elif args.checker_mode == "auto":
            log("The checker will use the API while it works and switch to the "
                "PRIMES bot on API errors - the bot needs meesho_bot enabled + configured.")
        return

    if args.set_referral_link or args.no_referral_link:
        config_path = next((n for n in CONFIG_FILES if os.path.exists(n)), None)
        if not config_path:
            log(f"Fatal: neither {', '.join(CONFIG_FILES)} could be found.")
            return
        link = (args.set_referral_link or "").strip()
        if link and not (link.startswith("http") and "meesho" in link):
            log(f"Fatal: '{link}' does not look like a Meesho referral link "
                "(it should start with http and contain app.meesho.com).")
            return
        try:
            set_referral_link(config_path, link)
        except Exception as exc:
            log(f"Fatal: could not update {config_path}: {exc}")
            return
        log(f"meesho_bot.referral_link {'set to ' + link if link else 'cleared'} in {config_path}.")
        log("The referral screen is now answered automatically: " +
            ("link pasted once per login, then the bot's own skip option." if link else
             "the bot's own 'I don't have a refer code' option."))
        return

    try:
        config = load_config()
    except Exception as exc:
        log(f"Fatal: {exc}")
        return

    coordinator = ParallelAutomationCoordinator(config, provider_override=args.provider)

    if args.balance:
        print(coordinator.get_balances_summary())
        return

    if args.checker_status:
        print(coordinator.checker_status_text())
        return

    coordinator.run()

    if coordinator.notify.telegram.configured:
        log("Listening for Telegram commands (/run, /status, /balance, /stop). Press Ctrl+C to exit.")
        try:
            while True:
                coordinator.notify.telegram.poll_signal({"run": "run", "stop": "stop"})
                time.sleep(2)
        except KeyboardInterrupt:
            log("Exiting.")


if __name__ == "__main__":
    main()
