# Notification setup (Termux + Telegram) & Multi-Provider OTP

## 1. Why Android notifications were not arriving

`notifier.py` looked for `termux-notification` and, if anything went wrong,
swallowed the error with a bare `except: pass`. So a missing package, a missing
Termux:API app, or a denied Android permission all looked identical: silence.

The rewritten notifier reports the exact reason instead, and adds Telegram as a
second, independent channel.

## 2. Fix Termux notifications

Two separate things are required — installing only one is the usual cause of failure:

```bash
pkg update
pkg install termux-api          # the command-line tools
```

Then install the **Termux:API app** from F-Droid (or the GitHub releases page).
It must come from the same source as your Termux app, or Android will refuse the
signature match and the commands will hang.

On Android 13 and newer, also grant the notification permission:

> Settings → Apps → Termux → Notifications → Allow

And disable battery optimisation for both **Termux** and **Termux:API**, or
Android will kill the API bridge in the background.

Verify:

```bash
python notifier.py
```

This checks each step in order and tells you which one failed.

## 3. Set up the Telegram bot

1. Open Telegram, search for **@BotFather**, send `/newbot`, follow the prompts.
2. Copy the token it gives you into `config.json`:

```json
"telegram": {
  "enabled": true,
  "bot_token": "PASTE_TOKEN_HERE",
  "chat_id": ""
}
```

3. Open your new bot in Telegram and press **Start** (or send any message).
4. Get your chat id:

```bash
python notifier.py --chat-id
```

5. Paste the printed number into `config.json` under `telegram.chat_id`.
6. Confirm both channels work:

```bash
python notifier.py
```

## 4. Telegram Interactive Buttons & Commands

### Interactive Notification Buttons
When an unregistered number is found:
- **`[📋 Copy <number>]` button**: Uses Telegram native `copy_text` button so tapping it puts the number straight onto your clipboard. The message also renders the phone number in monospace `` `914738485900` `` for single-tap copying on mobile.
- **`[✅ OTP Triggered]` button**: Tapping this gives **instant on-screen toast feedback** ("✅ OTP Triggered! Waiting for SMS...") and dynamically updates the inline button to `[ ✅ OTP Triggered (Confirmed) ]` so you know it was accepted.
- **`[⏭ Skip Number]` button**: Cancels the current number for a refund and updates the button to `[ ⏭ Number Skipped ]`.

### Bot Commands
You can interact with the running tool via Telegram anytime:
- **`/status`**: Checks whether the tool is RUNNING or IDLE, global attempts, active target number, and provider balances.
- **`/run`**: Starts searching for fresh numbers from Telegram if the script was stopped or idle (uses `active_otp_provider` from `config.json`).
- **`/run <provider>`**: Starts **only that provider** for this run — e.g. `/run vsimpro`, `/run otpindia`, `/run tempora,vsimpro` or `/run all`. Unknown names and providers without credentials are rejected with an explanation. The choice applies to this run only; the next bare `/run` returns to `active_otp_provider`. (`/status` shows the current run selection while it differs from Mode.)
- **`/balance`**: Retrieves live balances for every configured provider (TemporaSMS, VSImpro, OtpDoctor, OTPCart, OTPIndia).
- **`/stop`**: Gracefully stops the active search.
- **`/referral <link>`**: Saves/updates the Meesho referral link (`/referral off` clears it, `/referral` shows it).
- **`/checker api|bot|auto`**: Shows or switches the number-checker strategy (API only / PRIMES bot only / API first with the bot as fallback). See `SETUP_CHECKER.md`.
- **`/accounts`** (alias `/linked`): Linked-account counts for this Telegram instance — total, per provider (TEMPORA / VSIMPRO / OTPINDIA …), and, once a milestone is set, how many were linked *after* it with the first and the latest number. `/accounts list` prints every number linked since the last milestone.
- **`/milestone <last number shared> <note>`**: Marks a cut in the linked-account ledger — everything up to and including that number counts as "before the milestone", everything linked later is "new". Example: `/milestone 9876543210 10 used + 40 shared`. `/milestone` shows the milestones, `/milestone remove` drops the last one.
- **`/notify all|normal|quiet`**: Shows or changes how much reaches Telegram (see *Notification levels* below). Saved to `config.json`, so it survives restarts.
- **`/start`**: Shows available bot commands.

