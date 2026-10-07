# OTPSell provider (otpsell.com) + the SQLite refund ledger

This adds **OTPSell** as a sixth OTP provider and reworks the refund tally so
fast, parallel number buying (many numbers in flight while earlier cancels
still hold money) can no longer drift the expected balances - the problem
previously seen on OTPIndia.

## Configuration

```json
"otpsell": {
  "enabled": true,
  "base_url": "https://otpsell.com/stubs/handler_api.php",
  "api_key": "<your otpsell api key>",
  "service": "wa",
  "country": "91",
  "operator": "any",
  "max_price": null,
  "cancel_wait_seconds": { "default": 120, "any": 120, "1": 120, "2": 60 },
  "min_request_interval_seconds": 0.25,
  "max_attempts": 1000
}
```

Then run it like any provider: `python main.py --provider otpsell`,
`"active_otp_provider": "otpsell"`, or Telegram `/run otpsell`
(alias: `sell`).

### API surface used

| Action        | Notes                                                        |
| ------------- | ------------------------------------------------------------ |
| `getBalance`  | `ACCESS_BALANCE:<amount>`                                    |
| `getNumber`   | `service` + `country` + `operator`; `maxPrice` when set (mandatory for operators 6 & 9 - the client refuses those operators without one instead of burning bad requests) |
| `getStatus`   | `STATUS_WAIT_CODE` / `STATUS_OK:<sms>` / `STATUS_CANCEL`     |
| `setStatus`   | `3` = request another SMS (`ACCESS_RETRY_GET`), `8` = cancel (`ACCESS_CANCEL`); `6` (finish) is undocumented, attempted best-effort only |
| `getOperators` / `getCountries` / `getServices` | catalogs, for panel lookups  |

## Per-operator cancel windows

OTPSell operators hold the number's money for a window after issue before a
cancel is honoured - and the window length depends on the operator. Set it
per operator:

* a number: `"cancel_wait_seconds": 120` - same window for every operator;
* a map: `"cancel_wait_seconds": {"default": 120, "1": 120, "2": 60, "any": 90}`
  - the window of the operator that **served** the number applies, `"default"`
  is the fallback (and `"any"` is honoured when the provider picks).

A cancel inside the window is refused **client-side** with the same
`ACCESS_CANCEL_WAIT` contract the coordinator knows from OTPIndia: the
cancellation is deferred, the worker keeps hunting the next number
immediately, the background watcher retries the moment the window passes, and
only then is the refund tally run. The OTP wait is likewise stretched to the
end of the window (`automation.otp_wait_covers_cancel_window`, on by
default), so a number is never thrown away while its SMS could still be used.

### Operator rotation pools (more numbers, faster)

`"operators": ["1", "2"]` makes `getNumber` rotate through the listed
operators round-robin, so fast iterations pull from every operator's number
inventory instead of draining one pool to `NO_NUMBERS`. Each activation keeps
the window configured for the operator that served it. Leave it unset to use
the single `"operator"` value (`"any"` = provider picks).

### Rate-limit friendliness

`min_request_interval_seconds` (default `0.25`) paces requests client-side so
parallel workers do not trip `TOO_MANY_REQUESTS`. Set `0` to disable.

## The refund tally race, fixed (SQLite)

The old in-memory ledger refreshed the per-provider "expected balance" with
raw balance reads: `expected := live balance`. With parallel buying that read
happens while earlier deferred cancels still hold money - so the stored
expectation came out low by exactly the held amounts, every later tally
deducted the same holds a second time, and a refund that never arrived could
**silently pass** the tally (the bar had been lowered by the missing money
itself). That is the OTPIndia "refund tally" pain.

`refund_ledger.py` now stores each observation as an atomic SQLite pair:

```
baseline = live balance read      baseline_holds = holds open at that moment
at-rest  = baseline + baseline_holds       expected(X) = at-rest - others' holds
```

`refund_ledger[.<instance>].db` is written in one transaction per observation
(WAL; worker + watcher threads serialize on it), holds are single-counted by
construction, and the pairs survive restarts mid-queue (previously the first
tally after a crash re-seeded from a raw balance while old cancels still
held - the same double-count, one reboot later). The watcher also settles a
refund only **after** its pending record is closed, so the settled balance is
never paired with its own hold.

The DB additionally keeps an audit trail (`balance_observations`,
`activations`) of the fast-bought number set - every purchase, its hold, and
how it ended (`REFUNDED` / `CONSUMED` / `DEFERRED`) - for post-mortems when a
provider disputes a charge. `/balance` shows money currently held by pending
cancels and the at-rest balance per provider.

Behaviour on a missing refund is unchanged where it matters: it still
critical-stops and alerts (except now it actually detects it under parallel
load instead of waving it through).

### Live-run hardening (why a tally can never race the buying again)

