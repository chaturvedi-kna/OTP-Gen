"""
Meesho OTP Automation Orchestrator.

Parallel worker execution for SMS providers (TemporaSMS, VSImpro, OtpDoctor,
OTPCart, OTPIndia) with checker validation, refund/balance safety gating,
interactive Telegram commands (/run [provider], /status, /balance, /stop), and
optional end-to-end automation of the PRIMES Meesho concierge Telegram bot via
a Telethon userbot.
"""

import argparse
import json
import os
import re
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
    create_otp_clients,
    validate_provider_selection
)
from checker_client import (
    CheckerClient,
    CheckerAuthError,
    CheckerError,
    CheckerProxyError,
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
    accounts_filename,
    apply_instance_overrides,
    pending_filename,
    resolve_instance,
    state_filename,
    stats_filename,
)
from accounts import (
    LinkedAccountsStore,
    clean_number as clean_account_number,
    local_time,
    looks_like_number,
)
from cancel_watch import (
    CANCEL_RETRY_TYPES,
    CANCEL_SUCCESS_TYPES,
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
from notifier import Notifier, normalize_notify_level


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


def checker_extra_info(check):
    """
    Compact "k=v ..." string for v2 info fields beyond the verdict.

    The API enriches responses with the number's operator (Jio / Airtel / Vi /
    BSNL) and may include plan / validity / expiry / True-5G details. Field
    names beyond "operator" are not in the published schema, so everything
    past the known verdict bookkeeping is surfaced generically (operator first
    for readability).
    """
    skip = {
        "success", "api_version", "service", "number", "is_registered",
        "in_database", "is_down", "source", "checker_name",
        "checker_username", "batched", "api_error",
    }
    check = check or {}
    bits = []
    if check.get("operator") not in (None, ""):
        bits.append(f"operator={check['operator']}")
    for key in sorted(check):
        if key in skip or key == "operator":
            continue
        value = check[key]
        if value in (None, "", {}, []):
            continue
        bits.append(f"{key}={value}")
    return " ".join(bits)


class NumberContext:
    """Encapsulates acquired number and its specific provider client."""
    def __init__(self, client, activation_id, raw_number, clean_number):
        self.client = client
        self.provider_name = client.name
        self.activation_id = activation_id
        self.raw_number = raw_number
        self.clean_number = clean_number
        self.acquired_at = now()


def _bot_lease(coordinator, action, owner):
    """
    Mark / clear the PRIMES conversation lease on the bot client.

    Both the offer pre-warm and the number checker drive the SAME Telegram
    chat unless a dedicated checker bot answers the check. The lease lets the
    client reject (with a readable message) anything that would navigate the
    chat while the login / pre-warm owns it - instead of silently tapping in
    the middle of a reroll (the pre-warm then failed with "Offer screen has
    no reroll button").
    """
    bot = getattr(coordinator, "bot", None)
    method = getattr(bot, "hold_conversation" if action == "hold"
                     else "release_conversation", None)
    if not callable(method):
        return
    try:
        method(owner)
    except Exception:
        pass


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
            # Tell the bot client who is driving the PRIMES chat: the login
            # flow and the offer pre-warm leave the bot on a screen that must
            # not be walked away from, and a check that would use the SAME
            # conversation says so instead of tapping over it.
            if self.owner in ("login", "prewarm"):
                _bot_lease(coord, "hold", self.owner)
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
            if self.owner in ("login", "prewarm"):
                _bot_lease(coord, "release", self.owner)
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
        # Every linked account (number / provider / time) + the milestones
        # for /accounts and /milestone - see accounts.py.
        self.accounts = LinkedAccountsStore(filename=accounts_filename(self.instance))
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
                    f"@{checker_bot_username.lstrip('@')} ({exc}); it is not "
                    f"available - checks will use the PRIMES bot conversation.")
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

        # Providers stopped on their own, mapped to the reason. A problem that
        # is local to one provider (a refund tally that did not tally) must not
        # take the whole run down with it: the other providers keep hunting, and
        # the numbers they have in play are neither abandoned nor interrupted
        # half way through an OTP wait. A stop without a provider (a broken
        # checker, a failed referral step, a user /stop) is global.
        self.stopped_providers = {}
        self.stopped_providers_lock = threading.Lock()

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

        # Numbers that are paid for and NOT closed yet (bought, still being
        # checked / waiting for the OTP / waiting for the cancel window).
        # Their price is missing from the live balance, so a refund tally must
        # subtract it: without this, a cancel taken while another number is
        # still open always looks like a missing refund and critical-stops the
        # run over money that is simply still in use.
        #   {provider: {activation_id: {"price": float|None, "number": str}}}
        # A price of None means it could not be measured - the tally is then
        # suspended for that provider instead of guessing (see _suspend_tally).
        self.open_activations = {}
        self.open_activations_lock = threading.Lock()
        # The last price measured (or configured) per provider, used when a
        # fresh measurement is not possible.
        self.known_prices = {}
        self.price_lock = threading.Lock()

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
        # Set every time a deferred cancellation is resolved: a worker that is
        # out of balance because of pending cancels wakes up on it and asks
        # for the next number right away (see _wait_for_pending_otpindia_refunds).
        self._pending_resolved_event = threading.Event()
        # Providers whose refund tally is suspended because a deferred
        # cancellation holds an unknown amount (the balance could not be read
        # when the cancel was deferred). Suspending beats critical-stopping on
        # a difference that is fully explained by the pending refund.
        self._tally_suspended = set()
        self._tally_suspension_lock = threading.Lock()

        # Build active clients
        self.clients = create_otp_clients(config, provider_override=provider_override)
        # The startup selection: a bare /run returns to it, /balance always
        # covers it, and a Telegram "/run <provider>" narrows self.clients for
        # that run only (see request_run).
        self.configured_clients = list(self.clients)
        # Every client ever built here, so a deferred cancellation from an
        # earlier /run selection stays resolvable after the selection changed.
        self.client_registry = {c.name: c for c in self.clients}
        for c in self.clients:
            c_conf = self._client_conf(c)
            self.worker_max_attempts[c.name] = c_conf.get("max_attempts") or self.settings.get("max_attempts", 200)
            self.worker_attempts[c.name] = 0

        # Attach Telegram commands
        self.notify.set_command_callbacks(
            status_cb=self.get_status_summary,
            balance_cb=self.get_balances_summary,
            run_cb=self.request_run,
            stop_cb=self.request_stop,
            referral_cb=self.command_referral_link,
            checker_cb=self.command_checker_mode,
            accounts_cb=self.command_accounts,
            milestone_cb=self.command_milestone,
            notify_cb=self.command_notify_level,
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
        refunded - minus the money still held by deferred cancellations AND by
        numbers that are still open (bought, not closed yet).

        Without the open-activation deduction, a number bought while a refused
        cancel is still waiting for expiry would always look like a refund
        mismatch (that number's price is still missing from the balance), which
        critical-stopped the run over a timing problem. Without the deferred
        deduction the same is true of the activation whose cancel was refused.
        """
        with self.ledger_lock:
            entry = self.ledger.get(name, {})
            base = entry.get("expected_balance")
        if base is None:
            return None
        hold, _unknown = self.pending_cancels.hold_total(name, exclude=activation_id)
        open_hold, _open_unknown = self._open_hold(name, exclude=activation_id)
        return base - hold - open_hold

    # -- open activations (paid for, not closed yet) -------------------------

    def _open_activation(self, client, activation_id, number=""):
        """
        Record a freshly bought number as open and work out what it cost.

        The price is what the ledger uses as this activation's hold, so it has
        to be trustworthy. It is resolved in this order:

          1. MEASURED - the drop between the balance the ledger expected before
             the purchase and the balance right after it. Ground truth, but it
             races with refunds the provider makes on its own (expiry, a manual
             cancel in the panel), which land in the same instant.
          2. CONFIGURED - "price" in the provider's config.json block. Set it
             and the measurement is never needed, so the race disappears.
          3. REMEMBERED - the last price measured on this provider.

        A price that is still unknown suspends the refund tally for that
        provider instead of guessing at one.
        """
        name = client.name
        try:
            balance = float(client.get_balance())
        except Exception as exc:
            log(f"Could not read the balance after buying {number or activation_id}: {exc}",
                prefix=name.upper())
            balance = None

        # The balance this provider should be at RIGHT NOW (baseline minus the
        # money the other open/deferred activations still hold) - that is what
        # the new number's price is measured against.
        before = self._expected_balance(name, activation_id)

        price = None
        source = None
        if balance is not None and before is not None:
            drop = round(float(before) - balance, 6)
            if drop < 0:
                # Money came back that we never cancelled: the provider refunded
                # an activation on its own (expiry, or a manual cancel in the
                # panel). The ledger was too high - re-anchor it on the real
                # balance, otherwise every later measurement is off by it.
                log(f"Balance rose by {-drop:.4f} while buying "
                    f"{number or activation_id}: the provider refunded an "
                    f"activation on its own. Re-anchoring the ledger.",
                    prefix=name.upper())
                self._rebaseline(name, balance)
                before = balance
                drop = 0.0
            cap = self._activation_price_cap(client)
            if 0 < drop <= cap:
                price, source = drop, "measured"
            else:
                log(f"Balance moved by {drop:+.4f} while buying "
                    f"{number or activation_id} (expected a price up to {cap:.2f}).",
                    prefix=name.upper())

        configured = self._configured_price(client)
        if price is None and configured is not None:
            price, source = configured, "configured"
        if price is None:
            remembered = self._remembered_price(name)
            if remembered is not None:
                price, source = remembered, "last known"
        if price is not None and source != "measured":
            log(f"Price of {number or activation_id} taken from the {source} "
                f"price: {price:.4f}.", prefix=name.upper())
        if (price is not None and configured is not None
                and abs(price - configured) > 0.01):
            log(f"Note: {name.upper()} charged {price:.4f} but config.json sets "
                f"price={configured:.4f} - update it if this keeps happening.",
                prefix=name.upper())
        if price is not None:
            self._remember_price(name, price)

        with self.open_activations_lock:
            self.open_activations.setdefault(name, {})[str(activation_id)] = {
                "price": price,
                "number": number,
            }
        if price is None:
            self._suspend_tally(
                name,
                f"the price of {number or activation_id} could not be measured "
                f"(set \"price\" in the {name} block of config.json to stop the "
                f"guessing)"
            )
        return price

    def _configured_price(self, client):
        """The price one number costs, when the operator states it in config."""
        conf = self._client_conf(client) or {}
        for key in ("price", "activation_price", "number_price"):
            try:
                value = float(conf.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return None

    def _remembered_price(self, name):
        with self.price_lock:
            return self.known_prices.get(name)

    def _remember_price(self, name, price):
        with self.price_lock:
            self.known_prices[name] = float(price)

    def _rebaseline(self, name, balance):
        """
        Re-anchor the ledger on a balance we have just read, keeping the money
        that is still held by the provider's other activations accounted for.
        """
        if balance is None:
            return
        open_hold, _u1 = self._open_hold(name)
        deferred_hold, _u2 = self.pending_cancels.hold_total(name)
        self._note_balance(name, round(float(balance) + open_hold + deferred_hold, 6))

    def _activation_price_cap(self, client):
        """Plausible upper bound for one number's price on this provider."""
        conf = self._client_conf(client) or {}
        for key in ("max_price", "price_cap"):
            try:
                value = float(conf.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return 1000.0

    def _open_hold(self, name, exclude=None):
        """
        Money still held by numbers that were bought and not closed yet.

        Returns (total, whether any of those holds is unknown).
        """
        total = 0.0
        unknown = False
        with self.open_activations_lock:
            items = list((self.open_activations.get(name) or {}).items())
        for act_id, info in items:
            if exclude and str(act_id) == str(exclude):
                continue
            price = (info or {}).get("price")
            if price is None:
                unknown = True
            else:
                try:
                    total += float(price)
                except (TypeError, ValueError):
                    unknown = True
        return total, unknown

    def _close_activation(self, name, activation_id, number=""):
        """
        An activation is finished (refunded, consumed or handed to the watcher):
        take it out of the open list so it stops holding money in the ledger.
        """
        with self.open_activations_lock:
            info = (self.open_activations.get(name) or {}).pop(str(activation_id), None)
        if info is None:
            return
        log(f"Activation {activation_id} ({number or info.get('number') or '?'}) "
            f"closed; it no longer holds money on {name.upper()}.",
            prefix=name.upper())
        self._recheck_tally_suspension(name)

    def _recheck_tally_suspension(self, name):
        """Lift the tally suspension once every hold on this provider is known."""
        _open, open_unknown = self._open_hold(name)
        _held, deferred_unknown = self.pending_cancels.hold_total(name)
        if not (open_unknown or deferred_unknown):
            self._clear_tally_suspension(name)

    # -- provider-scoped stops ----------------------------------------------

    def provider_stop_reason(self, name):
        """Why this provider was stopped on its own, or None if it is running."""
        with self.stopped_providers_lock:
            return self.stopped_providers.get(name)

    def provider_stopped(self, name):
        return self.provider_stop_reason(name) is not None

    def _worker_should_stop(self, client):
        """
        Should this provider's worker stop buying numbers?

        Either the whole run is stopping, or this provider alone was stopped
        (a refund tally that did not tally). The other providers keep going.
        """
        if self.stop_requested.is_set():
            return True
        return self.provider_stopped(client.name)

    def _stop_provider(self, name, title):
        """Stop one provider's worker; returns False if it already was."""
        with self.stopped_providers_lock:
            if name in self.stopped_providers:
                return False
            self.stopped_providers[name] = title
        return True

    # -- hooks for the deferred-cancellation watcher (cancel_watch.py) -------

    def _client_conf(self, client):
        """The config.json block for a client (OtpDoctor lives under "otp")."""
        return self.config.get(client.name, {}) or (
            self.config.get("otp", {}) if client.name == "otpdoctor" else {}
        )

    def client_by_name(self, name):
        for client in self.clients:
            if client.name == name:
                return client
        # A provider that was selected by an earlier "/run <provider>" but is
        # not part of the current selection: still resolvable, so its deferred
        # cancellations are not stranded.
        return self.client_registry.get(name)

    def expected_balance(self, name, activation_id=None):
        return self._expected_balance(name, activation_id)

    def note_balance(self, name, balance):
        return self._note_balance(name, balance)

    def settle_balance(self, name, balance, exclude=None):
        """
        Re-baseline the ledger after a deferred cancellation was resolved.

        The live balance becomes the expected balance for the next number, but
        the money that is STILL held by the provider's other open/deferred
        activations has to be added back: otherwise every settle would drag the
        baseline down by that money and the next refund would look missing.
        """
        if balance is None:
            return
        open_hold, _u1 = self._open_hold(name, exclude=exclude)
        deferred_hold, _u2 = self.pending_cancels.hold_total(name, exclude=exclude)
        self._note_balance(name, round(float(balance) + open_hold + deferred_hold, 6))

    def _drain_rebaseline(self, name, balance):
        """_rebaseline without an exclusion (used by the watcher hook)."""
        return self._rebaseline(name, balance)

    def critical_stop(self, title, message, provider=None):
        return self._critical_stop(title, message, provider=provider)

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
            log(f"Refund tally suspended ({reason}) until the pending cancel "
                f"resolves.", prefix=name.upper())
            self.notify.alert(
                f"⏳ [{name.upper()}] Refund tally suspended",
                f"{reason}.\nCancels on {name.upper()} are not tallied until that "
                f"money is back; everything else continues."
            )

    def _clear_tally_suspension(self, name):
        with self._tally_suspension_lock:
            self._tally_suspended.discard(name)

    def _tally_is_suspended(self, name):
        with self._tally_suspension_lock:
            return name in self._tally_suspended

    def tally_is_suspended(self, name):
        """Is this provider's refund tally suspended? (hook for cancel_watch)"""
        return self._tally_is_suspended(name)

    def _note_checker_failure(self):
        """Count one more consecutive failed check; returns the streak."""
        with self.attempts_lock:
            self.checker_failure_streak += 1
            return self.checker_failure_streak

    def _reset_checker_failures(self):
        with self.attempts_lock:
            self.checker_failure_streak = 0

    def _critical_stop(self, title, message, provider=None):
        """
        Halt the run and send a max-priority alert.

        provider: when the problem is local to ONE provider (a refund tally that
        did not tally), only that provider's worker is stopped. The other
        providers keep hunting and the numbers they have in play are neither
        abandoned nor interrupted half way through an OTP wait - stopping them
        too used to strand a paid number that could neither be cancelled (its
        provider's cancel window had not passed) nor watched (the run was
        over). Without a provider the whole run stops.
        """
        log(f"CRITICAL STOP: {title} - {message}")
        self.stats.increment("critical_stops")

        if provider is not None:
            fresh = self._stop_provider(provider, title)
            if not fresh:
                # Already stopped for this provider: a second alert would only
                # bury the first one.
                log(f"{provider.upper()} is already stopped ({title}); "
                    f"not alerting again.")
                return
            self.worker_statuses[provider] = f"Stopped ({title})"
            self.notify.alert(
                f"🛑 {title}",
                message + f"\n\nOnly {provider.upper()} is stopped - the other "
                f"providers keep running and their in-flight numbers are safe. "
                f"No new numbers will be bought from {provider.upper()} until "
                f"you /run again."
            )
            return

        self.stop_requested.set()
        self.notify.alert(f"🛑 {title}", message + "\n\nAutomation STOPPED - manual check needed.")

    # -- Status and Balances for Telegram & CLI ------------------------------

    def get_balances_summary(self):
        """Fetches live balance from every provider this instance can run
        (the configured selection plus anything a /run <provider> added)."""
        lines = ["💰 Current OTP Balances:"]
        seen = set()
        current_names = {c.name for c in self.clients}
        clients = list(self.clients) + [
            c for c in self.client_registry.values() if c.name not in current_names
        ]
        for client in clients:
            if client.name in seen:
                continue
            seen.add(client.name)
            try:
                bal = client.get_balance()
                lines.append(f"• {client.name.upper()}: {bal:.4f}")
            except Exception as exc:
                lines.append(f"• {client.name.upper()}: Error ({exc})")
        return "\n".join(lines)

    def get_status_summary(self):
        """Returns live tool status."""
        state_str = "🟢 RUNNING" if self.is_running else "⚪ IDLE / STOPPED"
        # Show what THIS tab actually runs (the --provider / instance selection),
        # not the raw config string, which can differ on a mixed-layout tab.
        mode_text = ", ".join(c.name.upper() for c in self.configured_clients) \
            or str(self.config.get("active_otp_provider", "both")).upper()
        lines = [
            f"🤖 Meesho Automation: {state_str}",
            f"Mode: {mode_text}",
        ]
        run_names = [c.name for c in self.clients]
        configured_names = [c.name for c in self.configured_clients]
        if run_names != configured_names:
            lines.append(
                "Run selection: "
                f"{', '.join(n.upper() for n in run_names)} "
                "(/run <provider>; a bare /run uses Mode again)"
            )
        if self.instance:
            lines.append(f"Instance: {self.instance} (own stats / state / signals)")
        lines += [
            f"PRIMES bot automation: {'🟢 ON' if self.bot.ready else '⚪ manual'}",
            f"Checker: {self.checker.describe()}",
        ]
        cooldown = self.checker.cooldown_remaining()
        if cooldown > 0:
            lines.append(f"  ⚠️ API cooling down, bot checker in use for ~{cooldown:.0f}s")

        # A provider stopped by its own refund tally: the rest of the run goes on.
        stopped = [(name, reason) for name, reason in self.stopped_providers.items()]
        if stopped:
            for name, reason in sorted(stopped):
                lines.append(f"  🛑 {name.upper()}: {reason} "
                             f"(stopped alone; other providers keep running)")

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
            lines.append("\n⏳ Deferred cancellations (waiting to become cancelable):")
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
            f"  {self.linked_counts_line()}\n"
            f"  Targets found: {s['targets_found']} | OTPs received: {s['otp_received']}\n"
            f"  Wrong OTP: {s['otp_wrong']} | Expired: {s['otp_expired']} | Blocked: {s['user_blocked']}\n"
            f"  OTP timeouts: {s['otp_timeout']} | Change-number: {s['change_number']} "
            f"(menu resets: {s.get('bot_menu_resets', 0)} | "
            f"kept in-flow: {s.get('bot_flow_kept', 0)})\n"
            f"  Referral step: {s.get('referral_pasted', 0)} pasted | "
            f"{s.get('referral_skipped', 0)} skipped | {s.get('offer_rerolls', 0)} offer rerolls\n"
            f"  Checks: {s.get('checker_api_checks', 0)} via API | "
            f"{s.get('checker_dedicated_checks', 0)} via dedicated bot | "
            f"{s.get('checker_bot_checks', 0)} via PRIMES bot | "
            f"{s.get('checker_fallbacks', 0)} API→bot fallbacks\n"
            f"  Numbers consumed (charged): {s['numbers_consumed']}\n"
            f"  Refunds verified: {s['refunds_verified']} | Refunds missing: {s['refunds_missing']} | "
            f"Late OTP salvaged: {s['late_otp_salvaged']}"
            + (f"\n  🛑 Critical stops: {s['critical_stops']}" if s.get('critical_stops') else "")
        )

        lines.append("\n" + self.get_balances_summary())
        return "\n".join(lines)

    # -- Linked accounts + milestones (/accounts, /milestone) -----------------

    @staticmethod
    def _provider_counts_text(counts):
        return ", ".join(f"{name.upper()} {count}" for name, count in counts.items()) or "none"

    def linked_counts_line(self):
        """'Accounts linked: 53 (TEMPORA 30, VSIMPRO 20, OTPINDIA 3)' for /status."""
        records = self.accounts.accounts()
        total = self.stats.snapshot().get("accounts_linked", 0)
        by_provider = self.accounts.count_by_provider(records)
        text = f"Accounts linked: {max(total, len(records))}"
        if by_provider:
            text += f" ({self._provider_counts_text(by_provider)})"
        return text

    def get_accounts_summary(self):
        """Reply for /accounts: totals per provider + what is new since the last milestone."""
        records = self.accounts.accounts()
        legacy_total = self.stats.snapshot().get("accounts_linked", 0)
        by_provider = self.accounts.count_by_provider(records)
        scope = f" ({self.instance})" if self.instance else ""

        lines = [f"👤 Linked accounts{scope}: {max(legacy_total, len(records))}"]
        for name, count in by_provider.items():
            lines.append(f"  • {name.upper()}: {count}")
        untracked = legacy_total - len(records)
        if untracked > 0:
            lines.append(f"  • (before per-provider tracking: {untracked})")
        if not records:
            lines.append("  No account has been linked with tracking on yet.")

        milestone = self.accounts.last_milestone()
        if milestone is None:
            lines.append("\n📍 No milestone yet. Set one with\n"
                         "/milestone <last number you shared> <note>")
            return "\n".join(lines)

        fresh = self.accounts.since(milestone)
        note = f' "{milestone.get("note")}"' if milestone.get("note") else ""
        lines.append(
            f"\n📍 Milestone #{milestone.get('seq')} ({local_time(milestone.get('created_at_epoch'))}):"
            f"{note}\n  up to {milestone.get('last_number')} = {milestone.get('total')} "
            f"({self._provider_counts_text(milestone.get('by_provider') or {})})"
        )
        lines.append(f"🆕 Since then: {len(fresh)}"
                     + (f" ({self._provider_counts_text(self.accounts.count_by_provider(fresh))})"
                        if fresh else ""))
        if fresh:
            first, last = fresh[0], fresh[-1]
            lines.append(f"  First: `{first['number']}` ({first['provider'].upper()}, "
                         f"{local_time(first.get('linked_at_epoch'))})")
            lines.append(f"  Last:  `{last['number']}` ({last['provider'].upper()}, "
                         f"{local_time(last.get('linked_at_epoch'))})")
            lines.append("/accounts list shows all of them.")
        return "\n".join(lines)

    def get_accounts_list(self):
        """Reply for /accounts list: every number linked since the last milestone."""
        milestone = self.accounts.last_milestone()
        fresh = self.accounts.since(milestone)
        if milestone is None:
            head = f"👤 All linked accounts ({len(fresh)}; no milestone yet):"
        else:
            head = (f"🆕 Linked since milestone #{milestone.get('seq')} "
                    f"(after {milestone.get('last_number')}): {len(fresh)}")
        if not fresh:
            return head + "\n  none"
        lines = [head]
        for record in fresh:
            lines.append(f"`{record['number']}`  {str(record.get('provider', '')).upper()}  "
                         f"{local_time(record.get('linked_at_epoch'))}")
        return "\n".join(lines)

    def command_accounts(self, argument):
        """Telegram /accounts handler ("" -> summary, "list" -> numbers since milestone)."""
        argument = (argument or "").strip().lower()
        if argument in ("list", "numbers", "all", "since"):
            return self.get_accounts_list()
        return self.get_accounts_summary()

    def command_milestone(self, argument):
        """
        Telegram /milestone handler.

          /milestone <last shared number> <note>  -> add a milestone
          /milestone                              -> show the milestones
          /milestone remove                       -> drop the last one
        """
        argument = (argument or "").strip()
        if argument.lower() in ("", "show", "list", "status", "?"):
            milestones = self.accounts.milestones()
            if not milestones:
                return ("📍 No milestone yet.\n"
                        "/milestone <last number you shared> <note> marks everything "
                        "up to that number as used/shared.")
            lines = [f"📍 Milestones ({len(milestones)}):"]
            for m in milestones[-8:]:
                note = f' "{m.get("note")}"' if m.get("note") else ""
                lines.append(f"  #{m.get('seq')} {local_time(m.get('created_at_epoch'))} - "
                             f"up to {m.get('last_number')} = {m.get('total')} linked{note}")
            fresh = self.accounts.since(milestones[-1])
            lines.append(f"🆕 Since #{milestones[-1].get('seq')}: {len(fresh)}")
            return "\n".join(lines)

        if argument.lower() in ("remove", "undo", "delete", "pop"):
            removed = self.accounts.remove_last_milestone()
            if removed is None:
                return "❌ There is no milestone to remove."
            log(f"Milestone #{removed.get('seq')} removed via Telegram.")
            return (f"🗑 Removed milestone #{removed.get('seq')} (up to {removed.get('last_number')}"
                    f", {removed.get('total')} linked).")

        parts = argument.split(None, 1)
        number, note = parts[0], (parts[1].strip() if len(parts) > 1 else "")
        if not looks_like_number(number):
            return ("❌ Usage: /milestone <last shared number> <note>\n"
                    "The first word must be the last number you shared, "
                    "e.g. /milestone 9876543210 10 used + 40 shared")
        try:
            milestone = self.accounts.add_milestone(number, note)
        except ValueError as exc:
            recent = self.accounts.accounts()[-3:]
            hint = ("\nRecent: " + ", ".join(f"`{r['number']}`" for r in recent)) if recent else ""
            return f"❌ {exc}.{hint}"
        log(f"Milestone #{milestone['seq']} set via Telegram: up to "
            f"{milestone['last_number']} ({milestone['total']} linked) {milestone['note']!r}.")
        fresh = self.accounts.since(milestone)
        note_text = f' "{milestone["note"]}"' if milestone["note"] else ""
        return (f"✅ Milestone #{milestone['seq']}{note_text}\n"
                f"Up to `{milestone['last_number']}`: {milestone['total']} linked "
                f"({self._provider_counts_text(milestone['by_provider'])})\n"
                f"🆕 After it: {len(fresh)}"
                + (f" (first `{fresh[0]['number']}`, last `{fresh[-1]['number']}`)" if fresh else ""))

    # -- Telegram verbosity (/notify) ------------------------------------------

    def notify_level_text(self):
        muted = self.notify.muted_count
        return (f"🔔 Telegram level: {self.notify.level.upper()}"
                + (f" ({muted} message(s) muted this run)" if muted else "")
                + "\n" + self.notify.describe_levels()
                + "\nSwitch with /notify all|normal|quiet.")

    def command_notify_level(self, argument):
        """Telegram /notify handler: show or change the verbosity (saved to config.json)."""
        argument = (argument or "").strip()
        if argument.lower() in ("", "show", "status", "?"):
            return self.notify_level_text()
        level = normalize_notify_level(argument, default=None)
        if level is None:
            return f"❌ Unknown level '{argument}'. Use all, normal or quiet.\n\n" + self.notify_level_text()

        previous = self.notify.level
        self.notify.set_level(level)
        self.config.setdefault("telegram", {})["notify_level"] = level
        config_path = next((name for name in CONFIG_FILES if os.path.exists(name)), None)
        if config_path is None:
            saved = "\n⚠️ config.json not found - the change applies to this run only."
        else:
            try:
                set_notify_level(config_path, level)
                saved = f"\nSaved to {config_path}."
            except Exception as exc:
                saved = f"\n⚠️ Could not save to {config_path}: {exc}"
        log(f"Telegram notify level changed via Telegram: {previous} -> {level}.")
        return f"✅ Telegram level set to {level.upper()}.{saved}\n\n" + self.notify_level_text()

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
        try:
            api_url = getattr(getattr(self.checker, "api", None), "endpoint_url", "")
        except Exception:
            api_url = ""
        if api_url:
            lines.append(f"API endpoint: {api_url}")
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
            f"{s.get('checker_dedicated_checks', 0)} via dedicated bot | "
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

    def request_run(self, provider=None):
        """
        Telegram /run handler, optionally with a provider selection:

          /run                 -> the configured active_otp_provider set
          /run vsimpro         -> only VSImpro for this run
          /run tempora,vsimpro  -> only those two for this run
          /run otpindia         -> only OTPIndia for this run
          /run all              -> every enabled provider with credentials

        The selection applies to this run only: a later bare /run returns to
        the configured set (and to the same instance's stats/state files).

        Returns the reply text for the Telegram message; an "❌ ..." reply
        means the run was NOT started.
        """
        selection = str(provider or "").strip()

        new_clients = None
        if selection:
            ok, error = validate_provider_selection(self.config, selection)
            if not ok:
                log(f"/run rejected: {error}")
                return f"❌ {error}"
            new_clients = create_otp_clients(self.config, provider_override=selection)
            if not new_clients:
                error = (
                    f"No usable provider in '{selection}' - check "
                    f"config.json (enabled + api_key/token)."
                )
                log(f"/run rejected: {error}")
                return f"❌ {error}"

        if self.is_running:
            log("Restart requested while running. Stopping current workers first...")
            self.stop_requested.set()
            time.sleep(1.5)

        if new_clients:
            self.clients = new_clients
        else:
            self.clients = list(self.configured_clients)
        for c in self.clients:
            self.client_registry[c.name] = c
            c_conf = self._client_conf(c)
            self.worker_max_attempts[c.name] = c_conf.get("max_attempts") or self.settings.get("max_attempts", 200)
            self.worker_attempts[c.name] = 0

        threading.Thread(target=self.run, daemon=True).start()

        names = ", ".join(c.name.upper() for c in self.clients)
        if selection:
            log(f"/run selection: {names} (this run only; a bare /run uses "
                f"active_otp_provider again).")
            return f"▶️ Started search for {names} only (this run)."
        return "▶️ Started search for target number."

    def request_stop(self):
        self.stop_requested.set()
        log("Stop requested via command.")

    def _wait_for_pending_otpindia_refunds(self, client):
        """Wait quietly until ONE of OTPIndia's pending cancels has refunded.

        OTPIndia holds the purchase amount until its two-minute cancel window
        elapses. If the worker has used the available balance while one or more
        routine cancels are pending, NO_BALANCE is a temporary back-pressure
        signal, not a reason to stop the provider worker.

        The wait ends as soon as the FIRST pending cancellation is resolved
        (its refund has been verified by the watcher), not when all of them
        are: one refund pays for the next number, so the worker asks for it
        right away and the remaining cancels keep maturing in the background.
        If that one refund was not enough (or the activation was consumed by a
        late OTP instead of refunded), getNumber answers NO_BALANCE again and
        the worker simply comes back here to wait for the next one.

        Returns True when the caller should retry getNumber (also when the run
        is stopping - the caller checks stop_requested), False when there is
        nothing pending to wait for.
        """
        provider = getattr(client, "name", "")
        # OTPIndia and OTPSell both hold the purchase until their ~2 minute
        # cancel window elapses, so a NO_BALANCE with cancels still pending is
        # back-pressure, not a stop, for either of them.
        if provider not in ("otpindia", "otpsell"):
            return False

        def provider_pending():
            return [r for r in self.pending_cancels.pending()
                    if r.get("provider") == provider]

        pending = provider_pending()
        if not pending:
            return False
        pname = provider.upper()
        # Everything pending at this moment; the wait is over once any of
        # these is gone (a cancel deferred later does not count as progress).
        waiting_on = {str(r.get("activation_id")) for r in pending}

        poll_seconds = max(0.25, float(self.settings.get(
            "otpindia_balance_wait_poll_seconds", 2.0) or 2.0))
        log(f"NO_BALANCE with {len(pending)} cancel(s) pending refund - waiting "
            f"for the first refund, then requesting the next number.", prefix=pname)
        self._pending_resolved_event.clear()
        while not self.stop_requested.is_set():
            pending = provider_pending()
            still_open = {str(r.get("activation_id")) for r in pending}
            resolved = waiting_on - still_open
            if resolved:
                try:
                    balance = client.get_balance()
                    self._note_balance(provider, balance)
                    log(f"Refund landed ({len(pending)} cancel(s) still pending); "
                        f"balance {balance:.4f} - requesting the next number.", prefix=pname)
                except Exception as exc:
                    log(f"Refund landed ({len(pending)} cancel(s) still pending) but "
                        f"the balance refresh failed: {exc} - requesting the next "
                        f"number.", prefix=pname)
                return True
            soonest = min(float(r.get("expiry_at", time.time())) for r in pending)
            wait_left = max(0, int(soonest - time.time()))
            self.worker_statuses[provider] = (
                f"NO_BALANCE: waiting for the first of {len(pending)} pending "
                f"refund(s) ({wait_left}s to next retry)"
            )
            # Woken early by on_pending_resolved(); otherwise a periodic re-check.
            if self._pending_resolved_event.wait(poll_seconds):
                self._pending_resolved_event.clear()
        return True

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
                f"`{number}` · code `{code}`\n{sms}\n\n"
                f"Cancel refused ({reason}) but the OTP arrived: charge stands, "
                f"activation open - the bot may still take this code."
            )
            # Complaint evidence: provider refused to cancel (ERROR) and the
            # OTP still arrived - persist a record for a provider complaint.
            try:
                from cancel_watch import now as _cw_now
                self.pending_cancels.disputes.append({
                    "provider": str(getattr(client, "name", "") or ""),
                    "activation_id": activation_id,
                    "number": number,
                    "reason": reason,
                    "otp_code": code,
                    "otp_sms": sms,
                    "otp_received_at": _cw_now(),
                    "expected_balance": expected_balance,
                    "cancel_response": cancel_res,
                    "source": "immediate_salvage",
                })
                log(f"Complaint record saved for activation {activation_id}.",
                    prefix=pname)
            except Exception:
                pass
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
        # The activation leaves our hands in this call (refunded, consumed or
        # parked with the watcher): it must stop holding money in the ledger.
        self._close_activation(client.name, activation_id, number)

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
                    f"`{number}` · code `{code}`\n{sms}\n\n"
                    f"Charge stands (SMS delivered). The bot is still on its OTP "
                    f"screen - auto-submitting; if that fails, enter it manually "
                    f"while it is valid."
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

                if self.stop_requested.is_set():
                    # The run is already stopping (a critical stop fired, or the
                    # user asked for it): a refund can no longer be waited for,
                    # and a second critical stop would only bury the first one.
                    # Re-baseline so the next run starts from the real balance.
                    try:
                        actual_balance = client.get_balance()
                        self._note_balance(client.name, actual_balance)
                    except Exception:
                        actual_balance = None
                    log("Refund tally skipped (the run is stopping); new balance "
                        f"baseline: {actual_balance}", prefix=pname)
                    tally_ok = True
                elif (expected_balance is None
                      or self._tally_is_suspended(client.name)):
                    # A deferred cancellation on this provider is holding an
                    # amount that could not be measured, so the expected
                    # balance is a guess: re-baseline instead of stopping the
                    # run over a difference that is probably just that money.
                    # The same goes for a provider whose baseline was never
                    # read at all - there is nothing to compare against yet.
                    try:
                        actual_balance = client.get_balance()
                        self._rebaseline(client.name, actual_balance)
                    except Exception:
                        actual_balance = None
                    log("Refund tally skipped (no trustworthy expected balance "
                        f"yet); new balance baseline: {actual_balance}", prefix=pname)
                    tally_ok = True
                else:
                    # Re-evaluated on every poll: numbers this provider buys
                    # while the refund is being waited for hold money too, and
                    # the balance it should return to moves with them.
                    def _expected_now(_name=client.name, _activation=activation_id,
                                      _fallback=expected_balance):
                        value = self._expected_balance(_name, _activation)
                        return value if value is not None else _fallback

                    tally_ok, actual_balance = self.guard.verify_refund(
                        client, _expected_now,
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
                        "screen - if the OTP shows up, enter it manually.",
                        provider=client.name,
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
        and the money stays deducted. OTPIndia answers {"type": "ACCESS_CANCEL_WAIT"}
        (its cancel window is 2 minutes from number issue). Tallies against the
        balance right now can only fail, so the cancellation is deferred and
        retried when the provider allows it.
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
        # From here on the money is tracked as a deferred hold, not as an open
        # activation: stop counting it twice in the ledger.
        self._close_activation(client.name, activation_id, number)
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

        # OTPIndia answers ACCESS_CANCEL_WAIT with the wait itself: a cancel
        # is only accepted once its cancel window (2 minutes from number
        # issue) has passed. The watcher retries after that window instead of
        # the assumed activation expiry, so the refund lands as soon as the
        # provider allows it.
        retry_after = None
        if cancel_res and cancel_res.get("type") == "ACCESS_CANCEL_WAIT":
            try:
                retry_after = max(1.0, float(cancel_res.get("seconds", 120)))
            except (TypeError, ValueError):
                retry_after = 120.0

        record = self.pending_cancels.defer(
            client, activation_id, number, reason, expected_balance,
            hold=hold, error_detail=detail, retry_after_seconds=retry_after,
        )

        if hold is None:
            self._suspend_tally(
                client.name,
                f"the deferred cancellation of {number} holds an amount that "
                f"could not be measured (the balance could not be read)"
            )

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
        try:
            retry_in = max(0.0, float(record.get("expiry_at", 0.0)) - time.time())
        except (TypeError, ValueError):
            retry_in = 0.0
        retry_note = "cancel window" if retry_after is not None else "activation expiry"
        # OTPIndia's / OTPSell's ACCESS_CANCEL_WAIT is their documented, routine
        # two-minute window. Keep it in logs/status, but don't alert the user for
        # each expected cancellation.
        if client.name not in ("otpindia", "otpsell"):
            self.notify.send(
                f"⏳ [{pname}] Cancel refused - deferred",
                f"`{number}` ({reason}): provider said `{detail or 'ERROR'}`; "
                f"retry in ~{retry_in:.0f}s ({retry_note}). Worker keeps hunting."
                + ("" if hold else "\n⚠️ Balance unreadable - refund tally "
                   "suspended until this resolves."),
                level="routine",
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
            log(f"Deferred cancel resolved; still held on {provider.upper()}: "
                f"{remaining:.4f}", prefix=provider.upper())
        # Wake a worker that ran out of balance while these cancels were
        # pending: one refund is enough for the next number.
        self._pending_resolved_event.set()

    def _drain_open_activations(self):
        """
        Close every number that is still paid for when the run ends.

        A critical stop used to abandon the number that was waiting for its
        OTP: the money stayed deducted and, if the SMS landed afterwards, it
        was spent on a code nobody was there to read. Cancelling here takes the
        refund back - or records the cancel as deferred, so the next run
        finishes it once the provider's cancel window has passed.
        """
        with self.open_activations_lock:
            snapshot = {
                pname: dict(acts)
                for pname, acts in self.open_activations.items() if acts
            }
        if not snapshot:
            return

        log("Closing the numbers that are still open before the run ends...")
        still_held = []
        for pname, acts in snapshot.items():
            client = self.client_by_name(pname)
            if client is None:
                log(f"Cannot close the open {pname.upper()} number(s): no client "
                    f"for that provider is available.", prefix=pname.upper())
                still_held.extend((pname, aid) for aid in acts)
                continue
            for activation_id, info in acts.items():
                number = (info or {}).get("number") or str(activation_id)
                try:
                    res = client.cancel(activation_id) or {}
                except Exception as exc:
                    res = {"type": "ERROR", "error": str(exc)}
                res_type = res.get("type")
                if res_type in CANCEL_SUCCESS_TYPES:
                    log(f"{number} ({activation_id}) closed on stop: {res_type}.",
                        prefix=pname.upper())
                    self._close_activation(pname, activation_id, number)
                    continue
                # Refused (OTPSell's / OTPIndia's two-minute cancel window) or
                # the call failed: hand it to the watcher so the refund is still
                # chased instead of forgotten.
                log(f"{number} ({activation_id}) could not be closed on stop "
                    f"(provider answered {res_type or 'nothing'}); recording it as "
                    f"a pending cancel so the next run finishes it.",
                    prefix=pname.upper())
                try:
                    self._defer_cancellation(
                        client, activation_id, number,
                        "Run stopped before this number was closed",
                        self._expected_balance(pname, activation_id),
                        cancel_res=res if res_type else None,
                        cancel_error=str(res.get("error") or ""),
                    )
                except Exception as exc:
                    log(f"Could not record the pending cancel for {activation_id}: {exc}",
                        prefix=pname.upper())
                self._close_activation(pname, activation_id, number)
                still_held.append((pname, activation_id))

        if still_held:
            self.notify.alert(
                "⏳ Numbers still open at stop",
                "These numbers could not be cancelled before the run ended and "
                "are recorded as pending cancels - the next run resumes them and "
                "chases the refund:\n"
                + "\n".join(f"• `{act}` ({pname.upper()})"
                            for pname, act in still_held),
            )

    # -- Parallel Worker Loop ------------------------------------------------

    def worker_loop(self, client):
        """
        Independent thread loop for a single provider.
        Fetches numbers, validates with checker, cancels if used (verifying the
        refund tally), and yields to the coordinator on an unregistered match.
        """
        pname = client.name.upper()
        log(f"Worker started.", prefix=pname)

        client_conf = self._client_conf(client)
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

        while not self._worker_should_stop(client):
            # If a target is being processed, pause fetching
            if self.target_found_event.is_set():
                self.worker_statuses[client.name] = "Paused (target being processed)"
                time.sleep(1)
                continue

            # Self-heal: if bot checker is in FloodWait cooldown, pause fetching
            # instead of buying numbers that will be cancelled.
            try:
                bot_fw = float(self.checker.bot_floodwait_remaining() or 0)
            except Exception:
                bot_fw = 0.0
            if bot_fw > 1.0:
                try:
                    self_heal_on = bool(getattr(self.checker, "self_heal_enabled", True))
                except Exception:
                    self_heal_on = True
                if self_heal_on:
                    try:
                        max_wait = float(getattr(self.checker, "self_heal_max_wait", 3600.0) or 3600.0)
                    except Exception:
                        max_wait = 3600.0
                    wait_for = bot_fw if max_wait <= 0 else min(bot_fw, max_wait)
                    log(f"Checker bot in FloodWait cooldown ({bot_fw:.0f}s left) - "
                        f"self-heal pausing {pname} for {wait_for:.0f}s before next number.",
                        prefix=pname)
                    self.worker_statuses[client.name] = f"Self-heal pause {wait_for:.0f}s (FloodWait {bot_fw:.0f}s)"
                    slept = 0
                    while slept < wait_for and not self.stop_requested.is_set():
                        if self.target_found_event.is_set():
                            break
                        chunk = min(5.0, wait_for - slept)
                        time.sleep(chunk)
                        slept += chunk
                    if self._worker_should_stop(client):
                        break
                    # Re-check after pause
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
            if self._worker_should_stop(client):
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
                elif client.name == "otpindia":
                    india_conf = self.config.get("otpindia", {})
                    res = client.get_number(
                        service=india_conf.get("service"),
                        server=india_conf.get("server")
                    )
                elif client.name == "otpsell":
                    sell_conf = self.config.get("otpsell", {})
                    res = client.get_number(
                        service=sell_conf.get("service", "meesho"),
                        country=sell_conf.get("country", "91"),
                        operator=sell_conf.get("operator"),
                        max_price=sell_conf.get("max_price")
                    )
                else:
                    otp_conf = self.config.get("otp", {})
                    res = client.get_number(
                        service=otp_conf.get("service", "12843"),
                        country=otp_conf.get("country", "in"),
                        max_price=otp_conf.get("max_price", 9.5)
                    )
            except OTPNoBalance as exc:
                if self._wait_for_pending_otpindia_refunds(client):
                    if self._worker_should_stop(client):
                        break
                    continue
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
                    "NO_BALANCE - worker stopped. Recharge, then /balance and /run."
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
                if self._wait_for_pending_otpindia_refunds(client):
                    if self._worker_should_stop(client):
                        break
                    continue
                log(f"Provider reported fatal error: NO_BALANCE (Insufficient Balance)", prefix=pname)
                self.worker_statuses[client.name] = "Stopped (NO_BALANCE)"
                self.notify.alert(
                    f"⚠️ [{pname}] Insufficient Balance",
                    "NO_BALANCE - worker stopped. Recharge, then /balance and /run."
                )
                return

            if res_type == "PRICE_TOO_HIGH":
                price = res.get("price")
                max_price = res.get("max_price")
                log(f"Price too high ({price} > max {max_price}). Worker killed.", prefix=pname)
                self.worker_statuses[client.name] = f"Stopped (PRICE_TOO_HIGH: {price} > {max_price})"
                self.notify.alert(
                    f"⚠️ [{pname}] Price Exceeded",
                    f"Price {price} > max {max_price} - worker stopped."
                )
                return

            if res_type in ("BAD_KEY", "BAD_SERVICE"):
                log(f"Provider fatal error: {res_type}", prefix=pname)
                self.worker_statuses[client.name] = f"Stopped ({res_type})"
                self.notify.alert(
                    f"❌ [{pname}] Provider Error",
                    f"{res_type} - worker stopped."
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
            # Its price is now missing from the live balance: register it so
            # every later refund tally on this provider subtracts it.
            self._open_activation(client, activation_id, clean_number)

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
                if self._worker_should_stop(client):
                    return
                continue
            except (CheckerUnavailable, CheckerError) as exc:
                msg = str(exc)
                is_flood = ("wait of" in msg.lower() and "seconds is required" in msg.lower()) or "floodwait" in msg.lower()
                # Extract FloodWait seconds for self-heal
                flood_seconds = 0
                if is_flood:
                    import re as _re
                    m = _re.search(r"wait of (\d+)", msg, _re.IGNORECASE)
                    if m:
                        try:
                            flood_seconds = int(m.group(1))
                        except Exception:
                            flood_seconds = 0
                    # Also check bot's own tracking (more accurate)
                    try:
                        remaining = float(self.checker.bot_floodwait_remaining() or 0)
                        if remaining > flood_seconds:
                            flood_seconds = int(remaining)
                    except Exception:
                        pass
                    # Fallback: try bot client directly
                    try:
                        br = float(self.checker.bot.floodwait_remaining or 0)
                        if br > flood_seconds:
                            flood_seconds = int(br)
                    except Exception:
                        pass

                if is_flood:
                    log(f"Checker error (mode {self.checker.mode}): Telegram FloodWait - {exc}. "
                        f"Both dedicated checker and PRIMES share the same Telegram account, "
                        f"so they share the rate limit. Cancelling {clean_number} with refund.",
                        prefix=pname)

                    # Self-heal mode: pause and auto-resume instead of hammering
                    try:
                        self_heal_on = bool(getattr(self.checker, "self_heal_enabled", True))
                    except Exception:
                        self_heal_on = True
                    try:
                        max_wait = float(getattr(self.checker, "self_heal_max_wait", 3600.0) or 3600.0)
                    except Exception:
                        max_wait = 3600.0

                    if self_heal_on and flood_seconds > 0:
                        # Enter bot FloodWait cooldown so other workers also pause
                        try:
                            self.checker._enter_bot_floodwait(flood_seconds)
                        except Exception:
                            pass

                        wait_for = flood_seconds
                        if max_wait > 0:
                            wait_for = min(flood_seconds, max_wait)

                        log(f"🤖 Self-heal mode ON: pausing {pname} for {wait_for:.0f}s "
                            f"(FloodWait was {flood_seconds}s, max_wait {max_wait:.0f}s) - "
                            f"will auto-resume after cooldown. No more numbers will be bought "
                            f"during this pause.", prefix=pname)
                        self.worker_statuses[client.name] = f"Self-heal pause {wait_for:.0f}s (FloodWait {flood_seconds}s)"

                        # Sleep in small chunks so stop_requested is responsive
                        slept = 0
                        while slept < wait_for and not self.stop_requested.is_set():
                            chunk = min(5.0, wait_for - slept)
                            time.sleep(chunk)
                            slept += chunk
                            # Update status with remaining
                            remaining = wait_for - slept
                            if remaining > 0 and int(remaining) % 30 == 0:
                                log(f"Self-heal: {pname} still paused, {remaining:.0f}s left...",
                                    prefix=pname)

                        if self._worker_should_stop(client):
                            return

                        log(f"Self-heal: {pname} resuming after {wait_for:.0f}s pause - "
                            f"clearing FloodWait cooldown and retrying.",
                            prefix=pname)
                        try:
                            # Clear if we waited the full FloodWait, otherwise keep remaining
                            if flood_seconds <= max_wait:
                                self.checker._clear_bot_floodwait()
                                # Also clear client-side tracking if possible
                                try:
                                    bot_client = self.bot
                                    if bot_client:
                                        with getattr(bot_client, "_floodwait_lock", threading.RLock()):
                                            bot_client._floodwait_until = 0.0
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        # Don't count FloodWait as a checker failure when self-heal is on
                        self._reset_checker_failures()
                    else:
                        if not self_heal_on:
                            log(f"Self-heal mode OFF: not auto-pausing, will continue and may hit "
                                f"FloodWait again. Enable with checker.telegram_bot.self_heal_enabled=true",
                                prefix=pname)
                else:
                    log(f"Checker error (mode {self.checker.mode}): {exc}. "
                        f"Cancelling number...", prefix=pname)
                self.handle_cancellation(client, activation_id, clean_number, f"Checker error: {exc}")
                if self._worker_should_stop(client):
                    return
                # With the bot (or the bot fallback) in the checking path, a run
                # of failures means the checker itself is broken: stop instead
                # of buying numbers only to cancel them.
                # For FloodWait with self-heal ON, we already reset the streak above,
                # so we skip counting it as a failure.
                if isinstance(exc, CheckerUnavailable) and self.checker.mode_wants_bot:
                    if is_flood:
                        try:
                            if bool(getattr(self.checker, "self_heal_enabled", True)):
                                # Self-heal handled the pause, don't count towards critical stop
                                continue
                        except Exception:
                            pass
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
            extra_info = checker_extra_info(check)
            log(f"Checker result via {checker_source}: is_registered={is_registered} "
                f"(Target: {self.target_registered})"
                + (f" [{extra_info}]" if extra_info else ""), prefix=pname)

            if is_registered != self.target_registered:
                reason = "Already registered on Meesho" if is_registered else "Not registered on Meesho"
                log(f"Number {clean_number} does not match target. Cancelling on {pname}...", prefix=pname)
                self.handle_cancellation(client, activation_id, clean_number, reason)
                if self._worker_should_stop(client):
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
                while self.target_found_event.is_set() and not self._worker_should_stop(client):
                    time.sleep(0.5)
            else:
                log(f"Another worker already claimed target. Cancelling duplicate...", prefix=pname)
                self.handle_cancellation(client, activation_id, clean_number, "Duplicate target match")
                if self._worker_should_stop(client):
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
                f"`{number}`: unexpected screen - cancelling and resetting the bot "
                f"(refund tally flags it if the SMS already went out).\n\n"
                f"{exc.screen_text[:400]}\n{buttons_line}"
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
                f"`{number}`: {exc}\nCancelling and resetting the bot (refund "
                f"tally flags it if the SMS already went out).\n"
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
            self.notify.alert(f"⚠️ [{pname}] PRIMES bot error", f"`{number}`: {exc}\nCancelling this number.")
            self.handle_cancellation(context.client, context.activation_id, number, f"Bot error: {exc}")
            return None

        if res.get("stage") == "blocked":
            self.stats.increment("user_blocked")
            log(f"Number {number} blocked by Meesho. Changing number.", prefix=pname)
            self.notify.alert(
                f"🚫 [{pname}] Number blocked by Meesho",
                f"`{number}`: {res.get('message', '')[:200]}\nChanging number.",
                level="important",
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
        referral_line = f" · Referral: {referral_action}" if referral_action else ""
        if isinstance(referral_action, str):
            if referral_action.startswith("pasted"):
                self.stats.increment("referral_pasted")
            elif referral_action.startswith(("tapped", "answered")):
                self.stats.increment("referral_skipped")
        self.notify.send(
            "📲 OTP requested via PRIMES bot",
            f"`{number}` ({pname}) · UPI ₹{res.get('upi')} · rerolls {rerolls}"
            f"{referral_line}\nWaiting for SMS...",
            level="routine", silent=True,
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
                f"`{context.clean_number}` · code `{code}` NOT submitted - use it "
                f"manually if the account is still pending.\n{exc}\n\n"
                f"{exc.screen_text[:400]}\n\n"
                "Fix /referral <link> or set meesho_bot.referral_failure_action "
                'to "skip", then /run.'
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
                f"`{context.clean_number}` · code `{code}`\n{exc}\n\n"
                f"{exc.screen_text[:400]}\n{buttons_line}"
                "Submit/verify manually if needed."
            )
            return "unknown"
        except MeeshoBotTimeout as exc:
            # The bot stopped responding while the code was being submitted /
            # verified. The code may or may not have reached the bot - check
            # it manually; the SMS was delivered, so the charge stands.
            self.notify.alert(
                f"⏱️ [{pname}] PRIMES bot timed out after OTP",
                f"`{context.clean_number}` · code `{code}`\n{exc}\n"
                "The code may or may not have reached the bot - check manually "
                "while it is valid. Changing number."
            )
            return "unknown"
        except MeeshoBotError as exc:
            self.notify.alert(
                f"⚠️ [{pname}] PRIMES bot error after OTP",
                f"`{context.clean_number}` · code `{code}`\n{exc}"
            )
            return "unknown"

        status = res.get("status", "unknown")

        if status == "linked":
            n_linked = self.stats.increment("accounts_linked")
            try:
                self.accounts.add(context.clean_number, context.provider_name,
                                  user_id=res.get("user_id"),
                                  account_number=res.get("account_number"))
            except Exception as exc:
                log(f"Note: could not record the linked account: {exc}", prefix=pname)
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
            # The charge stands: this number is spent, not pending a refund.
            self._close_activation(context.provider_name, context.activation_id,
                                   context.clean_number)
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
                f"`{context.clean_number}` · Meesho ID {res.get('user_id') or 'n/a'} · "
                f"bot #{res.get('account_number') or 'n/a'}\n"
                f"OTP {code}{' (late salvage)' if late else ''}\n"
                f"{self.linked_counts_line()}",
                level="linked",
            )
            return "linked"

        if status == "wrong_otp":
            n = self.stats.increment("otp_wrong")
            self.notify.alert(
                f"❌ [{pname}] Wrong OTP (#{n})",
                f"`{context.clean_number}` · code {code} rejected. Changing number.",
                level="important",
            )
        elif status == "otp_expired":
            n = self.stats.increment("otp_expired")
            self.notify.alert(
                f"⌛ [{pname}] OTP expired (#{n})",
                f"`{context.clean_number}` · code expired. Changing number.",
                level="important",
            )
        elif status == "blocked":
            n = self.stats.increment("user_blocked")
            self.notify.alert(
                f"🚫 [{pname}] User blocked (#{n})",
                f"`{context.clean_number}` blocked during verification. Changing number.",
                level="important",
            )
        else:
            self.notify.alert(
                f"⚠️ [{pname}] Unconfirmed bot result: {status}",
                f"`{context.clean_number}` · code `{code}`\n"
                f"{res.get('screen', '')[:300]}\nChanging number.",
                level="important",
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
        """Claim the bot for a whole login/OTP/recovery flow (blocking).

        Login outranks prewarm: if the offer pre-warm is rerolling when a
        number is found, the login waits for it to finish instead of
        cancelling the paid number. 120s covers worst-case reroll budgets.
        """
        return _BotClaim(self, "login", timeout=120, wait=True)

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
                # A checker screen means a number check took the conversation
                # over - say so, it is the one conflict worth warning about.
                if self._bot_in_checker_screen() and self._checks_can_use_primes_chat():
                    log("Parked offer prompt is gone: a number check is using "
                        "the PRIMES conversation (no dedicated checker bot, or "
                        "the check was routed to the login chat). Re-arming "
                        "the bot after the check.")
                else:
                    log("Parked offer prompt is gone; re-arming the PRIMES bot.")
                self.bot_at_number_prompt = False
            self._bot_warm_once()
            last_verified = time.time()

    def _log_dedicated_checker_state(self):
        """
        Say, once at startup, whether the dedicated checker bot is usable.

        "enabled: true" in config.json is not enough - the bot is only real
        once the userbot session can open ITS conversation. Without this line
        a check silently fell back to the PRIMES bot chat and the logs never
        explained why.
        """
        bot_checker = getattr(self.checker, "bot", None)
        if bot_checker is None:
            return
        if not getattr(bot_checker, "has_preferred", False):
            log("Checker: no dedicated checker bot configured "
                "(checker.telegram_bot.username is empty/disabled) - number "
                "checks use the PRIMES login conversation (they then wait for "
                "a login / the offer pre-warm instead of running alongside).")
            return
        summary = bot_checker.describe_preferred()
        if getattr(bot_checker, "preferred_ready", False):
            log(f"Checker: dedicated checker bot ready - {summary}; number "
                f"checks run in that conversation and never touch the PRIMES "
                f"login chat.")
        else:
            log(f"Checker: dedicated checker bot NOT usable - {summary}. Set "
                f"checker.telegram_bot.username to the bot's @handle (not its "
                f"display name) and press START in that bot once from this "
                f"Telegram account.")

    def _checker_api_preflight(self):
        """
        Best-effort v2 startup probe: liveness (GET /health, no auth), then
        the account check (GET /api/v1/me).

        Warns early when the API itself is down, the key is dead, or the Free
        plan's verified Indian proxy is missing - all otherwise surface as a
        cancelled first number. Never stops the run: the normal check path
        (and the bot fallback in auto mode) still applies per number.
        """
        if getattr(self.checker, "mode", None) == MODE_BOT:
            return
        api = getattr(self.checker, "api", None)
        # Liveness first (no auth): when the service itself is down, API
        # answers are not trusted - auto mode starts on the bot immediately
        # instead of learning it from a cancelled first number.
        health_probe = getattr(api, "health_status", None)
        if callable(health_probe):
            try:
                up, detail = health_probe()
            except Exception as exc:  # noqa: BLE001 - never block startup
                up, detail = False, f"health probe failed: {exc}"
            if not up:
                log(f"Checker API preflight: liveness probe says the API is DOWN "
                    f"({detail}) - API answers will not be trusted until it recovers.")
                self.notify.alert(
                    "⚠️ Checker API is down",
                    f"Health probe: {detail}\nauto mode: bot checks until it "
                    f"recovers; api mode: numbers are cancelled with this reason."
                )
                try:
                    self.checker.mark_api_down(f"liveness probe: {detail}")
                except Exception:
                    pass
                return
        probe = getattr(api, "get_me", None)
        if not callable(probe):
            return
        try:
            me = probe()
        except CheckerProxyError as exc:
            log(f"Checker API preflight: {exc}")
            self.notify.alert(
                "⚠️ Checker API needs an Indian proxy",
                f"{exc}\nauto mode: the bot checker covers; api mode: numbers "
                f"are bought and cancelled until this is fixed."
            )
            return
        except CheckerAuthError as exc:
            log(f"Checker API preflight: API key rejected ({exc}).")
            self.notify.alert(
                "❌ Checker API key rejected",
                f"{exc}\nCheck checker.api_keys in config.json."
            )
            return
        except Exception as exc:
            log(f"Checker API preflight: could not verify the key ({exc}); "
                f"continuing anyway - the first check will say more.")
            return
        plan = me.get("plan_name", "?") or "?"
        proxy_required = me.get("proxy_required")
        rate = me.get("rate_limit_seconds")
        rpm = me.get("requests_per_minute")
        window = f"{rate:.0f}s" if isinstance(rate, (int, float)) else "?"
        if rpm:
            window += f" (~{rpm:g}/min)"
        if proxy_required is True:
            log(f"Checker API preflight: key OK, plan '{plan}', but the account "
                f"still requires a verified Indian proxy - add one in the "
                f"Speedz Checker bot's Profile or checks will fail with 403.")
        else:
            log(f"Checker API preflight: key OK (plan '{plan}', rate window {window}).")

    def _bot_in_checker_screen(self):
        """
        Read-only: is the PRIMES bot showing its number checker right now?
        Best effort - a false negative only costs a less specific log line.
        """
        probe = getattr(self.bot, "in_checker_screen", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def _checks_can_use_primes_chat(self):
        """
        May a number check drive the PRIMES (login) Telegram conversation?

        True when no dedicated checker bot is configured, when the configured
        one IS the login bot (same @handle - it uses the one conversation the
        claim guards), or when PRIMES fallback is enabled. With a separate
        dedicated bot and fallback_to_primes=false a check NEVER taps this
        chat (the router cancels the number instead), so a checker-looking
        screen here belongs to the login flow itself and must not be reported
        as "a number check is using the conversation" - that message sent the
        user hunting for a checker conflict that did not exist.
        """
        checker = getattr(self, "checker", None)
        if checker is None:
            return True
        # api mode (or a plain CheckerClient) never uses a bot conversation.
        if not getattr(checker, "mode_wants_bot", True):
            return False
        bot_checker = getattr(checker, "bot", None)
        if bot_checker is None:
            return True
        if not getattr(bot_checker, "has_preferred", False):
            # No dedicated checker bot: checks use the PRIMES conversation.
            return True
        if bool(getattr(bot_checker, "fallback_to_primes", False)):
            return True
        try:
            return bool(bot_checker.preferred_shares_login(
                bot_checker.preferred_client))
        except Exception:
            return True

    def _bot_warm_once(self):
        """Walk the bot to an agreed offer and park it there (best effort).

        If a target number appears while we are about to warm, abort - the
        login flow will take the bot (it waits for prewarm to finish, see
        _bot_login_claim). This avoids the 'PRIMES bot is busy (owner: prewarm)'
        cancellation.
        """
        prepare = getattr(self.bot, "prepare_offer", None)
        if not callable(prepare):
            return
        # Quick check before trying to claim: don't start warming if a target
        # is already waiting to be processed.
        if self.target_found_event.is_set() or self.bot_login_active:
            return
        try:
            with _BotClaim(self, "prewarm", timeout=0, wait=False):
                # Re-check inside the claim: target may have appeared between
                # the outer check and acquiring the lock.
                if self.target_found_event.is_set() or self.bot_login_active:
                    return
                res = prepare()
        except BotBusy:
            return
        except Exception as exc:
            # A number check that drives the SAME chat (no dedicated checker
            # bot, or one mis-bound to the PRIMES bot) walks the bot off the
            # offer screen mid-reroll - that is a conflict, not a bot bug:
            # say so instead of a bare "pre-warm failed". With a separate
            # dedicated checker bot and fallback_to_primes=false no check can
            # have touched this chat, so the conflict line would be a false
            # alarm (the pre-warm's own number-prompt / "fetching your offer"
            # copy reads as checker-like) - report the real reason instead.
            if self._bot_in_checker_screen() and self._checks_can_use_primes_chat():
                log("Offer pre-warm stopped: the PRIMES bot is on its number "
                    "checker screen - a number check is using the same "
                    "conversation. Configure a dedicated checker bot "
                    "(checker.telegram_bot.username) so checks stop sharing "
                    "the login chat; the offer is re-armed after the check.")
            else:
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

    def _otp_wait_timeout(self, context):
        """
        How long to wait for the OTP on this number.

        automation.otp_timeout_seconds applies to every provider. A provider
        that refuses to cancel a number for a while after issuing it (OTPIndia:
        cancel_wait_seconds, 2 minutes from getNumber) cannot refund it before
        that window has passed anyway - giving up on the OTP earlier only parks
        the cancel in the background, and an SMS that lands in the rest of the
        window (say at 119s) is then a paid OTP nobody uses, with no refund.

        So for such a provider the wait is stretched to the end of the cancel
        window whenever the configured timeout would end before it: the number
        is only abandoned once it can actually be refunded. When the OTP was
        triggered late enough (slow bot flow, manual trigger) that the
        configured timeout already ends after the window, the configured value
        stands and the cancel goes through right after it, exactly like on the
        other providers. automation.otp_wait_covers_cancel_window=false turns
        the stretch off.
        """
        try:
            configured = max(0.0, float(self.settings.get("otp_timeout_seconds", 180) or 0.0))
        except (TypeError, ValueError):
            configured = 180.0
        if not self.settings.get("otp_wait_covers_cancel_window", True):
            return configured

        getter = getattr(context.client, "cancel_window_remaining", None)
        if not callable(getter):
            return configured
        try:
            window_left = max(0.0, float(getter(context.activation_id) or 0.0))
        except Exception:
            window_left = 0.0
        if window_left <= configured:
            return configured

        pname = context.provider_name.upper()
        window_total = getattr(context.client, "cancel_wait_seconds", None)
        window_note = (f" of {window_total:.0f}s"
                       if isinstance(window_total, (int, float)) else "")
        log(f"OTP wait for {context.clean_number} extended to {window_left:.0f}s "
            f"(cancel window{window_note} ends then; configured {configured:.0f}s) "
            f"- no refund is possible before that.", prefix=pname)
        return window_left

    def wait_for_otp(self, context):
        """
        Poll the provider for the SMS.

        Returns (kind, status):
          ("ok", status)        - OTP received in time
          ("late", status)      - OTP found by the final salvage probes after timeout
          ("cancelled", None)   - provider cancelled the activation
          ("timeout", None)     - no OTP (and no late salvage)

        The wait is automation.otp_timeout_seconds, or longer on a provider
        whose cancel window has not passed yet - see _otp_wait_timeout().
        """
        timeout = self._otp_wait_timeout(context)
        poll_interval = self.settings.get("otp_poll_interval_seconds", 3)
        start_time = time.time()
        pname = context.provider_name.upper()

        log(f"Waiting for OTP on {context.clean_number} ({pname}) (Timeout: {timeout:.0f}s)...", prefix=pname)
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
                    log(f"Waiting for OTP ({elapsed}s/{timeout:.0f}s)...", prefix=pname)
                    last_log = elapsed
            time.sleep(poll_interval)

        # FINAL SALVAGE: the SMS can land in the seconds between timeout and a
        # cancel call. Probe a few times before declaring the number dead.
        log(f"OTP wait window elapsed ({timeout:.0f}s). Running final salvage probes...", prefix=pname)
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
                f"`{context.clean_number}` · code `{code}` landed at timeout - using it.",
                level="routine",
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
        # A provider stopped by a refund tally in a previous run starts hunting
        # again: /run is the explicit "I have looked at it" signal.
        with self.stopped_providers_lock:
            self.stopped_providers.clear()
        self.total_attempts = 0
        self.bot_at_number_prompt = False
        self.bot_change_attempts = 0
        self._reset_checker_failures()

        log("=" * 60)
        log("STARTING PARALLEL MEESHO OTP AUTOMATION")
        # Snapshot the selection: a /run <provider> while this run is live
        # swaps self.clients for the NEXT run, never this one.
        clients = list(self.clients)
        log(f"Active Providers: {', '.join(c.name.upper() for c in clients)}")
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
                        f"{self.bot.start_error}\nUsing the manual OTP trigger flow. "
                        "Run login_userbot.py / check meesho_bot config for full automation."
                    )
        use_bot = self.bot.ready
        log(f"PRIMES bot flow: {'AUTO' if use_bot else 'MANUAL TRIGGER'}")
        log(f"Checker: {self.checker.describe()}")
        # Only now does "the dedicated checker bot is ready" mean anything:
        # it rides on the same userbot connection.
        self._log_dedicated_checker_state()
        # v2 account probe: a dead key or a missing Free-plan proxy is found
        # here, before the first number is bought - not on number #1's cancel.
        self._checker_api_preflight()

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
                    f"checker.mode is \"bot\" but the userbot is not ready: {reason}\n"
                    "No workers started. Fix meesho_bot or set checker.mode to "
                    "api/auto, then /run."
                )
                return
            log("Checker mode AUTO: the PRIMES bot fallback is not ready, so an API "
                "error will cancel the number as before (enable meesho_bot for the "
                "fallback).")
        log("=" * 60)

        for client in clients:
            try:
                bal = client.get_balance()
                log(f"[{client.name.upper()}] Initial Balance: {bal:.4f}")
            except Exception as exc:
                log(f"[{client.name.upper()}] Warning: Balance fetch failed: {exc}")

        workers = []
        for client in clients:
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
                        for c in clients
                    )
                    if all_reached_max:
                        summary_att = ", ".join(
                            f"{c.name.upper()}: {self.worker_attempts.get(c.name, 0)}/{self.worker_max_attempts.get(c.name, 200)}"
                            for c in clients
                        )
                        log(f"All workers reached configured max attempts ({summary_att}).")
                        self.notify.alert("Automation Finished",
                                          f"All workers hit max attempts ({summary_att}).\n"
                                          f"{self.stats.summary()}")
                    else:
                        log("All worker threads have stopped.")
                        self.notify.alert(
                            "⚠️ All OTP Workers Stopped",
                            "Every worker stopped (balance / refund mismatch / fatal error). "
                            "/balance to check, /run to restart."
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
                            # (see _BotClaim). Login outranks prewarm - it now
                            # waits up to 120s for prewarm to finish instead of
                            # raising BotBusy immediately.
                            # Retry once if prewarm was still holding the lock.
                            last_exc = None
                            for attempt in range(3):
                                try:
                                    with self._bot_login_claim():
                                        cont = self._process_target(target, use_bot)
                                    last_exc = None
                                    break
                                except BotBusy as busy_exc:
                                    # Prewarm busy should NOT cancel a paid
                                    # number - it should wait and then use the
                                    # parked offer (or reroll if needed).
                                    if "prewarm" in str(busy_exc).lower() and attempt < 2:
                                        log(f"PRIMES bot busy with prewarm while processing "
                                            f"{target.clean_number} (attempt {attempt+1}/3) - "
                                            f"waiting for prewarm to finish instead of cancelling...",
                                            prefix=target.provider_name.upper())
                                        time.sleep(3 + attempt * 2)
                                        last_exc = busy_exc
                                        continue
                                    raise
                            if last_exc is not None:
                                # Still busy after retries - treat as busy, not crash
                                raise last_exc
                        else:
                            cont = self._process_target(target, use_bot)
                    except BotBusy as exc:
                        # Login claim still busy after waiting (prewarm stuck or
                        # another login - the latter should not happen). For
                        # prewarm we WAIT and retry the same number instead of
                        # cancelling it - prewarm exists to SAVE time/numbers.
                        pname = target.provider_name.upper()
                        is_prewarm = "prewarm" in str(exc).lower()
                        if is_prewarm:
                            log(f"PRIMES bot busy with prewarm for {target.clean_number} "
                                f"after retries - keeping the number and waiting for "
                                f"prewarm to release (not cancelling).",
                                prefix=pname)
                            # Don't cancel, don't clear target - let it be
                            # retried after a short wait. The outer loop will
                            # re-enter processing for the same active_target.
                            time.sleep(5)
                            # Keep the target, don't clear, retry processing
                            # in next iteration of the outer while loop.
                            continue
                        # Genuine login busy (should not happen for login vs login
                        # because only one target is processed at a time) - fall
                        # through to generic handler which cancels with refund.
                        log(f"Unexpected BotBusy while processing {target.clean_number}: {exc}",
                            prefix=pname)
                        log(traceback.format_exc(), prefix=pname)
                        self.notify.alert(
                            f"🛑 [{pname}] Bot busy - target skipped",
                            f"`{target.clean_number}`: {exc}\nCancelled with refund tally; "
                            "search continues."
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
                                f"Bot busy: {exc}", expect_refund=True,
                            )
                        except Exception as exc2:
                            log(f"Cancellation after bot busy failed: {exc2}", prefix=pname)
                        self._clear_target()
                        cont = "continue"
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
                            f"`{target.clean_number}`: {exc}\nCancelled with refund tally; "
                            "search continues."
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
            # Never leave a paid number open: a stop that lands while a number
            # is waiting for its OTP must cancel it (refund) instead of
            # abandoning money that an incoming SMS would then spend for
            # nothing. See _drain_open_activations.
            try:
                self._drain_open_activations()
            except Exception as exc:
                log(f"Could not close the open activations on stop: {exc}")
                log(traceback.format_exc())
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
                                     f"⏭ `{target.clean_number}` skipped; searching on.",
                                     level="routine")
                    self.handle_cancellation(target.client, target.activation_id, target.clean_number, "Skipped by user")
                    self._clear_target()
                    return "continue" if not self.stop_requested.is_set() else "stop"
                if decision == "timeout":
                    self.notify.alert("Trigger Timed Out",
                                      f"No confirmation for `{target.clean_number}` - cancelling.",
                                      level="important")
                    self.handle_cancellation(target.client, target.activation_id, target.clean_number,
                                             "Manual trigger timed out")
                    self._clear_target()
                    return "continue" if not self.stop_requested.is_set() else "stop"
                self.notify.send("Trigger Confirmed",
                                 f"✅ `{target.clean_number}`: waiting for SMS...",
                                 level="routine")
            else:
                self.notify.alert("Target Number Found",
                                  f"`{target.clean_number}` ({target.provider_name.upper()})")

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
            self._close_activation(target.provider_name, target.activation_id,
                                   target.clean_number)
            self.stop_requested.set()
            return "stop"

        if kind == "cancelled":
            self.notify.alert(
                f"⚠️ [{pname}] activation cancelled",
                f"`{target.clean_number}` (activation {target.activation_id}) cancelled "
                "by the provider - checking refund, continuing.",
                level="important",
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
                f"No OTP for `{target.clean_number}` - cancelling for refund, continuing.",
                level="important",
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
                    f"`{target.clean_number}` · code `{code}`\nBot screen: "
                    f"{state or 'unknown'} - no code prompt (charge stands). "
                    "If a prompt is visible anywhere, enter the code NOW."
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
                detail = "Bot is waiting for the replacement number."
            elif outcome.get("menu_reset"):
                detail = ("Bot flow will restart from the menu "
                          f"({outcome.get('reason') or 'reset to the main menu'}).")
            else:
                detail = ("Bot stays in-flow "
                          f"({outcome.get('reason') or 'the checker API is answering'}); "
                          "the next number goes to its prompt.")
            self.notify.send(
                "🔄 Changing number",
                f"`{target.clean_number}` ({reason}). {detail}",
                level="routine",
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


def _top_level_section_span(text, section):
    """
    (inner_start, inner_end) of the TOP-LEVEL "section": {...} object in a
    config.json text, or None. Skips strings and nested objects, so a same-named
    block inside "instances" (e.g. instances.vsimpro.telegram) is never picked.
    """
    depth = 0
    index = 0
    length = len(text)
    key_re = re.compile(r'"([^"\\]|\\.)*"')
    while index < length:
        char = text[index]
        if char == '"':
            match = key_re.match(text, index)
            if not match:
                return None
            token = match.group(0)
            index = match.end()
            if depth == 1 and json.loads(token) == section:
                rest = re.match(r'\s*:\s*\{', text[index:])
                if rest:
                    inner_start = index + rest.end()
                    inner_depth = 1
                    scan = inner_start
                    while scan < length and inner_depth:
                        c = text[scan]
                        if c == '"':
                            m = key_re.match(text, scan)
                            if not m:
                                return None
                            scan = m.end()
                            continue
                        if c == "{":
                            inner_depth += 1
                        elif c == "}":
                            inner_depth -= 1
                        scan += 1
                    if inner_depth:
                        return None
                    return inner_start, scan - 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return None


def set_notify_level(config_path, level):
    """
    Write telegram.notify_level into config.json in place (top-level "telegram"
    block; ordering and other keys untouched). Returns True.
    """
    normalized = normalize_notify_level(level, default=None)
    if normalized is None:
        raise ValueError(f"unknown notify level: {level!r} (use all, normal or quiet)")
    level = normalized

    with open(config_path, "r", encoding="utf-8") as f:
        text = f.read()

    span = _top_level_section_span(text, "telegram")
    if span is None:
        raise ValueError(f'no top-level "telegram" section found in {config_path}')
    inner_start, inner_end = span
    inner = text[inner_start:inner_end]
    quoted = json.dumps(level)

    existing = re.search(r'("notify_level"\s*:\s*)("(?:[^"\\]|\\.)*")', inner)
    if existing:
        new_inner = inner[:existing.start(2)] + quoted + inner[existing.end(2):]
    else:
        new_inner = '\n    "notify_level": ' + quoted + "," + inner

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
                        help="Provider(s): tempora, vsimpro, otpdoctor, otpcart, otpindia, "
                             "'all', or comma-combinations e.g. 'tempora,vsimpro'")
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
        log("Listening for Telegram commands (/run [provider], /status, /balance, /stop). Press Ctrl+C to exit.")
        try:
            while True:
                coordinator.notify.telegram.poll_signal({"run": "run", "stop": "stop"})
                time.sleep(2)
        except KeyboardInterrupt:
            log("Exiting.")


if __name__ == "__main__":
    main()