### Notification levels (`/notify`)

Every message is still written to the console log; the level only decides what
is pushed to Telegram. Default is **all**.

| Level | Delivered to Telegram |
|---|---|
| `all` | everything |
| `normal` | 🎉 Account linked, ❌ Wrong OTP / ⌛ expired / 🚫 blocked / unconfirmed, ⏱ OTP timed out, remote cancels, and everything critical (stops, balance, refund tally, bot needs attention) |
| `quiet` | 🎉 Account linked + critical only |

`📲 OTP requested`, `🔄 Changing number`, `⏳ Cancel refused - deferred` /
`✅ Deferred cancel completed`, `🆘 Late OTP salvaged` and the manual-trigger
progress notes are the *routine* tier: they are dropped at `normal`/`quiet` and,
at `all`, arrive **without sound** (`telegram.silent_routine`, default `true`;
set it to `false` to get the sound back). `🎉 Account linked` is always sent
immediately, per account, with sound.

### Linked-account ledger & milestones

Each linked account is appended to `accounts.json` (`accounts.<instance>.json`
in a `--instance` tab, next to `stats.<instance>.json`) with its number,
provider and time — that is what `/accounts` and `/milestone` read. Accounts
linked before this ledger existed are only in the `accounts_linked` counter, so
`/accounts` shows them as "before per-provider tracking".

### Adding an OTP provider (example: OTPIndia)

Every provider lives in its own `config.json` block and is picked up by
`--provider` / `active_otp_provider` / `/run <provider>`. For OTPIndia
(`otpindia.org`, handler_api protocol):

```json
"otpindia": {
  "enabled": true,
  "base_url": "https://otpindia.org/api/stubs/handler_api.php",
  "api_key": "YOUR_API_KEY",
  "service": "meesho",
  "server": "Operator-1",
  "cancel_wait_seconds": 120,
  "max_attempts": 500
}
```

- `api_key` — from your OTPIndia account (the provider is skipped everywhere,
  including `/run otpindia`, until this is set).
- `service` — the service display code (e.g. `meesho`, `wa`).
- `server` — a server code listed for that service on otpindia.org. For
  Meesho the listed codes are `Operator-1` … `Operator-4`, `Operator-9`,
  `Operator-10` and `v1-22` (sent as `server=` with `getNumber`).
- `cancel_wait_seconds` — OTPIndia's cancel window: a cancel sent earlier than
  this after `getNumber` is answered with `ACCESS_CANCEL_WAIT` (the number
  stays open, the money stays held) and is retried in the background once the
  window has passed. Keep it at the provider's 2 minutes unless they change it.
- Rate limit is 900 requests/minute — the defaults are far below it.
- CLI: `python main.py --provider otpindia` (also accepts the alias `india`).

How the cancel window shapes an OTPIndia run:

- **OTP wait**: a found number waits for its OTP for
  `automation.otp_timeout_seconds` like on every provider — but never shorter
  than the rest of its cancel window. Giving up at, say, 80s cannot refund the
  number before 120s anyway; an SMS arriving at 119s would then be paid for and
  unused. So when the OTP was triggered soon after the number was bought, the
  wait runs to `cancel_wait_seconds` from `getNumber` (an SMS in that time is
  used normally); when the trigger came late enough that the configured timeout
  already ends after the window, the configured timeout stands and the cancel
  is accepted immediately after it. `automation.otp_wait_covers_cancel_window:
  false` disables the stretch.
- **Out of balance while cancels are pending**: `NO_BALANCE` with routine
  cancels still inside their window is back-pressure, not a stop. The worker
  waits for the **first** pending cancel to refund, then requests the next
  number right away — it does not wait for all of them. If that refund was not
  enough, it waits for the next one, and so on; only `NO_BALANCE` with nothing
  pending stops the worker with the usual recharge alert.

### OTPSell (`otpsell.com`, handler_api protocol)

OTPSell speaks the same SMS-Activate `handler_api` protocol as TemporaSMS /
VSImpro but takes a **country** (and a specific **operator**) on `getNumber`
instead of a Tempora-style operator routing string. It has two OTPSell-specific
quirks (both handled by the client, but they shape the config):

