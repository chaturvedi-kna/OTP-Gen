# Checker setup: modes (API / PRIMES bot / auto), rate limits, retries & keys

Two ways to validate a number before it is spent:

* the **checker API** (superassets.in) — fast, parallel, rate limited per key;
* the **PRIMES bot's own number checker** — driven through the same Telethon
  userbot that runs the login flow (`SETUP_MEESHO_BOT.md`).

`config.json` → `"checker"` → `"mode"` picks how they are used.

## 1. Choosing the mode

| `checker.mode` | Behaviour |
| --- | --- |
| `"api"` (default in code) | API only — the original behaviour. Any API error cancels the number. |
| `"bot"` | **PRIMES bot checker only.** Needs `meesho_bot` enabled + a connected userbot. If the userbot is not ready the run **refuses to start** (see §4). |
| `"auto"` | **API first, PRIMES bot on API trouble.** The API is used while it works; when it is down (`is_down: true`), too slow (read/connect **timeout**), unreachable (network/5xx), or rejects every key, the same number is checked through the bot instead — so a paid activation is not thrown away over a temporary API problem. |

```json
"checker": {
  "mode": "auto",
  "base_url": "https://superassets.in",
  "api_keys": ["AK__...", "AK__second_key_if_you_have_one"],
  "service": "meesho",

  "fallback": {
    "is_down": true,
    "read_timeout": true,
    "network": true,
    "http_5xx": true,
    "auth": true,
    "rate_limit": false,
    "cooldown_seconds": 60,
    "max_cooldown_seconds": 600
  },

  "bot": {
    "entry": "auto",
    "command": "",
    "button_hints": [],
    "number_prompt_hints": [],
    "registered_hints": [],
    "not_registered_hints": [],
    "step_timeout_seconds": 30,
    "max_attempts": 2,
    "reset_after_check": true
  }
}
```

Aliases are accepted for convenience: `primes`, `primes_bot`, `bot_checker` →
`bot`; `fallback`, `hybrid`, `both` → `auto`; `api_checker`, `http` → `api`.

Switch mode at runtime without editing files:

```
/checker            # show the current strategy, cooldown and counters
/checker api        # API only
/checker bot        # PRIMES bot only
/checker auto       # API first, bot on API failures
```

```
python main.py --checker-mode auto      # save into config.json and exit
python main.py --checker-status         # print the configured strategy
```

The change is written into `config.json` and applied immediately.

## 2. `auto`: which API failures switch to the bot

`checker.fallback` (all default `true` except `rate_limit`):

| Key (aliases) | Trigger |
| --- | --- |
| `is_down` (`service_down`, `down`) | the API answered `{"is_down": true}` for the service |
| `read_timeout` (`timeout`, `timed_out`) | connect/read timeout — "takes more time to respond" |
| `network` (`network_error`, `connection`) | connection refused / DNS / TLS failures |
| `http_5xx` (`5xx`, `server_error`) | the API returned a 5xx |
| `auth` (`auth_error`, `bad_keys`) | every configured API key was rejected (401/403) |
| `rate_limit` (`429`) | the 429 retry budget was spent — **off** by default: a rate limit means the API is alive, it is only waiting for its turn |
| `unknown` (`other`) | `success=false` / unexpected payload — **off**: that is a definitive answer, not an outage |

Write them either nested (`"fallback": {"read_timeout": true}`), flat
(`"fallback_read_timeout": true`, `"fallback_cooldown": 30`) or as a single
switch (`"fallback": false` disables every trigger, `true` enables them all).

**Cooldown.** After a fallback the API is skipped for `cooldown_seconds`,
doubling on every further failure up to `max_cooldown_seconds` — a dead API is
not re-tried on every single number (each retry would block a rented
activation). The first healthy answer resets it and checking returns to
API-first. `cooldown_seconds: 0` keeps probing the API on every check.

**The budget matters.** In `auto` mode keep `max_retry_wait_seconds` modest
(e.g. `20`–`30`) so the bot fallback starts before the number rental window
matters; the API retry budget is spent *before* the bot is asked.

**If the bot is not available** (disabled/unconfigured/userbot not connected),
`auto` behaves exactly like `api`: the API error is raised and the worker
cancels the number with the refund tally, as before.

**If both fail**, the error carries both reasons (`Checker API failed (...) and
the PRIMES bot checker failed too (...)`) and the usual cancel path runs.

Every fallback is logged, counted and visible:

