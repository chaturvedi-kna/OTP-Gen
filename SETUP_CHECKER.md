# Checker setup: modes (API / dedicated checker bot / PRIMES bot / auto), rate limits & keys

Three ways to validate a number before it is spent, in preference order:

1. the **checker API** (Speedz Checker API v2, `https://tubesave.in`) — fast,
   parallel, rate limited per key;

> ⚠️ **API v2 migration (Sep 2026).** The old base URL (`superassets.in`) is
> dead — `checker.base_url` must be `https://tubesave.in` (docs:
> `https://tubesave.in/docs`). Free plan: **no monthly limit**, 1 request /
> 5s — and the proxy requirement was dropped again (**No Proxy Needed For
> API**, so nothing to add in Profile). Responses now also carry the number's
> operator (Jio / Airtel / Vi / BSNL) plus plan / validity / expiry / True-5G
> details where available — the worker log shows them in brackets. At startup
> the tool probes liveness + `GET /api/v1/me` and warns you if the API is
> down or the key is dead — before the first number is bought.
2. a **dedicated Telegram checker bot** (`"checker" -> "telegram_bot"`) —
   driven through the SAME logged-in Telegram account but a SECOND bot
   conversation, so it never walks the PRIMES login bot out of a waiting OTP
   screen. Fill in its username + screen hints; the same session file as
   `meesho_bot` is used, so **no extra `login_userbot.py` is needed**;