```json
"otpsell": {
  "enabled": true,
  "base_url": "https://otpsell.com/stubs/handler_api.php",
  "api_key": "YOUR_API_KEY",
  "service": "meesho",
  "country": "91",
  "operator": "server-62",
  "max_price": 9,
  "cancel_wait_seconds": 120,
  "timeout": 30,
  "max_attempts": 500
}
```

- `service` — the service id listed on otpsell.com. Several codes map to the
  same app and the right one can be **operator-specific** (e.g. `meesho` works
  with `server-62`, while `hp` — which also reads "Meesho" in `getServices` —
  is rejected there with `BAD_SERVICE`). Use the code that works for your chosen
  operator; check `getServices`.
- `country` — the country id (e.g. `91` = India, from `getCountries`).
- `operator` — **must be a specific operator id** (the *value* in
  `getOperators`, e.g. `server-62`, not the display key `SERVER-62`). Do **not**
  leave it empty or use `any`: otpsell then assigns an operator you cannot see,
  and the order can never be cancelled/refunded (`setStatus` needs the exact
  operator and answers `BAD_STATUS` otherwise). `max_price` is mandatory for
  operators `6` and `9`.
- `max_price` — optional price cap forwarded as `maxPrice`.
- `price` — optional, the exact amount ONE number costs (e.g. `9`). The refund
  tally books every number as holding this much money until it is cancelled or
  consumed, so a cancel taken while other numbers are still open is not mistaken
  for a missing refund. Without it the price is measured from the balance drop
  at purchase time, which can fail (a balance read that errors, or the provider
  refunding an expired number at the same instant); the price is then taken from
  the last one measured, and if there is none the refund tally is suspended for
  this provider until it resolves. Setting `price` removes that guessing
  entirely — worth doing for any provider that charges a fixed rate. The same
  key works for `otpindia`.
- **Cancel window (~2 minutes, like OTPIndia)**: a cancel (`setStatus` status
  `8`) sent sooner than `cancel_wait_seconds` after `getNumber` is refused with
  `BAD_STATUS`; the client surfaces that as `ACCESS_CANCEL_WAIT` so the
  coordinator defers the cancel, keeps waiting for the OTP until the number can
  actually be refunded, and retries the cancel once the window passes. So a
  found number waits up to `cancel_wait_seconds` for its OTP rather than being
  abandoned early (same as OTPIndia — see the OTP wait / out-of-balance notes
  above). Set `cancel_wait_seconds: 0` only if the provider ever cancels
  immediately. Requesting another SMS (status `3`) returns `ACCESS_RETRY_GET`.
- Catalog helpers `getOperators` / `getCountries` / `getServices` return JSON
  maps (used for discovery; not required to run).
- CLI: `python main.py --provider otpsell` (also accepts the alias `sell`).

## 5. Dual OTP Providers (TemporaSMS + OtpDoctor)

The system supports running **both providers in parallel on independent worker threads**:
- OtpDoctor wait cooldowns (e.g. `WAIT_CANCEL:120`) run strictly on OtpDoctor's thread.
- TemporaSMS requests, validates, and cancels immediately on its thread without waiting.
- When either worker finds an unregistered number, both pause while you enter the number and verify OTP.

### CLI Commands
- Check balances:
  ```bash
  python main.py --balance
  ```
- Run diagnostic tests:
  ```bash
  python otp_client.py
  ```
- Run with specific provider:
  ```bash
  python main.py --provider tempora
  python main.py --provider otpdoctor
  python main.py --provider both
  ```

## 6. Configuration Reference (`config.json`)

```json
{
  "active_otp_provider": "both",

  "tempora": {
    "enabled": true,
    "base_url": "https://api.temporasms.com/stubs/handler_api.php",
    "api_key": "77406bf7b2e5becec96437e80f4c30684509",
    "country": "22",
    "service": "meesho",
    "operator": "auto",
    "max_price": null,
    "operator_services": {}
  },

  "otp": {
    "enabled": true,
    "base_url": "https://otpdoctor.in/stubs/handler_api.php",
    "api_key": "cnis63cpc16umpleul2faa0iy5cwa0js",
    "country": "in",
    "service": "12843",
    "max_price": 9.5
  },

  "telegram": {
    "enabled": true,
    "bot_token": "...",
    "chat_id": "...",
    "notify_level": "all",
    "silent_routine": true
  },

  "automation": {
    "target_registered": false,
    "max_attempts": 200,
    "require_manual_trigger": true,
    "trigger_wait_seconds": 300
  }
}
```