A real run once stopped on a false `REFUND DID NOT TALLY`: the watcher's
expectation was frozen when a deferred refund started polling, ~6 numbers
were bought during the 60 s poll, and the live balance could never reach the
frozen number. The fix set on top of the SQLite ledger:

* **In-flight holds** — every purchase is priced the moment it happens
  (`balance before getNumber − balance right after`) and recorded as an OPEN
  row in the ledger. Any refund tally taken while that number is still being
  checked / driven through the bot counts its money as still out. When the
  number is consumed (code submitted, account created) or deferred, the hold
  moves to its end state and stops counting. Disable with
  `automation.price_check_on_buy: false`.
* **Expectations are re-derived every poll** — `verify_refund` accepts a
  getter instead of a frozen number, so numbers bought while a refund is
  being tallied lower the expectation by exactly their price. Only a
  genuinely missing refund keeps failing.
* **Deferred refunds are patient** — once a refused cancel is finally
  accepted (the money already waited out the whole window), the tally waits
  `automation.deferred_refund_wait_seconds` (default `240`) before it calls
  the mismatch, which providers that credit refunds asynchronously need.
* **A critical stop never strands a paid OTP wait** — a refund mismatch
  stops the *buying*, but the active target's OTP wait runs to the end of
  its window so a late code can still be used (before, it was aborted
  mid-wait and the provider charged the delivered SMS anyway). A user
  `/stop` still aborts immediately; restore the old behaviour with
  `automation.keep_otp_wait_on_critical_stop: false`.
* **Diagnostics in alerts** — a critical-stop alert now carries the ledger
  breakdown (baseline, pending-cancel holds, in-flight holds) so a real
  dispute has its numbers attached, and `/balance` shows both hold kinds. It
  also prints the *last re-derived* expectation (the one that actually
  failed), not the value frozen when the tally started.
* **Ghost-hold sweeps + last-chance confirmation** — numbers bought but
  never cleanly closed (aborted runs, rows left by older builds) get swept
  against the provider (`get_status`: `STATUS_OK` → CONSUMED,
  `STATUS_CANCEL`/`NO_ACTIVATION` → REFUNDED/EXPIRED) so their money stops
  appearing in tallies once it is back. Before any critical stop, the tally
  sweeps ghosts and takes a few fresh comparisons (a refund that landed
  right at the deadline passes here); and a tally that ended because the
  run was already stopping elsewhere never escalates a second time.

### Dispute evidence (for the provider team)

Whenever money does not come back the way it should, a self-contained
evidence record is appended to the per-instance dispute log
(`cancel_refused_otp.<instance>.jsonl` in the run directory) — one JSON
object per line:

* `refund_mismatch` - a cancelled number's refund never arrived. The record
  carries the order id, number, reason, expected vs. actual balance, the
  ledger row for that activation, the hold breakdown, and the last balance
  observations, so it stands alone when you raise it with OTPSell.
* `otp_after_cancel_refused` - the provider refused the cancel *and* the
  SMS arrived anyway (kept since the OTPIndia days).

Every critical-stop alert names the file; on Telegram `/disputes [n]`
shows the newest n records (default 5) with their ledger lines. Hand the
matching JSON line (plus a panel screenshot) to the provider - the numbers
inside are identical to what the bookkeeper used.

A is bought (balance 90) and sits in its cancel window. Meanwhile B, C, D are
bought (balance 60). When A's window ends and its cancel lands, the refund
tally expects **70**: A's +10 refund on top of the live 60, i.e. the at-rest
100 minus the 30 still held by B, C, D. Not 90 (that was the balance when A
was bought - stale) and not 100 (30 is still out). Afterwards the queue
cascades exactly: B tallies against 80, C against 90, D against 100 - and if
the three others were already refunded instead, A's own cancel expects the
full 100. (`test_cancel_expectation_with_parallel_buys` replays this step by
step.)

The money flow for reference:

```
seed            100   (nothing held)
buy A  (-10)     90   hold 10  [A waits in its cancel window]
buy B  (-10)     80   hold 10
buy C  (-10)     70   hold 10
buy D  (-10)     60   hold 10
refund A (+10)   70   <- tallied against 70  (100 - 30 still held)
refund B (+10)   80   <- against 80          settle notes (80, holds 20)
refund C (+10)   90   <- against 90          settle notes (90, holds 10)
refund D (+10)  100   <- against 100         settle notes (100, holds 0)
```

## Tests

```
python test_otpsell_provider.py
```

covers the protocol, per-operator windows (incl. client-side refusal = zero
provider calls, rotation pools, provider-side `ACCESS_CANCEL_WAIT:<s>`),
factory/selection wiring, the coordinator (OTP-wait stretch, quiet deferred
cancel + watcher retry + refund tally), and the ledger (expected-balance math
under churn, restart persistence, thread-safety smoke, and the missing-refund
regression).