```
[...] Checker API failed (is_down: Checker reports service is_down=true); falling back to
      the PRIMES bot checker for this and the next 60s.
[...] Checker: using the PRIMES bot checker for 9876543210 (API error: is_down)
[...] [MEESHO-BOT] Bot checker: 9876543210 -> NOT registered.
[...] Checker result via bot: is_registered=False (Target: False)
[...] Checker API answered again - back to API-first checking.
```

`/status` shows the mode, the cooldown left, and the counters
(`checker_api_checks`, `checker_bot_checks`, `checker_fallbacks`).

## 3. `bot`: how the PRIMES bot checker is driven

One check = reset to the main menu → open the checker → send the 10-digit
number → read the verdict → back to the main menu. The **live bot's screens
are covered by the defaults** (verified against the recorded screens and
locked in by `test_bot_checker_flow.py`):

```
/start -> [🛍️ PRIMES Meesho … 📍 Change Address / 🔍 Check Number / 🔗 Set Refer Link …]
   tap "🔍 Check Number"
        -> "🔍 Check Number — Send the 10-digit mobile number you want to verify.
            I'll tell you if it's registered on Meesho."   [✖️ Cancel]
   send 9876543210
        -> "🔍 +91 9876543210 — ✅ Registered on Meesho."     [🔍 Check Another] [🏠 Main Menu]
        or "🔍 +91 9876543210 — ❌ Not Registered on Meesho."  [🔍 Check Another] [🏠 Main Menu]
   tap "🏠 Main Menu"  (never "Check Another" - that would start another check)
```

* the menu's **🔍 Check Number** button is picked exactly; **🏷️ Check Price**,
  **Claim All Refunds**, **Set Refer Link**, … can never be picked for it;
* the prompt is never misread as a verdict, even though it says
  "…tell you if it's registered on Meesho";
* `✅ Registered on Meesho.` → `is_registered: true`,
  `❌ Not Registered on Meesho.` → `is_registered: false`.

`checker.bot` settings:

| Key | Default | Meaning |
| --- | --- | --- |
| `entry` | `auto` | `auto` = menu button if one is found, else `command`; `button` = only the menu button (error if missing); `command` = always send `command` |
| `command` | `""` | Bot command template used instead of a menu button, e.g. `"/check {number}"` |
| `button_hints` | `[]` | Extra menu-button labels (they **add** to the built-in list: `check number`, `check registration`, `check account`, `check status`, `verify number`, …) |
| `number_prompt_hints` | `[]` | Extra wording for the "send me the number" screen |
| `registered_hints` / `not_registered_hints` | `[]` | Extra result wording for registered / not-registered screens |
| `step_timeout_seconds` | `30` | Budget for one screen/step of the check |
| `max_attempts` | `2` | How many times the number may be sent if the bot keeps re-asking |
| `reset_after_check` | `true` | Return to the main menu after the verdict |
| `stop_after_failures` | `3` | Consecutive failed checks (bot mode / bot fallback) after which the whole run stops with a 🛑 alert instead of buying numbers only to cancel them. `0` = never stop |

Matching is done on emoji-stripped, lowercased labels, so hints are plain text
(`"check number"`, not `"🔍 Check Number"`). A button containing `balance`,
`offer`, `shop`, `wallet`, `referr`, … is never picked, so **Check Balance**
can't be mistaken for the checker.

The defaults already cover this wording (`✅ Registered on Meesho.`,
`❌ Not Registered on Meesho.`, `already registered`, `no account`, `not
linked`, `available`, caps variants, `🟢 number registered ✅`, …). Negative
wording wins when both appear, and a screen that is *asking* for the number is
never read as a result.

### Tuning it against the real bot (recommended once)

```
python meesho_bot_client.py                 # read-only: current screen + how it classifies
python meesho_bot_client.py 9876543210      # ONE check through the bot, verdict printed
```

The first command prints the checker classification, the entry button it would
tap and the verdict it reads; the second runs a real check and prints
`is_registered`. If a screen comes out as `unknown`, copy its exact wording
into the matching `checker.bot.*_hints` list — the error messages a failed
check raises already name the list to extend and include the screen text and
buttons.

### Safety properties

* The number is only ever typed into a screen the flow recognises as the
  checker's number prompt; an unrecognised screen fails loudly instead.
* Checks and logins share one conversation with one bot and are serialised by
  a lock, so a worker's check can never interleave with the coordinator's
  login flow (the second caller waits its turn).
* A check that starts while the bot sits on a leftover login/OTP screen
  resets to the main menu first.