`telegram.notify_level` (`all` / `normal` / `quiet`) is what `/notify` writes;
`telegram.silent_routine` mutes the routine tier (OTP requested, changing
number, deferred cancels) at level `all`.

---

## Deferred cancellations (provider refuses the cancel)

TemporaSMS and VSImpro answer a cancel that arrives while the activation is
still young with `{"type": "ERROR"}`. That number is NO LONGER critical-stopped
and forgotten; it is handed to a background watcher (see `cancel_watch.py`):

* the worker keeps hunting immediately (no waiting for the activation to
  expire),
* the amount still held is booked so later refund tallies stay correct,
* an OTP that lands while waiting is reported with its code (the SMS was
  delivered, so the charge legitimately stands - no cancel is retried),
* at expiry (assumed 15 minutes from the refuse, per your instruction) the
  cancel is retried and the refund tallied - only THEN is an imbalance a real
  `REFUND DID NOT TALLY` critical stop.

`/status` shows a section for deferred cancellations while any are open.

**Complaint evidence (`cancel_refused_otp.jsonl`).** When a cancel was refused
with `ERROR` and an OTP STILL arrives afterwards - right away or while the
watcher is waiting - the event is both notified AND persisted, one full
record per activation, so a complaint can be raised with the provider later:

```json
{"recorded_at": "...", "provider": "tempora", "activation_id": "123456",
 "number": "9876543210", "reason": "otp_timeout", "otp_code": "482913",
 "otp_sms": "482913 is your code", "otp_received_at": "...",
 "expected_balance": 100.0, "source": "deferred_cancel_watch"}
```

The file is append-only JSONL (one record per line), kept forever, and - like
every other runtime file - namespaced per instance
(`cancel_refused_otp.tempora.jsonl`, `cancel_refused_otp.vsimpro.jsonl`).

---

## Parallel runs (two Termux tabs: one for TemporaSMS, one for VSImpro)

By default, running `python main.py` in two tabs in the same directory would
have BOTH tabs write to the same `stats.json`, `state.json`, `pending_cancels.json`
and `.signals/`, so the runs would merge or lose each other's updates.

Each copy now gets its own **instance** name. Because you run exactly one
provider per tab, it is derived automatically:

    python main.py --provider tempora     # stats.tempora.json, .signals-tempora, ...
    python main.py --provider vsimpro    # stats.vsimpro.json, .signals-vsimpro, ...

To override that (or give any tab a standalone name):

    python main.py --instance tab1 --provider all

Optionally, a per-instance block in `config.json` under `"instances"` is merged
over the top-level config for that instance only (useful if the two tabs
should use different userbot sessions or a different command bot):

    "instances": {
      "tempora": { "meesho_bot": { "session_file": "userbot.tempora.session.txt" } },
      "vsimpro": { "meesho_bot": { "session_file": "userbot.vsimpro.session.txt" } }
    }

**Two different Telegram accounts (one per tab).** It is ONE `config.json` -
there is no conflict: only the tab's own `instances.<name>` block is merged
over the top-level config, so tempora keeps using its session and vsimpro
its own. Put each account's credentials in its block:

    "instances": {
      "tempora": {
        "meesho_bot": {
          "api_id": 1111111,
          "api_hash": "aaaa...",
          "session_file": "userbot.tempora.session.txt"
        }
      },
      "vsimpro": {
        "meesho_bot": {
          "api_id": 2222222,
          "api_hash": "bbbb...",
          "session_file": "userbot.vsimpro.session.txt"
        }
      }
    }

Then log in each account ONCE (each writes its own session file):

    python login_userbot.py --instance tempora
    python login_userbot.py --instance vsimpro

(`--instance` applies that instance's overrides first, so the session lands in
`userbot.<instance>.session.txt`; `--session-file <path>` overrides the output
path entirely. Skip the flag and the top-level `meesho_bot` defaults are used.)

Do NOT drive the PRIMES/checker bots from BOTH tabs with the SAME Telegram
account: the bot's chat history is shared server-side, so tab B's screen taps
and typed numbers would interleave with tab A's login/OTP screens (the
bot-claim lock only guards threads inside one process, not two). Give each
tab its own account via the `instances` block above - that is exactly what it
is for. One account + one bot-driving tab at a time stays safe.

