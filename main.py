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
    BotBusy,
    CheckerBotBusy,
    CheckerRouter,
    MODE_BOT,
    MODE_AUTO,
    normalize_mode
)
from state import StateStore
from stats import StatsStore
from balance_guard import BalanceGuard
from runtime import (
    apply_instance_overrides,
    pending_filename,
    resolve_instance,
    state_filename,
    stats_filename,
)
from cancel_watch import (
    CANCEL_RETRY_TYPES,
    CancelWatchManager,
    PendingCancelStore,
    resolve_settings as resolve_cancel_settings,
)
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


class _BotClaim:
    """
    Exclusive claim on the PRIMES bot conversation (see the coordinator).

    One Telegram chat with the bot is shared by three kinds of work:

      * "login"  - driving a paid number through login -> OTP -> link;
      * "check"  - asking the bot whether a number is registered;
      * "prewarm"- parking the bot on an agreed offer before a number exists.

    Only one may run at a time, and a login outranks the rest: a number check
    that starts while a login waits for its OTP walks the bot back to its main
    menu and types the number to check into the checker prompt - the paid
    number's OTP screen is gone, the OTP (if it lands) cannot be entered and
    the money is wasted. So a non-login claim never queues behind a login:
    it either waits a bounded time for the bot to become free, or reports
    "busy" and the number is cancelled with a refund instead.
    """

    def __init__(self, coordinator, owner, timeout=0.0, wait=False):
        self.coordinator = coordinator
        self.owner = owner
        self.timeout = max(0.0, float(timeout or 0.0))
        self.wait = bool(wait)
        self.held = False

    def __enter__(self):
        coord = self.coordinator
        lock = coord._bot_claim_lock
        if not self.wait:
            acquired = lock.acquire(blocking=False)
        else:
            acquired = lock.acquire(blocking=True, timeout=self.timeout)
        if not acquired:
            raise BotBusy(
                f"the PRIMES bot is busy (owner: {coord._bot_claim_owner or 'unknown'})"
            )
        try:
            if self.owner == "login":
                coord._bot_login_depth += 1
                coord.bot_login_active = True
                coord._bot_claim_owner = "login"
            elif coord.bot_login_active:
                raise BotBusy("a login flow is using the PRIMES bot")
            else:
                coord._bot_claim_owner = self.owner
            self.held = True
            return coord
        except Exception:
            lock.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        coord = self.coordinator
        if not self.held:
            return False
        self.held = False
        try:
            if self.owner == "login":
                coord._bot_login_depth = max(0, coord._bot_login_depth - 1)
                if coord._bot_login_depth == 0:
                    coord.bot_login_active = False
            if coord._bot_login_depth == 0:
                coord._bot_claim_owner = None
        finally:
            coord._bot_claim_lock.release()
        return False