3. the **PRIMES login bot** as the last resort only — and never while an OTP
   is being waited on (a mid-flight login owns the bot, so the check is
   cancelled/refunded instead of walking away from a paid OTP).

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
  "base_url": "https://tubesave.in",
  "api_keys": ["AK__...", "AK__second_key_if_you_have_one"],
  "service": "meesho",
  "min_interval_seconds": 5.0,

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
    "reset_after_check": true,
    "claim_timeout_seconds": 60
  },

  "telegram_bot": {
    "enabled": false,
    "username": "",
    "name": "checker bot",
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

With `"telegram_bot"` enabled, the API falls back to THAT bot (same Telegram
account, separate conversation) before ever touching the PRIMES login bot -
and when the PRIMES bot is in use, it is still never used to check a number
while a login is waiting for its OTP (the check is cancelled instead).

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

## 1b. The dedicated checker bot (`checker.telegram_bot`)

`telegram_bot` reuses the same `userbot.session.txt` as the login userbot -
same `api_id`/`api_hash`, same account. You only describe WHERE the number
goes and HOW to read the reply:

* `entry` — `auto` (ask the screen, fall back to `command`), `button`, or
  `command` (if the bot only answers to a slash command);
* `command` — the slash command template, e.g. `"/check {number}"`;
* `button_hints` — extra labels to try on the menu if the automatic pick is
  not there (e.g. `"Check number"`, `"Number Check"`);
* `number_prompt_hints` — extra wording the bot uses when asking for the
  10-digit number;
* `registered_hints` / `not_registered_hints` — extra wording for the verdict.

Send the screenshot of the bot (or just paste its `/start` output) and these
can be tuned exactly. The flow is: it asks the bot for the 10-digit number,
reads `"registered" / "not registered" from the screen (matching the hints),
then optionally goes back to its main menu before it searches for another.

**Continuous checking (dedicated checker bot improvement):** after every number
check there is **no need for tapping Start** - it can directly give another
number. The dedicated bot stays at its result/prompt screen and the next
number is sent straight away without navigating via Main Menu / Check Number.
`reset_after_check: false` + `continuous: true` (the new defaults for
`checker.telegram_bot`) enables this. A "Check Another" button
(`check_another_hints`) is used as fallback when direct send is not accepted.
`checker.bot` (PRIMES) still defaults to `reset_after_check: true` because
that bot shares the login conversation.

### The "Meesho Xxpress Manish" bot (tuned defaults)

`config.json` -> `checker.telegram_bot` ships pre-tuned for the second
checker bot from the screenshots — set `enabled: true` and its `username`
(the bot's @handle; the display name "Meesho Xxpress Manish" is not the
username), and nothing else needs changing:

* `button_hints`: `"Check Number"` — the reply-keyboard button on the
  welcome screen (tapping it sends the label itself; no inline buttons);
* `number_prompt_hints`: the bot's Hinglish ask
  ("Meesho Number Check", "Ek ya kai phone numbers bhejo",
  "Cancel likho to exit", ...);
* verdict hints cover `NOT REGISTERED (NEW USER)` / `— REGISTERED`; the
  🆕 / ✅ badges are recognised even without any hint;
* the transient `⏳ Checking N number(s) on Meesho...` screen is waited out.

> If the bot gates you with "Join Channel" / "✅ I've Joined", do that ONCE
> by hand from the userbot's own Telegram account — afterwards the checker
> runs unassisted.

### Batch verification (`batch_enabled` / `batch_size` / `batch_wait_seconds`)

The bot accepts SEVERAL comma-separated numbers in one message
(`9876543210, 9123456789, ...`). Turn it on to answer up to `batch_size`
concurrent number checks with a single bot visit:

```json
"telegram_bot": {
  "enabled": true,
  "username": "@MeeshoXxpressManishBot",
  "batch_enabled": true,
  "batch_size": 3,
  "batch_wait_seconds": 6.0
}
```

* When one worker asks for a check, a short window (`batch_wait_seconds`)
  stays open for other workers' checks to join; the window also closes early
  once `batch_size` numbers have joined. One comma-separated message is sent,
  one reply answers the whole batch — much faster than sequential visits.
* **Matching is by number, never by position**: the bot's reply
  (`📋 Number Check Results ...`) lists its verdicts in a DIFFERENT order
  than the input (observed in the screenshots). Every participant is handed
  exactly the verdict for its own number; a number the bot forgot to answer
  fails just that one check (the usual cancel-and-refund path), not the batch.
* With batching off (default), every check is its own bot visit — same
  behaviour as before, but with `continuous: true` the dedicated bot accepts
  the next number directly without tapping Start. A lone check with batching
  on simply runs as a single visit after the window closes, and stays ready
  for the next batch.

### When the dedicated checker bot is NOT used (and why)

`enabled: true` alone is not enough - the userbot has to open that bot's chat,
which only resolves after THIS Telegram account has pressed START in it once.
Startup now says which case you are in:

```
Checker: dedicated checker bot ready - Meesho Xxpress Manish (@manishmeeshobot) ready; number
         checks run in that conversation and never touch the PRIMES login chat.
Checker: dedicated checker bot NOT usable - Meesho Xxpress Manish (@manishmeeshobot) NOT ready
         (...). Set checker.telegram_bot.username to the bot's @handle (not its display name)
         and press START in that bot once from this Telegram account.
```

and every fallback names the bot that actually answered:

```
[...] Checker: using the dedicated checker bot @manishmeeshobot for 9876543210 (API error: network)
[...] Checker: using the PRIMES bot checker for 9876543210 (API error: network) - the dedicated
      checker bot @manishmeeshobot cannot answer (the checker bot userbot is not connected)
```

Two failures that used to be silent:

* **`Could not open the dedicated checker conversation with @x`** - the account
  cannot resolve that @handle (typo, display name instead of the handle, or the
  bot was never started). The check is then **not** sent to the PRIMES login
  bot as a stand-in; it fails, and the number keeps its normal cancel path.
* **`Refusing to run a number check: the PRIMES bot conversation is being
  driven by the prewarm flow`** - the checker is pointed at the login bot (or
  no dedicated bot is configured) while the offer pre-warm owns that chat. A
  check and the pre-warm tap in the SAME conversation, so the check waits
  (`Checker: the PRIMES bot chat is busy with the prewarm flow ...`) or is
  refused - instead of walking the bot off the offer screen mid-reroll, which
  used to end in `Offer pre-warm failed: Offer screen has no reroll button`
  while the checker read a half-finished screen. With a dedicated checker bot
  configured, checks and the pre-warm simply run in two chats at once.

`checker.fallback` (all default `true` except `rate_limit`):

| Key (aliases) | Trigger |
| --- | --- |
| `is_down` (`service_down`, `down`) | the API answered `{"is_down": true}` for the service |
| `read_timeout` (`timeout`, `timed_out`) | connect/read timeout — "takes more time to respond" |
| `network` (`network_error`, `connection`) | connection refused / DNS / TLS failures |
| `http_5xx` (`5xx`, `server_error`) | the API returned a 5xx |
| `auth` (`auth_error`, `bad_keys`) | every configured API key was rejected (401/403), incl. Free-plan "no verified proxy" |
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
(`checker_api_checks`, `checker_dedicated_checks` - answered by
`checker.telegram_bot` -, `checker_bot_checks` - answered by the PRIMES bot -,
`checker_fallbacks`).

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
| `reset_after_check` | `true` (PRIMES) / `false` (dedicated) | Return to the main menu after the verdict. For dedicated checker bot `false` keeps it ready for direct next number |
| `continuous` / `reuse_checker` | `false` (PRIMES) / `true` (dedicated) | Stay at result/prompt so next number is sent directly without tapping Start. Dedicated bot improvement |
| `check_another_hints` | `[]` | Extra labels for "Check Another Number" button (fallback when direct send fails) |
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
* The two near-identical "send the number" prompts are told apart by
  vocabulary: the login flow accepts its own Change Number prompt
  ("✏️ Change Number — Send the 10-digit mobile number you'd like to use
  instead." with a lone Cancel button) even without a price line, but never
  accepts the checker's prompt ("Send the 10-digit mobile number you want to
  verify … registered on Meesho") on a context-free read — check/verify/
  registered wording rules it out, so a paid number can never be typed into
  the checker.
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

The checker service rate limits **per API key per service** (v2: Free = 1
request / 5s / service / key; Starter / Plus / Pro = 30 / 45 / 60 per minute
per key plus a monthly quota). With several provider workers validating
numbers in parallel through a single key, the server answers:

```
HTTP 429: {"detail":"Rate limit exceeded. Please wait 4.6s for service 'meesho'"}
```

**Free plan + Indian proxy (no longer needed).** The Sep 2026 update dropped
the proxy requirement ("No Proxy Needed For API"): Free keys work with no
proxy and no monthly limit. The detection stays as a safety net — if a key is
ever refused with HTTP 403 mentioning the proxy, the client raises
`CheckerProxyError` (a `CheckerAuthError`, so `auto` mode falls back to the
bot checker) with the fix in the message, and only that key is retired.

**Operator info in responses.** The API enriches each verdict with the
number's operator (`Jio` / `Airtel` / `Vi` / `BSNL`) and, where available,
plan / validity / expiry / True-5G details. The client passes every extra
field through untouched, and the worker log prints them:
`Checker result via api: is_registered=False (Target: False) [operator=JIO
validity=...]`. Field names beyond `operator` are not in the published
schema, so unknown fields are shown generically (`key=value`).

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
| `min_interval_seconds` | `5.0` | Minimum spacing per key: matches the Free plan (1 req / 5s). Paid plans can lower it to `2.0`–`1.0`. A 429 still teaches longer spacing automatically. |
| `rate_limit_buffer_seconds` | `0.5` | Safety margin on top of the server-asked wait. |
| `network_backoff_seconds` | `1.5` | Backoff between retries after network errors / 5xx. |
| `timeout` | `15` | Per-request HTTP timeout — the read timeout that `auto` treats as "too slow" and hands to the bot. |

**Account helpers (v2).** `CheckerClient.get_me()` (`GET /api/v1/me`: plan,
rate window, usage, `proxy_required`), `.list_services()` and `.health()` are
single-shot probes. The coordinator calls `get_me()` once at startup and logs
`key OK (plan '…', rate window …s)` — or alerts when the key is dead / the
Free-plan proxy is missing.

**Liveness gate (no auth).** Before trusting the API, the router asks
`GET /health` (cached `health_cache_seconds`, default 60s; disable with
`health_check_enabled: false`):

* a down API is **not asked at all** — `auto` goes straight to the bot (plus
  cooldown), `api` cancels with a clear "liveness probe says down" reason;
* an **ambiguous answer** (`success=false` / 4xx / malformed — normally
  definitive, no fallback) is re-verified with a **fresh** probe: if the
  service itself is down, it is treated as `is_down` (bot fallback) instead
  of acting on garbage by cancelling the number. If the service is up, the
  answer stands and the number is cancelled as before.
* the startup preflight probes liveness first and alerts
  (`⚠️ Checker API is down`), entering the API cooldown immediately in
  `auto` mode so check #1 already uses the bot.

Fresh probes are counted (`checker_health_probes` / `checker_health_down`).
Like `is_down: true` before it, a down service never produces a verdict —
only a fallback (or a clearly-labelled cancel in `api` mode).

## 6. PRIMES fallback disabled + self-heal mode (new)

**Problem solved:** dedicated checker `@manishmeeshobot` and PRIMES bot share the SAME Telegram account → same FloodWait limit (2622s in prod). Old flow: dedicated fails → fallback to PRIMES → PRIMES hits FloodWait → cancellation. Wasted.

**Fix:**

1. **PRIMES fallback disabled by default** when a dedicated checker bot is configured:
```json
"telegram_bot": {
  "enabled": true,
  "username": "@manishmeeshobot",
  "fallback_to_primes": false,   // default false - no PRIMES fallback
  "self_heal_enabled": true,     // default true - auto pause on FloodWait
  "self_heal_max_wait_seconds": 3600  // cap pause at 1h (2622s will be honored)
}
```
- `fallback_to_primes: false` (default): if dedicated checker fails for ANY reason, it does NOT try PRIMES. It cancels with refund directly. Set to `true` to restore old behaviour (try PRIMES as last resort).
- When no dedicated bot is configured, PRIMES is still used as before (flag only matters when dedicated is present).

2. **Self-heal mode** for FloodWait:
- When `A wait of X seconds is required` is seen, `meesho_bot_client` records `_floodwait_until` (shared between both bots via proxy).
- `checker_router` tracks bot FloodWait cooldown (`bot_floodwait_remaining()`).
- Worker loop (`main.py`) detects FloodWait, enters self-heal pause:
  - Logs `🤖 Self-heal mode ON: pausing WORKER for Xs (FloodWait was Ys)`
  - Sleeps in 5s chunks (responsive to stop), up to `self_heal_max_wait_seconds` (default 3600).
  - During pause, no new numbers are bought - prevents buying numbers that will be cancelled.
  - After pause, clears cooldown and resumes. Failure streak is NOT counted, so it doesn't trigger critical stop.
  - If `self_heal_enabled: false`, old behaviour: immediate cancel and continue (may hit FloodWait repeatedly).

**Config changes required?** None - defaults are safe:
- Existing installs without these keys get `fallback_to_primes=false` (no PRIMES fallback) and `self_heal_enabled=true` (auto pause). This is what you want to prevent the 2622s cascade.
- To re-enable old PRIMES fallback: set `checker.telegram_bot.fallback_to_primes: true` in config.json.
- To disable self-heal pause: set `checker.telegram_bot.self_heal_enabled: false`.

**Logs you will see:**
```
Dedicated checker bot hit FloodWait (A wait of 2622 seconds...); NOT trying PRIMES bot as fallback (same Telegram account shares the limit) - cancelling with refund.
Checker error (mode auto): Telegram FloodWait - ... Both dedicated checker and PRIMES share the same Telegram account, so they share the rate limit. Cancelling 9954276790 with refund.
🤖 Self-heal mode ON: pausing VSIMPRO for 2622s (FloodWait was 2622s, max_wait 3600s) - will auto-resume after cooldown.
```

## 7. Verify

Offline checks (no network, scripted responses and a fake Telethon bot):

```bash
python test_checker_client.py               # API client: 429s, retries, error types
python test_checker_router.py               # modes, fallback triggers, cooldown
python test_bot_checker_flow.py             # bot checker screens + safety
python test_checker_mode_integration.py     # worker/run/Telegram wiring
python test_change_number_recovery.py       # which mode may keep the bot in-flow
```