### Adding a third parallel tab (e.g. OTPIndia)

The PRIMES userbot is the per-tab resource: **one Telegram account drives one
process** — the bot's chat history with PRIMES is shared server-side, so tab
B's taps and typed numbers would interleave with tab A's login/OTP screens
(the claim lock only guards threads inside ONE process), and two processes
must not share one Telethon session file either. A third tab therefore needs
a third account — and its own command bot, because every process long-polls
`getUpdates` with its token (two processes on one bot token would randomly
swallow each other's `/run` / `/status` messages):

```json
"instances": {
  "tempora": {
    "meesho_bot": { "api_id": 1111111, "api_hash": "aaaa...",
                    "session_file": "userbot.tempora.session.txt" },
    "telegram":   { "bot_token": "<tab1 command bot token>", "chat_id": "..." }
  },
  "vsimpro": {
    "meesho_bot": { "api_id": 2222222, "api_hash": "bbbb...",
                    "session_file": "userbot.vsimpro.session.txt" },
    "telegram":   { "bot_token": "<tab2 command bot token>", "chat_id": "..." }
  },
  "otpindia": {
    "meesho_bot": { "api_id": 3333333, "api_hash": "cccc...",
                    "session_file": "userbot.otpindia.session.txt" },
    "telegram":   { "bot_token": "<tab3 command bot token>", "chat_id": "..." }
  }
}
```

```bash
python login_userbot.py --instance otpindia   # once, with the third account
python main.py --provider otpindia            # third tab
```

Each tab then owns its PRIMES conversation exclusively — login, checks and
prewarm never collide across tabs, and a FloodWait cooldown on one account
only self-heals that tab.

### No third PRIMES account? Two alternatives

**1. Bot-free third tab.** The PRIMES account is only needed for the
auto login/link flow and the bot-based number checker. Run the OTPIndia tab
without one:

```json
"instances": {
  "otpindia": {
    "meesho_bot": { "enabled": false },
    "checker": { "mode": "api" }
  }
}
```

Checks go through the checker API, the manual trigger flow delivers the OTP
through the Telegram buttons — zero PRIMES usage. (`"auto"` also degrades to
API-only when no bot is ready; `"api"` just makes it explicit.)

**2. One process, three providers.** `active_otp_provider` accepts a list:

```json
"active_otp_provider": "tempora,vsimpro,otpindia"
```

`python main.py` runs all three workers against ONE PRIMES account: the
in-process `_BotClaim` lock already guarantees only one flow (login / check /
prewarm) talks to the PRIMES chat at a time — the others wait a bounded time
or cancel-with-refund instead of interleaving. Trade-offs: one shared
stats/state file for all three providers, and the bot serializes whatever it
touches (the provider workers themselves still run fully parallel).

### Mixed layout: one provider in tab 1, two in tab 2

`--instance` (which PRIMES account / command bot / stats files a tab owns)
and `--provider` (which workers that tab runs) are independent:

```bash
# Tab 1: tempora only
python main.py --provider tempora

# Tab 2: vsimpro + otpindia sharing the vsimpro account and files
python main.py --instance vsimpro --provider vsimpro,otpindia
```

The explicit `--instance vsimpro` on tab 2 matters: a multi-provider
`--provider` alone derives no instance name (it would fall back to the
top-level userbot session / command bot instead of this tab's own).

Alternatively put the mix in the instance block and start the tab with only
`--instance`:

```json
"instances": {
  "vsimpro": { "active_otp_provider": "vsimpro,otpindia" }
}
```

```bash
python main.py --instance vsimpro
```

Switch a tab live from its own command chat — no restart needed:

| Command (tab 2's bot chat) | Runs |
|---|---|
| `/run vsimpro` | only vsimpro |
| `/run otpindia` | only otpindia |
| `/run vsimpro,otpindia` | both again |
| `/run` | whatever the tab started with |

otpindia rides on tab 2's existing PRIMES account: both workers are in ONE
process, so the in-process bot-claim lock serializes everything they do in
the bot chat — no third Telegram account is required. (Until `otpindia.api_key`
is set, tab 2 simply runs vsimpro only; `/run vsimpro,otpindia` replies with
the reason instead.)