class ParallelAutomationCoordinator:

    def __init__(self, config, provider_override=None, instance=None):
        self.config = config
        self.provider_override = provider_override

        # Parallel runs (one Termux tab per provider) get their own instance
        # name: it namespaces stats/state/pending-cancel files and the signal
        # directory, and merges an optional config["instances"][name] block.
        self.instance = resolve_instance(instance, provider_override)
        # A parallel run may bring its own settings (userbot session, Telegram
        # command bot, provider selection): config["instances"][name] is merged
        # over the shared config for this instance only.
        config = apply_instance_overrides(config, self.instance)
        self.config = config

        self.checker_conf = config.get("checker", {})
        self.settings = config.get("automation", {})

        self.state = StateStore(filename=state_filename(self.instance))
        self.stats = StatsStore(filename=stats_filename(self.instance))
        self.guard = BalanceGuard(config.get("balance_guard", {}), log_fn=log)
        self.notify = Notifier(config, instance=self.instance)
        self.bot = MeeshoBotClient(config, log_fn=log)

        # Number checking strategy: API only, PRIMES bot only, or API with an
        # automatic bot fallback (checker.mode in config.json - see
        # SETUP_CHECKER.md). The bot is looked up lazily so replacing
        # self.bot at runtime (tests) is picked up.
        # Dedicated checker bot (config "checker" -> "telegram_bot"): the same
        # logged-in Telegram account drives a SECOND bot conversation, so a
        # number check never walks the PRIMES login bot out of a waiting OTP
        # screen. It never takes the login claim; PRIMES is only the last
        # resort when nothing else can answer.
        self._dedicated_checker_client = None
        checker_bot_conf = config.get("checker", {}).get("telegram_bot") or {}
        checker_bot_username = (
            checker_bot_conf.get("username") or checker_bot_conf.get("bot_username") or ""
        )
        if checker_bot_conf.get("enabled", False) is False:
            # Disabled dedicated checker bot: do not build the client.
            checker_bot_username = ""
        if checker_bot_username:
            try:
                from meesho_bot_client import CheckerBotClient
                self._dedicated_checker_client = CheckerBotClient(
                    self.bot,
                    username=checker_bot_username,
                    conf=checker_bot_conf,
                    log_fn=log,
                )
            except Exception as exc:
                log(f"Checker: could not set up the dedicated checker bot "
                    f"({exc}); it is not available.")
                self._dedicated_checker_client = None

        self.checker = CheckerRouter(
            config,
            bot_getter=lambda: self.bot,
            log_fn=log,
            stats=self.stats,
            gate=self._bot_check_allowed,
            claim=self._bot_check_claim,
            checker_bot_getter=(lambda: self._dedicated_checker_client)
                if self._dedicated_checker_client is not None else None,
            checker_bot_username=checker_bot_username or None,
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

        # One PRIMES conversation, claimed exclusively (see _BotClaim): a login
        # flow owns the bot, so a number check can never be typed into - or
        # walk the bot away from - a login that is waiting for its OTP.
        self._bot_claim_lock = threading.RLock()
        self._bot_claim_owner = None
        self._bot_login_depth = 0
        self.bot_login_active = False
        # Offer pre-warm: the bot is walked to an agreed offer while the
        # workers are still hunting, so a found number is sent straight away
        # instead of waiting for Add Account -> ... -> offer rerolls.
        self.bot_warm = {"ready": False, "upi": None, "at": 0.0, "rerolls": 0}
        self.prewarm_enabled = bool(self.settings.get("prewarm_offer", True))
        self.warm_refresh_seconds = self._warm_refresh_seconds()

        # Deferred cancellations: a provider that refuses a cancel with ERROR
        # (TemporaSMS / VSImpro) keeps the activation (and the money) open, so
        # the cancel is retried in the background at activation expiry while
        # the worker keeps hunting - see cancel_watch.py.
        self.pending_cancels = CancelWatchManager(
            self,
            store=PendingCancelStore(filename=pending_filename(self.instance)),
            log_fn=log,
        )
        # Providers whose refund tally is suspended because a deferred
        # cancellation holds an unknown amount (the balance could not be read
        # when the cancel was deferred). Suspending beats critical-stopping on
        # a difference that is fully explained by the pending refund.
        self._tally_suspended = set()
        self._tally_suspension_lock = threading.Lock()

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
        """
        The balance this provider should be back at once the activation is
        refunded - minus the money still held by deferred cancellations.

        Without the deduction, a number bought while a refused cancel is still
        waiting for expiry would always look like a refund mismatch (the
        deferred activation's price is still missing from the balance), which
        critical-stopped the run over a timing problem.
        """
        with self.ledger_lock:
            entry = self.ledger.get(name, {})
            base = entry.get("expected_balance")
        if base is None:
            return None
        hold, _unknown = self.pending_cancels.hold_total(name, exclude=activation_id)
        return base - hold

    # -- hooks for the deferred-cancellation watcher (cancel_watch.py) -------

    def client_by_name(self, name):
        for client in self.clients:
            if client.name == name:
                return client
        return None

    def expected_balance(self, name, activation_id=None):
        return self._expected_balance(name, activation_id)

    def note_balance(self, name, balance):
        return self._note_balance(name, balance)

    def critical_stop(self, title, message):
        return self._critical_stop(title, message)

    def pending_cancel_hold(self, name=None, exclude=None):
        """(amount still held, whether any of it is unknown) - see cancel_watch."""
        return self.pending_cancels.hold_total(name, exclude=exclude)

    def _suspend_tally(self, name, reason):
        """
        Stop running refund tallies for `name` until its deferred cancellations
        are resolved: the balance holds money whose amount is unknown, so any
        comparison would be guesswork (and a guessed mismatch stops the run).
        """
        with self._tally_suspension_lock:
            fresh = name not in self._tally_suspended
            self._tally_suspended.add(name)
        if fresh:
            log(f"Refund tally suspended for {name.upper()} ({reason}); it resumes "
                f"when the pending cancellation is resolved.", prefix=name.upper())
            self.notify.alert(
                f"⏳ [{name.upper()}] Refund tally suspended",
                f"{reason}\\n\\nUntil that money is back, cancellations on "
                f"{name.upper()} are not compared against the expected balance "
                f"(a comparison could not tell a missing refund from the pending "
                f"one). Everything else continues normally."
            )

    def _clear_tally_suspension(self, name):
        with self._tally_suspension_lock:
            self._tally_suspended.discard(name)

    def _tally_is_suspended(self, name):
        with self._tally_suspension_lock:
            return name in self._tally_suspended

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
        ]
        if self.instance:
            lines.append(f"Instance: {self.instance} (own stats / state / signals)")
        lines += [
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

        pending = self.pending_cancels.pending()
        if pending:
            lines.append("\n⏳ Cancellations deferred to activation expiry:")
            for record in pending:
                hold = record.get("hold")
                hold_text = "amount unknown" if hold is None else f"holding {float(hold):.4f}"
                lines.append(
                    f"  • {str(record.get('provider', '')).upper()}: {record.get('number')} "
                    f"- retried in {record.get('seconds_to_expiry', 0):.0f}s ({hold_text})"
                )

        s = self.stats.snapshot()
        lines.append(
            "\n📊 Totals\n"
            f"  Accounts linked: {s['accounts_linked']}\n"
            f"  Targets found: {s['targets_found']} | OTPs received: {s['otp_received']}\n"
            f"  Wrong OTP: {s['otp_wrong']} | Expired: {s['otp_expired']} | Blocked: {s['user_blocked']}\n"
            f"  OTP timeouts: {s['otp_timeout']} | Change-number: {s['change_number']} "
            f"(menu resets: {s.get('bot_menu_resets', 0)} | "
            f"kept in-flow: {s.get('bot_flow_kept', 0)})\n"
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
        bot = self.checker.bot
        if bot.has_preferred:
            lines.append(f"Dedicated checker bot (PRIMES-safe): {bot.describe_preferred()}")
        if self.checker.mode_wants_bot:
            if self.checker.bot_ready:
                lines.append("PRIMES bot checker: 🟢 ready")
            else:
                lines.append(f"PRIMES bot checker: ⚪ not ready "
                             f"({self.checker.bot.unavailable_reason or 'unknown reason'})")
        # Whether the login flow must be able to fall back to the main menu:
        # the bot checker works from there, the API checker does not need it.
        needed = self._bot_checker_needed()
        why = str(getattr(self.checker, "bot_check_reason", "") or "").strip()
        lines.append(
            f"Checks need the bot at its main menu: {'YES' if needed else 'NO'}"
            + (f" ({why})" if why else "")
            + ("\n→ a failed Change Number resets the bot to the menu"
               if needed else
               "\n→ a failed Change Number keeps the bot in-flow (no menu restart, "
               "no offer reroll)")
        )
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
        cancel_error = ""
        try:
            cancel_res = client.cancel(activation_id)
            log(f"Cancellation response: {cancel_res}", prefix=pname)
        except Exception as exc:
            cancel_error = str(exc)
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
                cancel_error = str(exc)
                log(f"Error while retrying cancellation {activation_id}: {exc}", prefix=pname)

        # A cancel the provider refuses (TemporaSMS / VSImpro answer a plain
        # {"type": "ERROR"} while the activation is young) is NOT a refund: the
        # activation - and the money - stay open. It is handed to a background
        # watcher that retries at activation expiry and reports a late OTP,
        # instead of running the refund tally now and critical-stopping the run
        # over money that is simply still pending.
        _probes = self.settings.get("cancel_salvage_probes", 2)
        if expect_refund and self._should_defer_cancel(cancel_res, cancel_error):
            # One last chance for an OTP that landed while the cancel was in
            # flight: it is used as usual (the charge stands) instead of being
            # parked behind a deferred cancellation.
            salvaged_now = None
            if _probes > 0:
                try:
                    salvaged_now = self.guard.salvage_late_otp(
                        client, activation_id,
                        probes=_probes,
                        delay=self.settings.get("cancel_salvage_delay", 1.5),
                        prefix=pname,
                    )
                except Exception:
                    salvaged_now = None
            if not salvaged_now:
                return self._defer_cancellation(
                    client, activation_id, number, reason, expected_balance,
                    cancel_res=cancel_res, cancel_error=cancel_error,
                )
            # The SMS arrived on a number whose cancel was refused: the charge
            # stands and the code is reported immediately.
            self.stats.increment("late_otp_salvaged")
            self.stats.increment("numbers_consumed")
            code = salvaged_now.get("code")
            sms = salvaged_now.get("sms", "")
            log(f"🚨 OTP arrived despite the refused cancel! Code: {code}", prefix=pname)
            self.notify.alert(
                f"🚨 [{pname}] OTP on a number whose cancel was refused",
                f"Number: {number}\nActivation: {activation_id}\nReason: {reason}\n\n"
                f"Code: `{code}`\nSMS: {sms}\n\n"
                "The provider refused the cancel, but the OTP arrived anyway - "
                "the SMS was delivered, so this charge stands (no refund) and "
                "the activation stays open. The PRIMES bot may still be waiting "
                "for this code."
            )
            refund_delay = self.settings.get("refund_check_delay_seconds", 2)
            if refund_delay > 0:
                time.sleep(refund_delay)
            try:
                actual_balance = client.get_balance()
                self._note_balance(client.name, actual_balance)
            except Exception:
                pass
            return {"tally_ok": True, "deferred": False, "salvaged": salvaged_now,
                    "balance": actual_balance}

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
            if _probes > 0:
                try:
                    salvaged = self.guard.salvage_late_otp(
                        client, activation_id,
                        probes=_probes,
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

                if expected_balance is not None and self._tally_is_suspended(client.name):
                    # A deferred cancellation on this provider is holding an
                    # amount that could not be measured, so the expected
                    # balance is a guess: re-baseline instead of stopping the
                    # run over a difference that is probably just that money.
                    try:
                        actual_balance = client.get_balance()
                        self._note_balance(client.name, actual_balance)
                    except Exception:
                        actual_balance = None
                    log(f"Refund tally skipped (suspended while a deferred "
                        f"cancellation is pending); new balance baseline: "
                        f"{actual_balance}", prefix=pname)
                    tally_ok = True
                else:
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

    # -- Deferred cancellations (provider refused the cancel) ----------------

    def _should_defer_cancel(self, cancel_res, cancel_error):
        """
        True when a cancel must be retried later instead of tallied now.

        TemporaSMS / VSImpro answer a cancel that arrives before the activation
        is old enough with a plain {"type": "ERROR"}: the activation stays open
        and the money stays deducted. Tallies against the balance right now can
        only fail, so the cancellation is deferred to the activation expiry.
        """
        if not self.settings.get("defer_refused_cancels", True):
            return False
        if cancel_res is None:
            # The cancel call itself failed (network / provider error): the
            # activation is almost certainly still open.
            return bool(cancel_error)
        return cancel_res.get("type") in CANCEL_RETRY_TYPES

    def _defer_cancellation(self, client, activation_id, number, reason,
                            expected_balance, cancel_res=None, cancel_error=""):
        """
        Hand a refused cancellation to the background watcher (cancel_watch.py)
        and let the caller carry on with the next number.
        """
        pname = client.name.upper()
        hold = None
        balance = None
        try:
            balance = float(client.get_balance())
        except Exception as exc:
            log(f"Could not read the balance for the deferred cancel: {exc}", prefix=pname)

        if balance is not None and expected_balance is not None:
            # How much of the expected balance is still tied up in this
            # activation: every later refund tally is measured against the
            # expected balance minus this hold.
            hold = max(0.0, round(float(expected_balance) - balance, 6))

        detail = " ".join(part for part in (str(cancel_error or ""), str(cancel_res or "")) if part)
        record = self.pending_cancels.defer(
            client, activation_id, number, reason, expected_balance,
            hold=hold, error_detail=detail,
        )

        if hold is None:
            self._suspend_tally(
                client.name,
                f"the deferred cancellation of {number} holds an amount that "
                f"could not be measured (the balance could not be read)"
            )

        settings = resolve_cancel_settings(self.settings)
        self.state.save({
            "status": "CANCEL_DEFERRED",
            "provider": client.name,
            "activation_id": activation_id,
            "number": number,
            "reason": reason,
            "expected_balance": expected_balance,
            "hold": hold,
            "retry_at": record.get("expiry_at"),
            "deferred_at": now()
        })
        self.notify.send(
            f"⏳ [{pname}] Cancel refused - deferred",
            f"Number: {number}\nActivation: {activation_id}\nReason: {reason}\n\n"
            f"The provider answered `{detail or 'ERROR'}`, so the activation is "
            f"still open. It is retried in ~{settings['cancel_error_expiry_seconds']:.0f}s "
            f"(activation expiry) in the background; this worker keeps hunting "
            f"and the refund is tallied after the retry."
            + ("" if hold else "\n\n⚠️ The balance could not be read, so the "
               "refund tally for this provider is suspended until it resolves.")
        )
        return {
            "tally_ok": True,
            "deferred": True,
            "salvaged": None,
            "balance": balance,
            "record": record,
        }

    def on_pending_resolved(self, provider):
        """A deferred cancellation finished: refresh the ledger bookkeeping."""
        _hold, unknown = self.pending_cancels.hold_total(provider)
        if unknown:
            self._suspend_tally(
                provider,
                "another deferred cancellation still holds an amount that could "
                "not be measured"
            )
        else:
            self._clear_tally_suspension(provider)
            remaining = self.pending_cancels.hold_total(provider)[0]
            log(f"Deferred cancellation resolved; remaining held on "
                f"{provider.upper()}: {remaining:.4f}", prefix=provider.upper())

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
            except CheckerBotBusy as exc:
                # The bot is busy with a login: the number is cancelled with a
                # refund (never typed into a screen that is waiting for an
                # OTP), and this does NOT count as a broken checker.
                log(f"Checker: the PRIMES bot is busy ({exc}); cancelling "
                    f"{clean_number} with a refund instead...", prefix=pname)
                self.stats.increment("bot_claims_denied")
                self.handle_cancellation(client, activation_id, clean_number,
                                         f"Checker busy: {exc}")
                if self.stop_requested.is_set():
                    return
                continue
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
            if checker_source == "bot":
                # A bot check navigates the bot (menu -> checker -> menu), so a
                # parked offer is gone: re-arm it before the next number.
                self._release_bot_warm()
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
        if from_prompt and not self._bot_at_prompt():
            # The coordinator believes the bot is waiting for a number, but it
            # is not on the prompt anymore: a bot number check resets the bot to
            # its main menu (checker.mode "bot" / an "auto" fallback), its prompt
            # may also have expired. Ask read-only and take the full flow
            # instead of typing a paid number into whatever is on screen now -
            # and instead of failing and cancelling the number. The full flow
            # reuses the prompt anyway when the bot happens to be on one.
            log("Bot is no longer at the number prompt; running the full flow "
                "instead of continuing from the prompt.", prefix=pname)
            from_prompt = False
            self.bot_at_number_prompt = False
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

    def _bot_at_prompt(self):
        """
        Read-only: is the bot sitting on the login number prompt right now?
        Never taps or types, so it is safe to ask between recovery steps.
        """
        try:
            probe = getattr(self.bot, "at_number_prompt", None)
            if callable(probe):
                return bool(probe())
            return self.bot.screen_state() == "offer"
        except Exception as exc:
            log(f"Could not read the bot screen: {exc}")
            return False

    def _bot_checker_needed(self):
        """
        True when the PRIMES bot (not the checker API) has to answer the next
        number check - and therefore has to sit on its main menu.
        """
        needed = getattr(self.checker, "bot_check_needed", None)
        if needed is None:
            # A checker without the property (older stubs): assume the bot is
            # needed whenever the mode wants it.
            return bool(getattr(self.checker, "mode_wants_bot", False))
        return bool(needed)

    def _bot_menu_reset_needed(self):
        """
        (reset?, why) for a Change Number that could not be recovered.

        The main menu is only worth the restart when the bot checker needs it:
        with the checker API answering, dropping the bot out of the login flow
        buys nothing and costs a full Add Account -> Login with Number -> Normal
        -> offer-reroll walk on the next number.
        """
        policy = str(getattr(self.bot, "menu_reset_policy", "auto") or "auto").strip().lower()
        if policy == "always":
            return True, "meesho_bot.reset_to_menu_on_change_failure is 'always'"
        if policy == "never":
            return False, "meesho_bot.reset_to_menu_on_change_failure is 'never'"
        why = str(getattr(self.checker, "bot_check_reason", "") or "").strip()
        if self._bot_checker_needed():
            return True, why or "the PRIMES bot checker is needed for the next number check"
        return False, why or "the checker API is answering, so the bot is not needed on its main menu"

    def _bot_menu_reset(self, pname, why):
        """Drop the bot back to its main menu (best effort)."""
        log(f"Resetting the bot to the main menu ({why}).", prefix=pname)
        try:
            self.bot.cancel_flow()
        except Exception as exc:
            log(f"Reset of the bot flow failed ({exc}); a manual /start may be needed.",
                prefix=pname)
        self.bot_at_number_prompt = False

    def _bot_prepare_change_number(self, context):
        """
        Tell the PRIMES bot to Change Number so the next found number is sent
        straight to the number prompt. Cancels/refunds the provider activation
        via the caller.

        Returns {"prompt_ready", "menu_reset", "reason"}:
          * prompt_ready - the bot now awaits the replacement number;
          * menu_reset   - the bot was dropped back to the main menu, so the
            next number runs the full flow (offer rerolls included).

        A Change Number the bot does not answer is retried INSIDE the bot client
        (bounded by meesho_bot.change_number_* settings), and a failure no
        longer automatically means "restart from the main menu": see
        _bot_menu_reset_needed().
        """
        pname = context.provider_name.upper()
        self.bot_change_attempts += 1
        outcome = {"prompt_ready": False, "menu_reset": False, "reason": ""}

        if self.bot_change_attempts > self.bot.max_change_number:
            log(f"Change Number attempt cap ({self.bot.max_change_number}) reached.",
                prefix=pname)
            self._bot_menu_reset(pname, "the Change Number attempt cap was reached")
            self.bot_change_attempts = 0
            outcome.update(menu_reset=True, reason="Change Number attempt cap reached")
            return outcome

        # The bot may already sit on the number prompt - a previous Change
        # Number that reported "unknown" can still have worked. Asking is
        # read-only and much cheaper than tapping again.
        if self._bot_at_prompt():
            self.bot_at_number_prompt = True
            log("Bot is already at the number prompt; no Change Number tap needed.",
                prefix=pname)
            outcome.update(prompt_ready=True, reason="already at the number prompt")
            return outcome

        failure = "the bot did not reach its number prompt"
        # Short label for the notification: the full error (with the screen text
        # and the hint to extend) goes to the log, not to Telegram.
        short = "the bot never showed its number prompt"
        try:
            res = self.bot.change_number(None)
            self.stats.increment("change_number")
            stage = res.get("stage")
            if stage == "prompt":
                self.bot_at_number_prompt = True
                log(f"Bot ready for replacement number (attempt {self.bot_change_attempts}).",
                    prefix=pname)
                outcome.update(prompt_ready=True,
                               reason="the bot is waiting for the replacement number")
                return outcome
            if stage == "needs_full_flow":
                # The bot left the login flow by itself (its OTP prompt expired,
                # a manual /start): it is already where a full flow starts, so
                # there is nothing to reset.
                log("Bot needs the full flow again (it is back at the menu/login steps).",
                    prefix=pname)
                self.bot_at_number_prompt = False
                outcome.update(reason="the bot left the login flow, so the full flow is needed")
                return outcome
            failure = f"Change Number returned stage '{stage}'"
            short = f"the bot answered Change Number with stage '{stage}'"
            log(f"{failure}.", prefix=pname)
        except MeeshoBotUnknownScreen as exc:
            failure = f"Change Number failed in bot: {exc}"
            short = "the bot showed an unrecognised screen after Change Number"
            log(f"{failure}; screen: {(exc.screen_text or '')[:300]}", prefix=pname)
        except MeeshoBotError as exc:
            failure = f"Change Number failed in bot: {exc}"
            short = "the bot did not answer the Change Number tap"
            log(failure, prefix=pname)
        except Exception as exc:
            failure = f"Change Number failed in bot: {exc!r}"
            short = "the bot did not answer the Change Number tap"
            log(failure, prefix=pname)

        # A failure can still have landed on the prompt (a slow edit, a copy
        # classify() does not know): look once more before giving up on it.
        if self._bot_at_prompt():
            self.stats.increment("change_number")
            self.bot_at_number_prompt = True
            log("Bot reached the number prompt after all; continuing in-flow.",
                prefix=pname)
            outcome.update(prompt_ready=True,
                           reason="the bot reached the number prompt on a second look")
            return outcome

        self.bot_at_number_prompt = False
        reset, why = self._bot_menu_reset_needed()
        if reset:
            self.stats.increment("bot_menu_resets")
            self._bot_menu_reset(pname, why)
            outcome.update(menu_reset=True, reason=f"{short}; {why}")
        else:
            self.stats.increment("bot_flow_kept")
            log(f"{failure}; keeping the bot in-flow ({why}). The next number is sent "
                f"from the number prompt when the bot is on one - no main-menu "
                f"restart and no offer reroll.", prefix=pname)
            outcome.update(reason=f"{short}; bot kept in-flow ({why})")
        return outcome

    # -- PRIMES bot: exclusive claim, offer pre-warm -------------------------

    def _warm_refresh_seconds(self):
        """How often a parked offer prompt is re-verified (meesho_bot)."""
        conf = self.config.get("meesho_bot", {}) or {}
        try:
            value = float(conf.get("offer_warm_refresh_seconds", 90) or 0)
        except (TypeError, ValueError):
            value = 90.0
        return value if value > 0 else 90.0

    def _bot_check_allowed(self):
        """
        (allowed, reason) - may the PRIMES bot be used for a number check now?

        A login/OTP flow outranks a check: the check would navigate the bot
        away from the OTP screen and the paid number would be lost.
        """
        if not self.bot.ready:
            return False, "the PRIMES userbot is not connected"
        if self.bot_login_active:
            return False, "a login flow is using the PRIMES bot"
        if self.target_found_event.is_set():
            return False, "a target number is being driven through the login flow"
        return True, ""

    def _bot_check_claim(self):
        """Claim the bot for one number check (waits, but never for a login)."""
        conf = self.checker_conf.get("bot") or {}
        try:
            timeout = float(conf.get("claim_timeout_seconds", 60) or 0)
        except (TypeError, ValueError):
            timeout = 60.0
        return _BotClaim(self, "check", timeout=timeout, wait=timeout > 0)

    def _bot_login_claim(self):
        """Claim the bot for a whole login/OTP/recovery flow (blocking)."""
        return _BotClaim(self, "login", timeout=0, wait=True)

    def _bot_warm_loop(self):
        """
        Keep the PRIMES bot parked on an agreed offer while workers hunt.

        The offer reroll (Add Account -> Login with Number -> Normal -> reroll
        until UPI <= target) normally runs AFTER a number was found, while its
        paid OTP window is ticking. Here it runs before there is a number, so
        the found number is typed into a screen that is already waiting for it.
        """
        last_verified = 0.0
        while not self.stop_requested.is_set():
            self.stop_requested.wait(1.0)
            if self.stop_requested.is_set():
                break
            if not (self.prewarm_enabled and self.bot.ready):
                continue
            # Never touch the bot while a target is being processed (or between
            # "target found" and the coordinator picking it up).
            if self.target_found_event.is_set() or self.bot_login_active:
                last_verified = 0.0
                continue

            if self.bot_at_number_prompt:
                now_ts = time.time()
                if now_ts - last_verified < self.warm_refresh_seconds:
                    continue
                if self._bot_at_prompt():
                    last_verified = now_ts
                    continue
                # The parked prompt is gone (bot timeout, manual /start): warm
                # again instead of typing the next number into a wrong screen.
                log("Parked offer prompt is gone; re-arming the PRIMES bot.")
                self.bot_at_number_prompt = False
            self._bot_warm_once()
            last_verified = time.time()

    def _bot_warm_once(self):
        """Walk the bot to an agreed offer and park it there (best effort)."""
        prepare = getattr(self.bot, "prepare_offer", None)
        if not callable(prepare):
            return
        try:
            with _BotClaim(self, "prewarm", timeout=0, wait=False):
                res = prepare()
        except BotBusy:
            return
        except Exception as exc:
            log(f"Offer pre-warm failed: {exc}")
            self.bot_at_number_prompt = False
            return

        stage = (res or {}).get("stage")
        if stage and stage != "offer":
            log(f"Offer pre-warm ended on stage '{stage}'; the next number runs "
                f"the full flow.")
            self.bot_at_number_prompt = False
            return

        upi = (res or {}).get("upi")
        rerolls = (res or {}).get("rerolls", 0)
        if rerolls:
            self.stats.increment("offer_rerolls", rerolls)
        self.bot_warm = {
            "ready": True,
            "upi": upi,
            "at": time.time(),
            "rerolls": rerolls,
        }
        self.bot_at_number_prompt = True
        self.stats.increment("offer_prewarmed")
        log(f"PRIMES bot is parked on an agreed offer (UPI ₹{upi}, {rerolls} "
            f"reroll(s)) - the next number is sent straight to it.")

    def _release_bot_warm(self):
        """The parked offer was consumed or lost: the warm loop re-arms it."""
        self.bot_warm = {"ready": False, "upi": None, "at": 0.0, "rerolls": 0}
        self.bot_at_number_prompt = False

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
        if self.instance:
            log(f"Instance: {self.instance} "
                f"(stats: {self.stats.path.name}, state: {self.state.path.name}, "
                f"signals: {self.notify.signal_dir.name})")

        # Cancellations a previous run could not finish (the provider refused
        # them) still hold money: pick them up instead of losing it.
        leftover = self.pending_cancels.pending()
        if leftover:
            log(f"Resuming {len(leftover)} deferred cancellation(s) from a "
                f"previous run.")
            self.pending_cancels.resume()

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

        # Keep the PRIMES bot parked on an agreed offer while the workers hunt,
        # so a found number does not wait for the offer rerolls.
        warm_thread = None
        if use_bot and self.prewarm_enabled:
            warm_thread = threading.Thread(target=self._bot_warm_loop,
                                           name="BotOfferWarm", daemon=True)
            warm_thread.start()
            log(f"Offer pre-warm: ON (the bot waits on an agreed offer with "
                f"UPI <= Rs.{self.bot.target_upi_price}; re-verified every "
                f"{self.warm_refresh_seconds:.0f}s).")

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
                        if use_bot:
                            # The login/OTP/recovery flow owns the PRIMES bot
                            # for as long as this number is in play: no number
                            # check may walk the bot away from its OTP screen
                            # (see _BotClaim).
                            with self._bot_login_claim():
                                cont = self._process_target(target, use_bot)
                        else:
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

        A Change Number the bot does not answer is retried inside the bot
        client (meesho_bot.change_number_retries, within its own short budget).
        If it still fails, the bot is dropped back to the main menu ONLY when
        the bot checker is needed for the next number check (checker.mode
        "bot", or "auto" while the checker API is down / cooling down / has no
        keys) - the main menu is where the bot checker works from, so the reset
        costs nothing extra then. With the checker API answering, the bot stays
        in-flow: the next login reuses the number prompt it is sitting on
        instead of paying for Add Account -> Login with Number -> Normal ->
        offer rerolls again, and only walks back to the menu itself if the bot
        really is lost (see meesho_bot.reset_to_menu_on_change_failure).

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

        outcome = {"prompt_ready": False, "menu_reset": False, "reason": ""}
        if self.bot.ready:
            outcome = self._bot_prepare_change_number(target) or {}
            if outcome.get("prompt_ready"):
                detail = "The bot is waiting for the replacement number."
            elif outcome.get("menu_reset"):
                detail = ("The bot flow will restart from the menu "
                          f"({outcome.get('reason') or 'reset to the main menu'}).")
            else:
                detail = ("The bot stays in-flow - no main-menu restart and no offer "
                          f"reroll ({outcome.get('reason') or 'the checker API is answering'}); "
                          "the next number is sent from the number prompt when the bot "
                          "is on one.")
            self.notify.send(
                "🔄 Changing number",
                f"{target.clean_number} ({reason}). {detail}"
            )
        return outcome


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
    parser.add_argument("--instance",
                        help="Name of this run, for parallel copies (e.g. one Termux tab per provider). "
                             "Namespaces stats.json / state.json / pending_cancels.json / .signals/ "
                             "(stats.<name>.json, ...) and applies the matching config.json "
                             '"instances" block. Defaults to the provider name when --provider '
                             "names exactly one provider.")
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

    # Parallel copies (two Termux tabs) must not share runtime files, and each
    # may bring its own Telegram userbot session / command bot: --instance
    # namespaces the files and merges config["instances"][name] over the config.
    instance = resolve_instance(args.instance, args.provider)
    config = apply_instance_overrides(config, instance)

    coordinator = ParallelAutomationCoordinator(
        config, provider_override=args.provider, instance=instance
    )

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
