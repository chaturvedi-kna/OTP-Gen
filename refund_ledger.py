"""
SQLite-backed refund ledger: the fix for the parallel refund-tally race.

Why the old in-memory ledger drifted
------------------------------------
The coordinator tracked one number per provider - the "expected balance" the
provider should be back at once nothing is held - and REFRESHED it with raw
balance reads from several threads (the provider worker, the NO_BALANCE wait,
the deferred-cancel watcher settling a refund):

    expected_balance := <live balance>

That is only correct while nothing is held. With fast, parallel number buying
(deferred cancels holding money while the worker already buys the next
number - the routine OTPIndia/OTPSell flow), a raw balance read UNDERSTATES
the true at-rest balance by exactly the amounts still held. Storing it as the
new baseline and then deducting the very same holds again in
`_expected_balance()` applied every outstanding hold TWICE:

    target = (at_rest - held) - held        <- understated by `held`

A provider refund that never arrived then still passed the tally (the bar was
lowered by the missing money itself) - money silently disappeared, exactly
the "refund tally" pain seen on OTPIndia under parallel load.

The correct, race-free rule
---------------------------
A balance observation B taken while amounts H are still held by open
cancellations means the at-rest balance is B + H. So the ledger stores TWO
values, written in ONE SQLite transaction together with the observation:

    baseline        = B     (the raw provider balance that was read)
    baseline_holds  = H     (the holds that were open when it was read)

and every tally derives:

    expected(X) = baseline + baseline_holds - holds_open_now(excluding X)

The holds are never double-counted: they are added back exactly once (when
reconstructing the at-rest balance) and deducted exactly once (for the tally
at hand). The pair (baseline, baseline_holds) is always written atomically,
so a watcher settling a refund can no longer interleave with the worker's own
balance note. SQLite (WAL, single guarded connection) makes that ordering
total per provider, and keeps the baseline on disk so a restart no longer
loses it mid-queue (previously the first tally after a restart re-seeded from
a raw balance while old deferred cancels still held money - same double-count
bug, one reboot later).

Everything here is fail-safe: the ledger never raises into the automation.
If the DB is unavailable the callers fall back to their previous behaviour
(expected None -> the deferred record's own expected_balance is used).
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

from runtime import namespaced_name

DEFAULT_LEDGER_FILE = "refund_ledger.db"

# Observations kept per provider for the audit trail (older rows are pruned).
OBSERVATION_KEEP = 500


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def ledger_filename(instance=None):
    """refund_ledger.db -> refund_ledger.<instance>.db (runtime namespacing)."""
    return namespaced_name(DEFAULT_LEDGER_FILE, instance)


class RefundLedger:
    """
    Durable per-provider balance ledger.

    Tables:
      provider_ledger(provider, baseline, baseline_holds, holds_unknown, seq, updated_at)
        - one row per provider; (baseline, baseline_holds) pairs are only ever
          replaced inside a single transaction.
      balance_observations(provider, balance, holds, holds_unknown, source, taken_at, seq)
        - append-only audit trail of every balance the automation acted on.
      activations(provider, activation_id, number, operator, state, hold, hold_unknown,
                  reason, acquired_at, updated_at)
        - the large fast-bought set of numbers and what became of each
          (OPEN -> DEFERRED -> REFUNDED / CONSUMED / ...), purely for
          inspection/debugging; holds themselves stay authoritative in the
          pending-cancel store and are only mirrored here.
    """

    def __init__(self, filename=DEFAULT_LEDGER_FILE, instance=None, log_fn=None):
        if instance and filename == DEFAULT_LEDGER_FILE:
            filename = ledger_filename(instance)
        self.path = filename
        self._log_fn = log_fn
        self._lock = threading.RLock()
        self._db = None
        try:
            self._db = sqlite3.connect(
                filename, timeout=10.0, check_same_thread=False
            )
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._init_schema()
        except Exception as exc:
            self._log(f"refund ledger unavailable ({exc}); running without it")
            self._close_db()

    # -- internals ------------------------------------------------------------

    def _log(self, message):
        try:
            if self._log_fn:
                self._log_fn(message)
        except Exception:
            pass

    def _close_db(self):
        try:
            if self._db is not None:
                self._db.close()
        except Exception:
            pass
        self._db = None

    def _init_schema(self):
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_ledger (
                    provider        TEXT PRIMARY KEY,
                    baseline        REAL NOT NULL,
                    baseline_holds  REAL NOT NULL DEFAULT 0.0,
                    holds_unknown   INTEGER NOT NULL DEFAULT 0,
                    seq             INTEGER NOT NULL DEFAULT 0,
                    updated_at      TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS balance_observations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider        TEXT NOT NULL,
                    balance         REAL NOT NULL,
                    holds           REAL NOT NULL DEFAULT 0.0,
                    holds_unknown   INTEGER NOT NULL DEFAULT 0,
                    source          TEXT NOT NULL DEFAULT '',
                    taken_at        TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_obs_provider
                    ON balance_observations(provider, id);
                CREATE TABLE IF NOT EXISTS activations (
                    provider        TEXT NOT NULL,
                    activation_id   TEXT NOT NULL,
                    number          TEXT,
                    operator        TEXT,
                    state           TEXT NOT NULL DEFAULT 'OPEN',
                    hold            REAL,
                    hold_unknown    INTEGER NOT NULL DEFAULT 0,
                    reason          TEXT,
                    acquired_at     TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    PRIMARY KEY (provider, activation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_act_provider_state
                    ON activations(provider, state);
                """
            )
            self._db.commit()

    def close(self):
        with self._lock:
            self._close_db()

    # -- balance baseline ------------------------------------------------------

    def note_balance(self, provider, balance, source="", holds=0.0,
                     holds_unknown=False):
        """
        Record a balance observation AND the holds that were open at that
        moment, atomically. Returns the derived at-rest baseline
        (balance + holds), or None when the DB is unavailable.
        """
        if self._db is None or balance is None:
            return None
        try:
            holds_val = float(holds or 0.0)
            at_rest = float(balance) + holds_val
            with self._lock:
                with self._db:
                    self._db.execute(
                        """
                        INSERT INTO provider_ledger
                            (provider, baseline, baseline_holds, holds_unknown, seq, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?)
                        ON CONFLICT(provider) DO UPDATE SET
                            baseline = excluded.baseline,
                            baseline_holds = excluded.baseline_holds,
                            holds_unknown = excluded.holds_unknown,
                            seq = provider_ledger.seq + 1,
                            updated_at = excluded.updated_at
                        """,
                        (str(provider), float(balance), holds_val,
                         1 if holds_unknown else 0, _utcnow()),
                    )
                    self._db.execute(
                        """
                        INSERT INTO balance_observations
                            (provider, balance, holds, holds_unknown, source, taken_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (str(provider), float(balance), holds_val,
                         1 if holds_unknown else 0, str(source or ""), _utcnow()),
                    )
                    # Keep the audit table bounded.
                    self._db.execute(
                        """
                        DELETE FROM balance_observations
                        WHERE provider = ? AND id NOT IN (
                            SELECT id FROM balance_observations
                            WHERE provider = ? ORDER BY id DESC LIMIT ?
                        )
                        """,
                        (str(provider), str(provider), OBSERVATION_KEEP),
                    )
            return at_rest
        except Exception as exc:
            self._log(f"refund ledger note_balance failed: {exc}")
            return None

    def baseline(self, provider):
        """
        The stored observation pair for a provider:
        {"baseline", "holds", "holds_unknown", "seq", "updated_at"} or None.
        """
        if self._db is None:
            return None
        try:
            with self._lock:
                cur = self._db.execute(
                    "SELECT baseline, baseline_holds, holds_unknown, seq, updated_at"
                    " FROM provider_ledger WHERE provider = ?",
                    (str(provider),),
                )
                row = cur.fetchone()
            if row is None:
                return None
            return {
                "baseline": float(row[0]),
                "holds": float(row[1]),
                "holds_unknown": bool(row[2]),
                "seq": int(row[3]),
                "updated_at": row[4],
            }
        except Exception as exc:
            self._log(f"refund ledger baseline failed: {exc}")
            return None

    def expected(self, provider, holds_excluded=0.0):
        """
        What the provider balance must return to once the activation being
        tallied is refunded: at-rest baseline minus the OTHER holds still
        open. None when no baseline has been recorded yet.
        """
        row = self.baseline(provider)
        if row is None:
            return None
        try:
            return row["baseline"] + row["holds"] - float(holds_excluded or 0.0)
        except (TypeError, ValueError):
            return None

    def recent_observations(self, provider, limit=20):
        """Newest-first audit trail rows (for post-mortem debugging)."""
        if self._db is None:
            return []
        try:
            with self._lock:
                cur = self._db.execute(
                    "SELECT balance, holds, holds_unknown, source, taken_at"
                    " FROM balance_observations WHERE provider = ?"
                    " ORDER BY id DESC LIMIT ?",
                    (str(provider), int(limit)),
                )
                return [
                    {"balance": r[0], "holds": r[1], "holds_unknown": bool(r[2]),
                     "source": r[3], "taken_at": r[4]}
                    for r in cur.fetchall()
                ]
        except Exception as exc:
            self._log(f"refund ledger observations failed: {exc}")
            return []

    # -- activation audit trail -------------------------------------------------

    def record_purchase(self, provider, activation_id, number=None, operator=None):
        """A number was bought: open an audit row (idempotent)."""
        self._upsert_activation(
            provider, activation_id,
            number=number, operator=operator, state="OPEN",
            acquired_at=_utcnow(),
        )

    def mark_deferred(self, provider, activation_id, hold, hold_unknown=False,
                      reason=""):
        """The cancel was refused; the activation (and its money) is pending."""
        self._upsert_activation(
            provider, activation_id,
            state="DEFERRED", hold=hold, hold_unknown=hold_unknown,
            reason=reason,
        )

    def mark_resolved(self, provider, activation_id, outcome="REFUNDED"):
        """The activation is fully closed (refund tallied / legitimately spent)."""
        self._upsert_activation(provider, activation_id, state=outcome)

    def _upsert_activation(self, provider, activation_id, number=None,
                           operator=None, state=None, hold=None,
                           hold_unknown=False, reason=None, acquired_at=None):
        if self._db is None:
            return
        try:
            with self._lock:
                with self._db:
                    self._db.execute(
                        """
                        INSERT INTO activations
                            (provider, activation_id, number, operator, state,
                             hold, hold_unknown, reason, acquired_at, updated_at)
                        VALUES (?, ?, ?, ?, COALESCE(?, 'OPEN'), ?, ?, ?,
                                COALESCE(?, ?), ?)
                        ON CONFLICT(provider, activation_id) DO UPDATE SET
                            number = COALESCE(excluded.number, activations.number),
                            operator = COALESCE(excluded.operator, activations.operator),
                            state = COALESCE(excluded.state, activations.state),
                            hold = COALESCE(excluded.hold, activations.hold),
                            hold_unknown = excluded.hold_unknown,
                            reason = COALESCE(excluded.reason, activations.reason),
                            updated_at = excluded.updated_at
                        """,
                        (
                            str(provider), str(activation_id),
                            None if number is None else str(number),
                            None if operator is None else str(operator),
                            state,
                            None if hold is None else float(hold),
                            1 if hold_unknown else 0,
                            None if reason in (None, "") else str(reason)[:300],
                            acquired_at, _utcnow(), _utcnow(),
                        ),
                    )
        except Exception as exc:
            self._log(f"refund ledger activation write failed: {exc}")

    def open_activations(self, provider=None, states=("OPEN", "DEFERRED")):
        """Audit listing of activations that are not fully closed."""
        if self._db is None:
            return []
        try:
            placeholders = ",".join("?" for _ in states)
            query = ("SELECT provider, activation_id, number, operator, state, hold,"
                     " hold_unknown, reason, acquired_at, updated_at FROM activations"
                     f" WHERE state IN ({placeholders})")
            params = list(states)
            if provider:
                query += " AND provider = ?"
                params.append(str(provider))
            query += " ORDER BY updated_at"
            with self._lock:
                cur = self._db.execute(query, params)
                cols = ["provider", "activation_id", "number", "operator", "state",
                        "hold", "hold_unknown", "reason", "acquired_at", "updated_at"]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as exc:
            self._log(f"refund ledger open_activations failed: {exc}")
            return []

    # -- maintenance -------------------------------------------------------------

    def reset(self, provider=None):
        """Drop ledger rows (all providers, or one). For tests/maintenance."""
        if self._db is None:
            return
        try:
            with self._lock:
                with self._db:
                    for table in ("provider_ledger", "balance_observations", "activations"):
                        if provider:
                            self._db.execute(f"DELETE FROM {table} WHERE provider = ?",
                                             (str(provider),))
                        else:
                            self._db.execute(f"DELETE FROM {table}")
        except Exception as exc:
            self._log(f"refund ledger reset failed: {exc}")

    def db_size_bytes(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0