* Because a check ends at the main menu, the bot checker is the only reason the
  login flow must be able to fall back to it: with `mode: "api"` (or `"auto"`
  while the API answers) an unrecovered **Change Number** leaves the bot
  in-flow instead of restarting from the menu
  (`meesho_bot.reset_to_menu_on_change_failure`, see SETUP_MEESHO_BOT.md).
  `/checker` reports which of the two applies right now, and a check that runs
  after a login flow simply resets to the menu itself.
* Failure modes are `CheckerUnavailable` at the router level: the existing
  "cancel + refund tally" safety net is unchanged.

## 4. When the bot is required but not ready

* `mode: "bot"` + userbot not ready → the run **does not start** (alert +
  explanation). Otherwise every bought number would be checked, fail, and be
  cancelled.
* `mode: "auto"` + userbot not ready → the run starts normally and API errors
  cancel numbers as before (a warning says so at startup).
* The userbot dying **mid-run** (`mode: "bot"`, or `auto` with the API also
  down) is caught too: after `stop_after_failures` consecutive failed checks
  the run stops with a 🛑 critical alert (`Checker unavailable`), after the
  number in flight has been cancelled with its refund tally. Fix the userbot,
  `/run` again — or `/checker api` to switch back.

Check the userbot setup in `SETUP_MEESHO_BOT.md` (`enabled`, `api_id`,
`api_hash`, `bot_username`, `userbot.session.txt` via `login_userbot.py`).

## 5. The API side: rate limits, retries & multiple keys

The checker service rate limits **per API key per service**. With several
provider workers validating numbers in parallel through a single key, the
server answers:

```
HTTP 429: {"detail":"Rate limit exceeded. Please wait 4.6s for service 'meesho'"}
```

`checker_client.py` absorbs that instead of failing activations:

1. **Retry the same number.** On HTTP 429 the wait the server asks for is
   honoured (from the `"Please wait Xs"` hint in the body, or a `Retry-After`
   header) plus a small buffer, then the same check is retried.
2. **Key rotation.** Several keys → a rate-limited key is skipped for a free
   one immediately; N keys give roughly N times the throughput. Keys rejected
   with 401/403 are retired for the rest of the run.
3. **Learned pacing.** After a 429 the spacing the server demanded is
   remembered per (key, service) so later workers wait their turn.
4. **Transient-error retries.** Network errors, timeouts and HTTP 5xx are
   retried with a short backoff.
5. **Bounded budget.** Everything is capped by `max_retries` and
   `max_retry_wait_seconds`. Only a genuinely exhausted budget fails — and the
   cancel/refund path (or the bot fallback in `auto` mode) takes over.

Failure types (`checker_client.py`) are what `auto` mode classifies:

| Exception | Meaning | Fallback default |
| --- | --- | --- |
| `CheckerServiceDown` | API said `is_down: true` | yes |
| `CheckerTimeout` | read/connect timeout | yes |
| `CheckerUnavailable` | network error | yes |
| `CheckerServerError` | HTTP 5xx | yes |
| `CheckerAuthError` | every key rejected | yes |
| `CheckerRateLimited` | 429 budget spent | no |
| `CheckerError` | `success=false`, 4xx, bad payload | no |

| Field | Default | Meaning |
| --- | --- | --- |
| `api_keys` | – | **Recommended.** Requests rotate across them with independent pacing per key. |
| `api_key` | – | Legacy single key (still honoured, merged with `api_keys`). |
| `service` | `meesho` | Service name sent to the API. |
| `max_retries` | `10` | Maximum attempts per check (429s, 5xx, network, timeouts). |
| `max_retry_wait_seconds` | `45` | Hard ceiling on total time spent retrying one API check. |
| `min_interval_seconds` | `1.0` | Conservative minimum spacing per key before a 429 teaches the real spacing. |
| `rate_limit_buffer_seconds` | `0.5` | Safety margin on top of the server-asked wait. |
| `network_backoff_seconds` | `1.5` | Backoff between retries after network errors / 5xx. |
| `timeout` | `15` | Per-request HTTP timeout — the read timeout that `auto` treats as "too slow" and hands to the bot. |

## 6. Verify

Offline checks (no network, scripted responses and a fake Telethon bot):

```bash
python test_checker_client.py               # API client: 429s, retries, error types
python test_checker_router.py               # modes, fallback triggers, cooldown
python test_bot_checker_flow.py             # bot checker screens + safety
python test_checker_mode_integration.py     # worker/run/Telegram wiring
python test_change_number_recovery.py       # which mode may keep the bot in-flow
```
