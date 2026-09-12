"""
Meesho OTP Automation Orchestrator.
Supports true parallel multi-threaded worker execution for OtpDoctor and TemporaSMS,
independent cancellation wait queues, interactive Telegram bot commands (/run, /status, /balance, /stop),
and robust checker validation.
"""

import argparse
import json
import os
import sys
import threading
import time
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
from state import StateStore
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

        self.checker = CheckerClient(
            base_url=self.checker_conf.get("base_url", "https://superassets.in"),
            api_key=self.checker_conf.get("api_key", "")
        )

        self.checker_service = self.checker_conf.get("service", "meesho")
        self.target_registered = self.settings.get("target_registered", False)

        self.state = StateStore()
        self.notify = Notifier(config)

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
        self.attempts_lock = threading.Lock()
        self.is_running = False

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
            stop_cb=self.request_stop
        )

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
            f"Mode: {self.config.get('active_otp_provider', 'both').upper()}"
        ]

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

        if self.worker_statuses:
            lines.append("\nWorkers:")
            for name, st in self.worker_statuses.items():
                lines.append(f"  • {name.upper()}: {st}")

        lines.append("\n" + self.get_balances_summary())
        return "\n".join(lines)

    def request_run(self):
        if self.is_running:
            log("Restart requested while running. Stopping current workers first...")
            self.stop_requested.set()
            time.sleep(1.5)
        threading.Thread(target=self.run, daemon=True).start()

    def request_stop(self):
        self.stop_requested.set()
        log("Stop requested via command.")

    # -- Cancellation & Cooldown Logic --------------------------------------

    def handle_cancellation(self, client, activation_id, number, reason):
        """
        Cancels an activation on the specific provider client to trigger refund.
        Any wait cooldown (e.g. WAIT_CANCEL:120 on OtpDoctor) happens strictly
        inside the calling thread without blocking other providers.
        """
        pname = client.name.upper()
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
                log(f"Retrying cancellation for activation {activation_id}...", prefix=pname)
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
                log(f"Retrying cancellation for activation {activation_id}...", prefix=pname)
                cancel_res = client.cancel(activation_id)
                log(f"Cancellation response after cooldown: {cancel_res}", prefix=pname)
            except Exception as exc:
                log(f"Error while retrying cancellation {activation_id}: {exc}", prefix=pname)

        refund_delay = self.settings.get("refund_check_delay_seconds", 2)
        if refund_delay > 0:
            time.sleep(refund_delay)

        try:
            curr_bal = client.get_balance()
            if curr_bal < 5.0 and hasattr(client, "wait_for_usable_balance"):
                log(f"Balance is {curr_bal:.4f}. Waiting for refund ledger update...", prefix=pname)
                client.wait_for_usable_balance(min_balance=5.0)
                curr_bal = client.get_balance()
            log(f"Balance after cancellation: {curr_bal:.4f}", prefix=pname)
        except Exception:
            pass

        self.state.save({
            "status": "CANCELLED",
            "provider": client.name,
            "activation_id": activation_id,
            "number": number,
            "reason": reason,
            "cancelled_at": now()
        })

    # -- Parallel Worker Loop ------------------------------------------------

    def worker_loop(self, client):
        """
        Independent thread loop for a single provider.
        Fetches numbers, validates with checker, cancels if used, and yields to
        coordinator when an unregistered match is found.
        """
        pname = client.name.upper()
        log(f"Worker started.", prefix=pname)

        client_conf = self.config.get(client.name, {}) or (self.config.get("otp", {}) if client.name == "otpdoctor" else {})
        client_max_attempts = client_conf.get("max_attempts") or self.settings.get("max_attempts", 200)
        self.worker_max_attempts[client.name] = client_max_attempts
        self.worker_attempts[client.name] = 0
        retry_delay = self.settings.get("retry_delay_seconds", 1.0)

        # Initial provider balance
        try:
            bal = client.get_balance()
            log(f"Initial balance: {bal:.4f} (Max Attempts: {client_max_attempts})", prefix=pname)
        except Exception as exc:
            log(f"Warning: Could not fetch initial balance: {exc}", prefix=pname)

        while not self.stop_requested.is_set():
            # If a target was found by either worker, pause fetching
            if self.target_found_event.is_set():
                self.worker_statuses[client.name] = "Paused (Target found by a worker)"
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

            self.worker_statuses[client.name] = f"Attempt {current_attempt}/{client_max_attempts}: Requesting number"
            log(f"Requesting number (Attempt {current_attempt}/{client_max_attempts})...", prefix=pname)

            # Request number based on provider type
            try:
                if client.name == "tempora":
                    res = client.get_number()
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
                    f"Once recharged:\n"
                    f"• Send /balance to verify your updated balance\n"
                    f"• Send /run to restart search"
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

            res_type = res.get("type")

            if res_type in ("TRY_AGAIN", "NO_NUMBERS"):
                log(f"Provider reported {res_type}. Waiting {retry_delay}s...", prefix=pname)
                time.sleep(retry_delay)
                continue

            if res_type == "NO_BALANCE":
                balance_wait = getattr(client, "balance_wait_seconds", 0) or client_conf.get("balance_update_delay_seconds", 0)
                if balance_wait > 0:
                    log(f"Provider reported NO_BALANCE. Waiting up to {balance_wait}s for balance/refund update...", prefix=pname)
                    self.worker_statuses[client.name] = f"Waiting balance update (up to {balance_wait}s)"
                    deadline = time.time() + balance_wait
                    restored = False
                    while time.time() < deadline and not self.stop_requested.is_set():
                        time.sleep(3.0)
                        try:
                            bal = client.get_balance()
                            if bal >= 5.0:
                                log(f"Balance updated to {bal:.4f}. Resuming search...", prefix=pname)
                                restored = True
                                break
                        except Exception:
                            pass

                    if restored:
                        continue

                log(f"Provider reported fatal error: NO_BALANCE (Insufficient Balance)", prefix=pname)
                self.worker_statuses[client.name] = "Stopped (NO_BALANCE)"
                self.notify.alert(
                    f"⚠️ [{pname}] Insufficient Balance",
                    f"Provider {pname} reported NO_BALANCE.\n"
                    f"Please recharge your account.\n\n"
                    f"Once recharged:\n"
                    f"• Send /balance to verify your updated balance\n"
                    f"• Send /run to restart search"
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

            # Check registration on Meesho checker
            self.worker_statuses[client.name] = f"Checking registration for {clean_number}"
            log(f"Checking {clean_number} on {self.checker_service} checker...", prefix=pname)

            try:
                check = self.checker.check(self.checker_service, clean_number)
            except (CheckerUnavailable, CheckerError) as exc:
                log(f"Checker error: {exc}. Cancelling number...", prefix=pname)
                self.handle_cancellation(client, activation_id, clean_number, f"Checker error: {exc}")
                continue

            is_registered = check.get("is_registered", False)
            log(f"Checker result: is_registered={is_registered} (Target: {self.target_registered})", prefix=pname)

            # Evaluate registration
            if is_registered != self.target_registered:
                # Already registered / used number -> Cancel on provider (refund)
                reason = "Already registered on Meesho" if is_registered else "Not registered on Meesho"
                log(f"Number {clean_number} does not match target. Cancelling on {pname}...", prefix=pname)
                self.handle_cancellation(client, activation_id, clean_number, reason)
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
                    "target_registered": self.target_registered,
                    "found_at": now()
                })
                # Worker waits here OUTSIDE the lock while coordinator handles manual trigger & OTP
                while self.target_found_event.is_set() and not self.stop_requested.is_set():
                    time.sleep(0.5)
            else:
                # Another worker beat us to target, cancel ours
                log(f"Another worker already claimed target. Cancelling duplicate...", prefix=pname)
                self.handle_cancellation(client, activation_id, clean_number, "Duplicate target match")

        log(f"Worker stopped.", prefix=pname)
        self.worker_statuses[client.name] = "Stopped"

    # -- Manual Trigger & OTP Resolution (Main Thread) ----------------------

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
                st_type = st.get("type")
                if st_type == "STATUS_OK":
                    log(f"OTP arrived before confirmation - proceeding immediately.", prefix=context.provider_name.upper())
                    return "go"
                if st_type == "STATUS_CANCEL":
                    log(f"Activation cancelled remotely while waiting.", prefix=context.provider_name.upper())
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
                sms = status_res.get("sms", "")
                code = status_res.get("code") or sms

                log(f"🎉 [SUCCESS] OTP Received! Code: {code}", prefix=pname)
                log(f"Full SMS Content: {sms}", prefix=pname)

                self.state.save({
                    "status": "COMPLETED",
                    "provider": context.provider_name,
                    "activation_id": context.activation_id,
                    "number": context.clean_number,
                    "otp_code": code,
                    "sms": sms,
                    "completed_at": now()
                })

                self.notify.otp_result(code, context.clean_number, sms, provider_name=context.provider_name)

                if self.settings.get("auto_finish_activation", True):
                    try:
                        context.client.finish(context.activation_id)
                        log(f"Activation {context.activation_id} finished successfully.", prefix=pname)
                    except Exception as exc:
                        log(f"Note: Could not complete activation: {exc}", prefix=pname)

                return True

            elif status_type == "STATUS_WAIT_CODE":
                if elapsed - last_log >= 15:
                    log(f"Waiting for OTP ({elapsed}s/{timeout}s)...", prefix=pname)
                    last_log = elapsed
                time.sleep(poll_interval)

            elif status_type == "STATUS_CANCEL":
                log(f"Activation was cancelled remotely.", prefix=pname)
                return False

            else:
                time.sleep(poll_interval)

        log(f"OTP timed out after {timeout}s.", prefix=pname)
        return False

    # -- Orchestration Run Method --------------------------------------------

    def run(self):
        self.is_running = True
        self.stop_requested.clear()
        self.target_found_event.clear()
        self.total_attempts = 0

        log("=" * 60)
        log("STARTING PARALLEL MEESHO OTP AUTOMATION")
        log(f"Active Providers: {', '.join(c.name.upper() for c in self.clients)}")
        log("=" * 60)

        # Initial Balances
        for client in self.clients:
            try:
                bal = client.get_balance()
                log(f"[{client.name.upper()}] Initial Balance: {bal:.4f}")
            except Exception as exc:
                log(f"[{client.name.upper()}] Warning: Balance fetch failed: {exc}")

        # Spawn worker threads
        workers = []
        for client in self.clients:
            t = threading.Thread(target=self.worker_loop, args=(client,), name=f"Worker-{client.name}", daemon=True)
            workers.append(t)
            t.start()

        max_attempts = self.settings.get("max_attempts", 200)

        try:
            while not self.stop_requested.is_set():
                # Check if all worker threads have stopped
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
                        self.notify.alert(
                            "Automation Finished",
                            f"All workers finished their max attempts ({summary_att}) without finding target."
                        )
                    else:
                        log("All worker threads have stopped.")
                        self.notify.alert(
                            "⚠️ All OTP Workers Stopped",
                            "All OTP worker threads have stopped (e.g. insufficient balance or fatal errors).\n\n"
                            "Please recharge your account.\n"
                            "Once recharged:\n"
                            "• Send /balance to check new balance\n"
                            "• Send /run to restart search"
                        )
                    break

                # Wait for a worker to find a target number
                if self.target_found_event.wait(timeout=1.0):
                    with self.active_target_lock:
                        target = self.active_target

                    if not target:
                        continue

                    log(f"\n>>> PROCESSING TARGET NUMBER: {target.clean_number} ({target.provider_name.upper()}) <<<\n")

                    # Manual trigger gate
                    if self.settings.get("require_manual_trigger", True):
                        decision = self.wait_for_manual_trigger(target)

                        if decision == "skip":
                            log(f"User skipped {target.clean_number}. Cancelling and continuing search...")
                            self.notify.send(
                                "Number Skipped",
                                f"⏭ Number {target.clean_number} was skipped.\nContinuing search for next number..."
                            )
                            self.handle_cancellation(target.client, target.activation_id, target.clean_number, "Skipped by user")
                            with self.active_target_lock:
                                self.active_target = None
                            self.target_found_event.clear()
                            continue

                        if decision == "timeout":
                            log(f"Trigger timed out for {target.clean_number}. Cancelling...")
                            self.notify.alert("Trigger Timed Out", f"No trigger confirmation for {target.clean_number}.\nCancelling and continuing search...")
                            self.handle_cancellation(target.client, target.activation_id, target.clean_number, "Manual trigger timed out")
                            with self.active_target_lock:
                                self.active_target = None
                            self.target_found_event.clear()
                            continue

                        log("Trigger confirmed. Waiting for OTP...")
                        self.state.save({
                            "status": "TRIGGER_CONFIRMED",
                            "provider": target.provider_name,
                            "activation_id": target.activation_id,
                            "number": target.clean_number,
                            "confirmed_at": now()
                        })
                        self.notify.send(
                            "Trigger Confirmed",
                            f"✅ OTP Triggered for {target.clean_number} ({target.provider_name.upper()}).\nWaiting for SMS code..."
                        )
                    else:
                        self.notify.alert("Target Number Found", f"Number: {target.clean_number}\nProvider: {target.provider_name}")

                    # Wait for OTP SMS
                    otp_success = self.wait_for_otp(target)

                    if otp_success:
                        log("Process completed successfully!")
                        self.stop_requested.set()
                        break
                    else:
                        log(f"OTP failed or timed out for {target.clean_number}. Cancelling...")
                        self.notify.alert(
                            f"⚠️ [{target.provider_name.upper()}] OTP Timed Out",
                            f"OTP was not received for {target.clean_number}.\nNumber cancelled for refund. Continuing search..."
                        )
                        self.handle_cancellation(target.client, target.activation_id, target.clean_number, "OTP timeout")
                        with self.active_target_lock:
                            self.active_target = None
                        self.target_found_event.clear()
                        continue

        finally:
            self.stop_requested.set()
            for t in workers:
                t.join(timeout=2.0)
            self.is_running = False
            log("Automation run finished.")


def load_config():
    for config_name in CONFIG_FILES:
        if os.path.exists(config_name):
            try:
                with open(config_name, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                log(f"Error reading {config_name}: {exc}")
    raise FileNotFoundError(f"Neither {', '.join(CONFIG_FILES)} could be found.")


def main():
    parser = argparse.ArgumentParser(description="Meesho OTP Automation with Dual Parallel Clients.")
    parser.add_argument("--provider", choices=["both", "tempora", "otpdoctor"], help="Override active OTP provider")
    parser.add_argument("--balance", action="store_true", help="Print live balances for all providers and exit")
    parser.add_argument("--daemon", action="store_true", help="Keep Telegram command listener alive after runs")

    args = parser.parse_args()

    try:
        config = load_config()
    except Exception as exc:
        log(f"Fatal: {exc}")
        return

    coordinator = ParallelAutomationCoordinator(config, provider_override=args.provider)

    if args.balance:
        print(coordinator.get_balances_summary())
        return

    # Start run
    coordinator.run()

    # If Telegram is configured, keep a lightweight listener alive for /run commands
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