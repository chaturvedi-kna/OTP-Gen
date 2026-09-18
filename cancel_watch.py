"""
Deferred cancellations: what to do when a provider refuses to cancel.

TemporaSMS and VSImpro answer a cancel that arrives too early with a plain

    {"type": "ERROR"}

instead of ACCESS_CANCEL. That is NOT a refund: the activation is still open,
the money is still deducted and the number is still waiting for an SMS. The old
behaviour was to fall straight through to the refund tally, which then reported

    [BALANCE-GUARD] REFUND MISMATCH ... -> CRITICAL STOP

for a refund that was never going to arrive yet - and it stopped the whole
automation over a timing problem, not over lost money.

What happens instead now:

  1. The cancellation is DEFERRED and handed to a background watcher thread.
     The worker that owned the number is free again immediately (it keeps
     buying and checking numbers), and the amount still tied up is booked as
     a "hold" on that provider's balance ledger, so every refund tally made in
     the meantime stays correct instead of reporting a false mismatch.

  2. The watcher polls the activation until it is expected to expire
     (automation.cancel_error_expiry_seconds, default 900 - providers do not
     publish the activation lifetime, so it is assumed).

  3. An OTP that lands while waiting is NOT lost: it is reported with its code
     (the SMS was delivered, so that charge legitimately stands and the
     cancellation is not retried).

  4. At expiry the cancel is retried (bounded), and only then is the refund
     tally run - a mismatch after that is a real one and still stops the run.

Pending cancellations are persisted (pending_cancels.json), so restarting the
tool after a crash resumes the watchers instead of silently losing the money.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

from runtime import (DEFAULT_PENDING_FILE, DEFAULT_DISPUTE_FILE,
                     namespaced_name, dispute_filename)

# setStatus answers that mean "the activation is closed / refunded".
CANCEL_SUCCESS_TYPES = {
    "ACCESS_CANCEL",
    "ACCESS_CANCEL_ALREADY",
    "ACCESS_ACTIVATION",
    "STATUS_CANCEL",
}

# Answers that mean "not yet, try later": the wait is resumed, not abandoned.
CANCEL_RETRY_TYPES = {"ERROR", "WAIT_CANCEL", "EARLY_CANCEL_DENIED",
                      "NO_ACTIVATION", "TRY_AGAIN", "TOO_MANY_REQUESTS"}

DEFAULT_SETTINGS = {
    # Assumed activation lifetime when the provider does not publish one.
    "cancel_error_expiry_seconds": 900,
    # Extra safety margin after the assumed expiry before the cancel is retried.
    "cancel_error_grace_seconds": 10,
    # How often the deferred activation is polled for a late OTP meanwhile.
    "cancel_error_poll_interval_seconds": 20,
    # Cancel retries once the activation should be expired.
    "cancel_error_retry_attempts": 3,
    "cancel_error_retry_delay_seconds": 30,
    # Hard cap for one deferred cancellation (a stuck provider must not keep a
    # watcher alive forever); on expiry it is reported, not silently dropped.
    "cancel_error_max_wait_seconds": 1800,
}

SETTING_ALIASES = {
    "expiry_seconds": "cancel_error_expiry_seconds",
    "grace_seconds": "cancel_error_grace_seconds",
    "poll_interval_seconds": "cancel_error_poll_interval_seconds",
    "retry_attempts": "cancel_error_retry_attempts",
    "retry_delay_seconds": "cancel_error_retry_delay_seconds",
    "max_wait_seconds": "cancel_error_max_wait_seconds",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def resolve_settings(settings):
    """Merge the automation config over the defaults (aliases accepted)."""
    merged = dict(DEFAULT_SETTINGS)
    for key, value in (settings or {}).items():
        name = SETTING_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        if name in DEFAULT_SETTINGS and value is not None:
            try:
                merged[name] = float(value)
            except (TypeError, ValueError):
                continue
    merged["cancel_error_retry_attempts"] = max(1, int(merged["cancel_error_retry_attempts"]))
    for key in ("cancel_error_expiry_seconds", "cancel_error_grace_seconds",
                "cancel_error_poll_interval_seconds", "cancel_error_retry_delay_seconds",
                "cancel_error_max_wait_seconds"):
        merged[key] = max(0.0, float(merged[key]))
    return merged


class DisputeLog:
    """
    Persistent, append-only complaint evidence: every time the provider
    REFUSES a cancellation (cancel returned ERROR) and an OTP still arrives
    for that activation, one full record per activation is appended here so
    the user can later raise a complaint with the provider (proof that the
    number that could not be cancelled did receive its SMS).

    JSONL (one JSON object per line), namespaced per instance like the other
    runtime files so parallel tabs never share it.
    """

    def __init__(self, filename=DEFAULT_DISPUTE_FILE, instance=None):
        if instance and filename == DEFAULT_DISPUTE_FILE:
            filename = namespaced_name(filename, instance)
        self.path = filename
        self._lock = threading.Lock()

    def append(self, record):
        entry = {"recorded_at": now()}
        entry.update(record or {})
        entry.setdefault("type", "otp_after_cancel_refused")
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except Exception:
                pass
        return entry

    def records(self):
        entries = []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
        except OSError:
            pass
        return entries


class PendingCancelStore:
    """
    Tiny JSON store for cancellations that are still waiting for expiry.

    Written atomically (tmp + replace) so a crash mid-write cannot lose the
    record of money that is still held by the provider.
    """

    def __init__(self, filename=DEFAULT_PENDING_FILE, instance=None):
        if instance and filename == DEFAULT_PENDING_FILE:
            filename = namespaced_name(filename, instance)
        self.path = filename
        self._lock = threading.Lock()
        self._records = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return
        if isinstance(data, dict):
            for activation_id, record in data.items():
                if isinstance(record, dict):
                    record.setdefault("activation_id", activation_id)
                    self._records[str(activation_id)] = record
        elif isinstance(data, list):
            for record in data:
                if isinstance(record, dict) and record.get("activation_id"):
                    self._records[str(record["activation_id"])] = record

    def _save_locked(self):
        payload = json.dumps(self._records, indent=2)
        temporary = f"{self.path}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temporary, self.path)
        except Exception:
            try:
                with open(self.path, "w", encoding="utf-8") as handle:
                    handle.write(payload)
            except Exception:
                pass

    def all(self):
        with self._lock:
            return [dict(record) for record in self._records.values()]

    def get(self, activation_id):
        with self._lock:
            record = self._records.get(str(activation_id))
            return dict(record) if record else None

    def add(self, record):
        with self._lock:
            self._records[str(record["activation_id"])] = dict(record)
            self._save_locked()

    def update(self, activation_id, **fields):
        with self._lock:
            record = self._records.get(str(activation_id))
            if record is None:
                return None
            record.update(fields)
            self._save_locked()
            return dict(record)

    def remove(self, activation_id):
        with self._lock:
            if self._records.pop(str(activation_id), None) is not None:
                self._save_locked()
                return True
            return False


class CancelWatchManager:
    """
    Owns the deferred cancellations of one run.

    It talks to the coordinator through a small set of hooks (see
    `ParallelAutomationCoordinator`), so it can be driven by a fake in tests:

      log(message, prefix="")             - logging
      stats                               - StatsStore
      notify                              - Notifier
      guard                               - BalanceGuard
      settings                            - config["automation"]
      stop_requested                      - threading.Event
      client_by_name(name)                - provider client lookup
      expected_balance(name, activation_id)   - ledger, holds already deducted
      note_balance(name, balance)         - ledger update
      critical_stop(title, message)       - hard stop + alert
    """

    def __init__(self, owner, store=None, log_fn=None, disputes=None):
        self.owner = owner
        self.store = store or PendingCancelStore()
        if disputes is None:
            instance = getattr(owner, "instance", None)
            disputes = DisputeLog(dispute_filename(instance))
        self.disputes = disputes
        self._log_fn = log_fn or (lambda message, prefix="": None)
        self._threads = {}
        self._lock = threading.Lock()

    # -- helpers -------------------------------------------------------------

    def _log(self, message, prefix=""):
        try:
            self._log_fn(message, prefix)
        except Exception:
            pass

    @property
    def settings(self):
        return resolve_settings(getattr(self.owner, "settings", {}) or {})

    def pending(self):
        """Snapshot of the cancellations still waiting (for /status)."""
        records = self.store.all()
        now_ts = time.time()
        for record in records:
            record["seconds_to_expiry"] = max(0.0, float(record.get("expiry_at", now_ts)) - now_ts)
        return records

    def has_pending(self, provider_name=None, exclude=None):
        for record in self.store.all():
            if provider_name and record.get("provider") != provider_name:
                continue
            if exclude and str(record.get("activation_id")) == str(exclude):
                continue
            return True
        return False

    def hold_total(self, provider_name=None, exclude=None):
        """
        Money still held by deferred activations, plus a flag telling whether
        every one of those holds is known.

        A hold is unknown when the balance could not be read at defer time; the
        coordinator then suspends the refund tally for that provider instead of
        critical-stopping on a difference it cannot explain.
        """
        total = 0.0
        unknown = False
        for record in self.store.all():
            if provider_name and record.get("provider") != provider_name:
                continue
            if exclude and str(record.get("activation_id")) == str(exclude):
                continue
            hold = record.get("hold")
            if hold is None:
                unknown = True
            else:
                try:
                    total += float(hold)
                except (TypeError, ValueError):
                    unknown = True
        return total, unknown

    # -- deferring -----------------------------------------------------------

    def defer(self, client, activation_id, number, reason, expected_balance,
              hold=None, error_detail=""):
        """
        Hand a refused cancellation to a background watcher.

        Returns the pending record. The caller treats it as "the number is on
        its way out" - it must not run a refund tally for it.
        """
        settings = self.settings
        now_ts = time.time()
        record = {
            "provider": client.name,
            "activation_id": str(activation_id),
            "number": str(number),
            "reason": reason,
            "error_detail": str(error_detail or "")[:300],
            "expected_balance": expected_balance,
            "hold": hold,
            "hold_unknown": hold is None,
            "deferred_at": now(),
            "deferred_at_epoch": now_ts,
            "expiry_at": now_ts + settings["cancel_error_expiry_seconds"],
            "expiry_assumed": True,
            "attempts": 0,
            "notifications": 0,
        }
        self.store.add(record)
        self._start(record)
        self._log(
            f"[DEFERRED-CANCEL] The provider refused the cancel with ERROR; the "
            f"activation stays open and is retried at expiry "
            f"(~{settings['cancel_error_expiry_seconds']:.0f}s). The worker keeps "
            f"hunting - {number} / {activation_id}.",
            client.name.upper()
        )
        return record

    def resume(self):
        """
        Restart watchers for cancellations left over from a previous run.

        Their expiry is in the past, so the retry happens immediately; anything
        that is still open is reported instead of forgotten.
        """
        for record in self.store.all():
            if str(record.get("activation_id")) in self._threads:
                continue
            self._log(
                f"[DEFERRED-CANCEL] Resuming the pending cancel of "
                f"{record.get('number')} ({record.get('provider')}) left over from "
                f"a previous run.",
                str(record.get("provider", "")).upper()
            )
            self._start(record, resumed=True)

    def _start(self, record, resumed=False):
        activation_id = str(record["activation_id"])
        with self._lock:
            thread = self._threads.get(activation_id)
            if thread is not None and thread.is_alive():
                return thread
            thread = threading.Thread(
                target=self._watch,
                args=(activation_id,),
                kwargs={"resumed": resumed},
                name=f"CancelWatch-{record.get('provider')}-{activation_id[:8]}",
                daemon=True,
            )
            self._threads[activation_id] = thread
            thread.start()
            return thread

    def _client_for(self, record):
        getter = getattr(self.owner, "client_by_name", None)
        if not callable(getter):
            return None
        return getter(record.get("provider"))

    # -- the watcher ---------------------------------------------------------

    def _watch(self, activation_id, resumed=False):
        record = self.store.get(activation_id)
        if not record:
            return
        client = self._client_for(record)
        pname = str(record.get("provider", "")).upper()
        if client is None:
            self._log(
                f"[DEFERRED-CANCEL] No client for {record.get('provider')}; the "
                f"pending cancel of {record.get('number')} stays on disk for the "
                f"next run.",
                pname
            )
            return

        settings = self.settings
        number = record.get("number")
        expiry = float(record.get("expiry_at", time.time()))
        grace = settings["cancel_error_grace_seconds"]
        poll = max(1.0, settings["cancel_error_poll_interval_seconds"])
        deadline = time.time() + settings["cancel_error_max_wait_seconds"]
        stop = getattr(self.owner, "stop_requested", None)

        if resumed:
            self._notify_resumed(record, pname)

        # ---- 1) wait for expiry, watching for a late OTP -------------------
        otp_status = None
        while True:
            if stop is not None and stop.is_set():
                # The run is stopping: the record stays on disk so the next
                # run finishes the cancellation instead of losing the money.
                self._log(
                    f"[DEFERRED-CANCEL] Run is stopping; {number} stays pending "
                    f"and is retried on the next start.",
                    pname
                )
                return

            status = None
            try:
                status = client.get_status(activation_id)
            except Exception as exc:
                self._log(f"[DEFERRED-CANCEL] Status check failed: {exc}", pname)

            if status and status.get("type") == "STATUS_OK":
                otp_status = status
                break
            if status and status.get("type") == "STATUS_CANCEL":
                self.store.update(activation_id, cancelled_by_provider=True)
                break

            now_ts = time.time()
            if now_ts >= expiry + grace:
                break
            if now_ts >= deadline:
                self._log(
                    f"[DEFERRED-CANCEL] Gave up waiting for the expiry of {number} "
                    f"after {settings['cancel_error_max_wait_seconds']:.0f}s; "
                    f"retrying the cancel now.",
                    pname
                )
                break
            sleep_for = min(poll, max(1.0, expiry + grace - now_ts))
            if stop is not None:
                stop.wait(sleep_for)
            else:
                time.sleep(sleep_for)

        # ---- 2) the SMS made it: report it, the charge stands --------------
        if otp_status is not None:
            self._resolve_otp(record, otp_status, client)
            return

        if stop is not None and stop.is_set():
            self._log(
                f"[DEFERRED-CANCEL] Run is stopping before the retry; {number} "
                f"stays pending for the next run.",
                pname
            )
            return

        # ---- 3) retry the cancel now that the activation should be expired --
        attempts = int(settings["cancel_error_retry_attempts"])
        delay = settings["cancel_error_retry_delay_seconds"]
        cancel_res = None
        for attempt in range(1, attempts + 1):
            try:
                cancel_res = client.cancel(activation_id)
            except Exception as exc:
                cancel_res = None
                self._log(f"[DEFERRED-CANCEL] Cancel retry {attempt} failed: {exc}", pname)
            self.store.update(activation_id, attempts=attempt,
                             last_attempt_at=now())
            if cancel_res:
                self._log(
                    f"[DEFERRED-CANCEL] Cancel retry {attempt}/{attempts} for "
                    f"{number}: {cancel_res}",
                    pname
                )
                res_type = cancel_res.get("type")
                if res_type in CANCEL_SUCCESS_TYPES:
                    break
                if res_type == "WAIT_CANCEL":
                    wait_seconds = cancel_res.get("seconds", delay)
                    if attempt < attempts:
                        time.sleep(max(1.0, float(wait_seconds)))
                    continue
                if res_type in CANCEL_RETRY_TYPES:
                    if attempt < attempts:
                        time.sleep(max(1.0, delay))
                    continue
                break
            elif attempt < attempts:
                time.sleep(max(1.0, delay))

        res_type = (cancel_res or {}).get("type")
        self._resolve_refund(record, client, res_type, pname)

    # -- resolution ----------------------------------------------------------

    def _notify_resumed(self, record, pname):
        notify = getattr(self.owner, "notify", None)
        if notify is None:
            return
        try:
            notify.alert(
                f"♻️ [{pname}] Pending cancel resumed",
                f"Number: {record.get('number')}\\nActivation: "
                f"{record.get('activation_id')}\\n\\n"
                f"This cancellation was refused by the provider during an earlier "
                f"run (ERROR) and is still open. It is being retried now; if the "
                f"refund still does not tally, the run stops as usual."
            )
        except Exception:
            pass

    def _resolve_otp(self, record, status, client):
        """The OTP landed while the cancellation was waiting: report the code."""
        activation_id = str(record["activation_id"])
        pname = str(record.get("provider", "")).upper()
        number = record.get("number")
        code = status.get("code") or status.get("sms") or ""
        sms = status.get("sms", "")

        stats = getattr(self.owner, "stats", None)
        if stats is not None:
            try:
                stats.increment("cancel_deferred_otp")
                stats.increment("late_otp_salvaged")
                stats.increment("numbers_consumed")
            except Exception:
                pass

        self._log(
            f"🚨 [DEFERRED-CANCEL] OTP {code} arrived for {number} while the "
            f"cancellation was waiting for expiry - the SMS was delivered, so "
            f"this charge stands (no cancel retry).",
            pname
        )

        notify = getattr(self.owner, "notify", None)
        if notify is not None:
            try:
                notify.alert(
                    f"🚨 [{pname}] OTP arrived on a number being cancelled",
                    f"Number: {number}\\nActivation: {activation_id}\\n"
                    f"Deferred because: {record.get('reason')}\\n\\n"
                    f"Code: `{code}`\\nSMS: {sms}\\n\\n"
                    f"The provider refused the cancel with ERROR, so the number was "
                    f"kept until expiry - and the OTP arrived in the meantime. The "
                    f"SMS was delivered, so the charge stands (no refund is due) "
                    f"and the activation is left open for the code to be used."
                )
            except Exception:
                pass

        # Complaint evidence: the provider refused to cancel this activation
        # (cancel returned ERROR) and the OTP arrived anyway - keep a full,
        # persistent record so the user can raise a complaint later.
        try:
            self.disputes.append({
                "provider": str(record.get("provider", "")),
                "activation_id": activation_id,
                "number": number,
                "reason": record.get("reason"),
                "deferred_at": record.get("at"),
                "otp_code": code,
                "otp_sms": sms,
                "otp_received_at": now(),
                "expected_balance": record.get("expected_balance"),
                "source": "deferred_cancel_watch",
            })
            self._log(f"Complaint record saved for activation {activation_id} "
                      f"({self.disputes.path}).", pname)
        except Exception:
            pass

        # The money for this activation is spent: the live balance is the
        # baseline for everything that follows.
        self._settle_ledger(record, client, pname, consumed=True)
        self._resolved(record)
        self.store.remove(activation_id)
        with self._lock:
            self._threads.pop(activation_id, None)

    def _resolve_refund(self, record, client, res_type, pname):
        """The activation should be closed now: check that the refund landed."""
        activation_id = str(record["activation_id"])
        number = record.get("number")
        settings = self.settings
        stats = getattr(self.owner, "stats", None)
        notify = getattr(self.owner, "notify", None)
        guard = getattr(self.owner, "guard", None)

        expected = None
        expected_getter = getattr(self.owner, "expected_balance", None)
        if callable(expected_getter):
            try:
                expected = expected_getter(record.get("provider"), activation_id)
            except Exception:
                expected = None
        if expected is None:
            expected = record.get("expected_balance")

        refund_delay = float((getattr(self.owner, "settings", {}) or {}).get(
            "refund_check_delay_seconds", 2) or 0)
        if refund_delay > 0:
            time.sleep(refund_delay)

        actual = None
        tally_ok = True
        if guard is not None and expected is not None:
            try:
                tally_ok, actual = guard.verify_refund(
                    client, expected,
                    activation_id=activation_id, number=number,
                    stop_event=getattr(self.owner, "stop_requested", None),
                    prefix=pname,
                )
            except Exception as exc:
                self._log(f"[DEFERRED-CANCEL] Refund check failed: {exc}", pname)
                tally_ok, actual = False, None

        if tally_ok:
            if stats is not None:
                try:
                    stats.increment("numbers_cancelled")
                    stats.increment("cancel_deferred_refunded")
                    stats.increment("refunds_verified")
                except Exception:
                    pass
            self._log(
                f"[DEFERRED-CANCEL] {number} cancelled after the expiry wait; "
                f"refund tallied (last cancel answer: {res_type}).",
                pname
            )
            if notify is not None:
                try:
                    notify.send(
                        f"✅ [{pname}] Deferred cancel completed",
                        f"Number: {number}\\nActivation: {activation_id}\\n\\n"
                        f"The provider had refused the cancel with ERROR; it was "
                        f"retried at expiry and the refund tallied."
                    )
                except Exception:
                    pass
            self._settle_ledger(record, client, pname, consumed=False)
            self._resolved(record)
            self.store.remove(activation_id)
            with self._lock:
                self._threads.pop(activation_id, None)
            return

        # Not refunded - a real discrepancy now, not a timing accident.
        if stats is not None:
            try:
                stats.increment("refunds_missing")
            except Exception:
                pass
        waited = ""
        try:
            waited = (f"\\nWaited: {max(0.0, time.time() - float(record.get('deferred_at_epoch', time.time()))):.0f}s "
                      f"for the activation to expire")
        except Exception:
            pass
        critical = getattr(self.owner, "critical_stop", None)
        message = (
            f"Number: {number}\\nActivation: {activation_id}\\n"
            f"Expected balance: ~{expected}\\nActual balance: {actual}\\n"
            f"Last cancel answer: {res_type}\\n"
            f"Reason for cancel: {record.get('reason')}{waited}\\n\\n"
            f"The cancellation was deferred to the activation expiry and retried, "
            f"and the refund still did not tally. Verify this activation in the "
            f"provider panel before buying more numbers."
        )
        if callable(critical):
            critical(f"[{pname}] REFUND DID NOT TALLY (deferred cancel)", message)
        elif notify is not None:
            try:
                notify.alert(f"🛑 [{pname}] REFUND DID NOT TALLY", message)
            except Exception:
                pass
        # Keep the record: the next run retries instead of dropping the money.
        self.store.update(activation_id, last_failure_at=now(),
                          last_actual_balance=actual)
        with self._lock:
            self._threads.pop(activation_id, None)

    def _resolved(self, record):
        """Tell the coordinator a deferred activation is fully closed."""
        hook = getattr(self.owner, "on_pending_resolved", None)
        if callable(hook):
            try:
                hook(record.get("provider"))
            except Exception:
                pass

    def _settle_ledger(self, record, client, pname, consumed):
        """
        Re-baseline the provider ledger once a deferred activation is closed.

        The hold is gone (refunded or legitimately spent), so the live balance
        becomes the expected balance for the next number.
        """
        note = getattr(self.owner, "note_balance", None)
        if not callable(note):
            return
        try:
            balance = client.get_balance()
        except Exception as exc:
            self._log(f"[DEFERRED-CANCEL] Balance fetch failed: {exc}", pname)
            return
        try:
            note(record.get("provider"), balance)
        except Exception:
            pass
