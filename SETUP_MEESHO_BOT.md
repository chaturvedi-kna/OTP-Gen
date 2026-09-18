# PRIMES Meesho Bot Automation — Setup

The automation can drive the **PRIMES Meesho** Telegram bot itself, using a
regular Telegram *user* account (a "userbot"). This is MTProto/API automation —
it clicks the bot's real inline buttons and sends messages directly to
Telegram's servers; there is no screen tapping.

When disabled or not configured, the tool behaves exactly as before (manual
"OTP Triggered" gate in your notification bot).

The same userbot can also run the **bot's own number checker** — used as a
fallback when the checker API is down or too slow (`checker.mode: "auto"`, or
`"bot"` to use it for every check). Screen wording, entry button and timeouts
are configured under `"checker"` → `"bot"`; the full explanation, the tuning
helper (`python meesho_bot_client.py <number>`) and the safety rules are in
**SETUP_CHECKER.md** (§1–§4). Notes that concern this file:

* `checker.mode: "bot"` requires this userbot to be ready — the run refuses to
  start otherwise (numbers would otherwise be bought only to be cancelled).
* `checker.mode: "auto"` works without the userbot; API errors then cancel the
  number exactly as before.
* A number check and a login flow never interleave: both use the one bot
  conversation and are serialised, so enabling the bot checker costs nothing
  in flow safety.

## What it does automatically

While workers are still hunting for a number, the bot is parked on an agreed
offer (**offer pre-warm**): the Add Account → Login with Number → Normal walk
and the reroll to `target_upi_price` already happened **before** any number
exists. As soon as a number is found it is typed into that parked prompt
without waiting for an offer. The parked prompt is re-armed automatically if
it times out. Controlled by `automation.prewarm_offer` (default on),
`meesho_bot.offer_warm_refresh_seconds` (default 90) and
`meesho_bot.warmup_max_offer_rerolls` (0 = reuse `max_offer_rerolls`).

**Price guard before typing.** A paid number is NEVER typed into an offer
priced above `target_upi_price`: right before sending, the parked prompt's
price is verified — a drifted/expensive offer is re-rolled **in place** until
it fits (a genuine Change Number prompt carries no price line and no reroll
button; its price was already agreed when the offer was parked, so it is
accepted as-is). If no in-budget offer can be reached within the reroll
budget, the login fails loudly with the number UN-typed (so it can be
cancelled with a refund), instead of silently paying the higher UPI price.

1. `/start` → **Add Account** → **Login with Number**
2. **Referral step (optional — the bot does not always show it)** — when it does
   (🔗 *Set Refer Link* / 🎁 *Referral link?*), it appears **before** the
   Normal/Auto login mode and the configured `referral_link` is pasted for it
   (the bot commonly asks twice per login: once to save the link, once per
   account — see `max_referral_pastes`). If the screen never appears the login
   simply continues. The step is never Cancel-ed and never gets the phone number
   or the OTP code typed into it
3. **Normal** login mode
4. Reads the offer's `UPI · ₹` price and taps **Try Another Offer** until
   UPI ≤ `target_upi_price` (default ₹47; configurable), before any number is spent.
   Some bot revisions show a three-button **Try Again** variant in place of the
   offer instead of an offer with a price — it is tapped exactly the same way
   (and also whenever it appears while waiting for an offer). The transient
   "Setting things up…" screen the bot shows between a tap and the offer is
   simply waited out
5. Sends the found (unregistered) 10-digit number → the bot briefly shows
   "⏳ Sending your OTP…" before the "OTP on its way" screen; that transient
   is waited out, so it never cancels the number
6. Polls the OTP provider (tempora/vsimpro) exactly as before
7. Sends the OTP to the bot; on **Account linked** records User ID / account #,
   sends a 🎉 Telegram notification, and finishes the provider activation
8. If the OTP never arrives / is wrong / expired / number blocked, taps
   **Change Number**, verifies the provider refund tallied, and continues with
   the next found number — no full menu restart. A referral prompt re-appearing
   during recovery is answered the same way, so a paid number is never typed
   into the referral field or lost behind that screen

### Verified screen coverage

The recognition above is checked offline against the bot's **real** screens (fixtures
transcribed from live chats), in `test_primes_referral_flow.py` and
`test_change_number_recovery.py`:

| Screen | Copy (trimmed) | Must behave as |
| --- | --- | --- |
| Offer (login flow) | `Not happy with it? Tap 🔄 Try Another Offer to reroll. / 📱 Otherwise send your 10-digit mobile number to continue.` + `🔄 Try Another Offer` / `✖ Cancel` | an offer (`classify() → offer`); number prompt |
| Change Number prompt | `✏️ Change Number / Send the 10-digit mobile number you'd like to use instead.` + a lone `✖ Cancel` | THE number prompt - warm **and** cold reads, despite having no price line and no reroll button |
| Checker prompt | `🔍 Check Number / Send the 10-digit mobile number you want to verify. / I'll tell you if it's registered on Meesho.` + `✖️ Cancel` | NEVER a login prompt on a cold read (checker vocabulary); only typed into by the checker flow |
| Preparing transient | `⏳ Setting things up… / Finding the best offer for you.` | its own `working` state: waited out, never "no reroll button" |
| Failed-offer variant | `⚠️ Failed to fetch offer … UPI · ₹83 … Offer · Null` + `🔄 Try Again` / `➡️ Continue without offer` / `❌ Cancel` | a reroll screen: tapped, never typed into (decoy price) |

## Change Number recovery (no needless menu restart)

Getting back to the number prompt is much cheaper than restarting the flow
(**Add Account → Login with Number → Normal → offer rerolls**), so the recovery
works hard — but bounded — before it gives up:

1. **The prompt is recognised, not guessed.** `classify()`'s offer heuristics
   (`Try Another Offer` button, "10-digit mobile … continue") are only one way
   in: a screen that asks for the mobile number to link is accepted as the
   prompt as well, so a bot revision with different copy no longer ends the
   recovery with *"Expected number prompt after Change Number, got unknown"*.
   Unknown copy can be taught with `meesho_bot.number_prompt_hints`. The
   **⚠️ Failed to fetch offer / 🔄 Try Again** variant is never a prompt (its
   price lines are a decoy — it is rerolled), and the bot **checker's** "send
   the number" prompt is never mistaken for the login prompt on a cold read.
2. **An ignored tap is retried** (`change_number_retries`, default 2 extra
   taps), each wait bounded by `change_number_timeout_seconds` and the whole
   recovery by `change_number_budget_seconds` — never the full
   `step_timeout_seconds` on a screen that will not move.
3. **Already at the prompt?** Nothing is tapped: the coordinator asks read-only
   (`at_number_prompt()`) first, and a recovery that reported failure is
   re-read once before it is acted on.
4. **Only a real dead end resets the flow — and only when the menu is needed.**
   `reset_to_menu_on_change_failure` decides:

   | Value | Behaviour |
   |---|---|
   | `"auto"` (default) | Reset to the main menu **only when the bot checker is needed** for the next number check (`checker.mode: "bot"`, or `"auto"` while the checker API is down / cooling down / has no keys — the bot checker works from the menu, so the reset costs nothing extra). **With the checker API answering, the bot is left in-flow**: the next login reuses the number prompt it is sitting on instead of paying for a menu restart and a fresh offer reroll. |
   | `"always"` | Always reset (the old behaviour). |
   | `"never"` | Never reset from the recovery. |

   Leaving the bot in-flow is safe: when the next login cannot reuse what is on
   screen it walks back to the main menu itself, exactly as before.
5. **A prompt the bot is already on is reused** by the next login
   (`reuse_number_prompt`, default `true`): no menu walk, and an over-target
   price is rerolled **in place** instead of from a fresh flow. An unpriced
   prompt is only reused when its wording is an explicit number prompt or it can
   be rerolled, so `target_upi_price` keeps being enforced.

`/status` counts both outcomes (`Change-number: N (menu resets: X | kept
in-flow: Y)`), and every **🔄 Changing number** notification says which one
happened and why.

Verified offline (no Telegram, no network — a fake bot speaks the screens):

```bash
python test_primes_referral_flow.py     # flow + Change Number recovery screens
python test_change_number_recovery.py   # the reported bug, end to end
python test_primes_coordinator_integration.py
```

## Referral link

Paste your Meesho referral link once and it is given to the bot whenever the
bot asks for it. Three ways to set it:

**1. From Telegram (no restart needed)** — send to your notification bot:

```
/referral https://app.meesho.com/...?via=...
```

`/referral` alone shows the current setting, `/referral off` clears it. The link
is saved into `config.json` and applied to the running userbot immediately.

**2. From the CLI**

```
python main.py --set-referral-link "https://app.meesho.com/...?via=..."
python main.py --no-referral-link     # clear it
```

**3. Directly** — `config.json` → `meesho_bot.referral_link`.

### If the link cannot be used: `referral_failure_action`

This is the important switch. It applies when the bot **does** show the referral
screen and the link cannot be delivered (nothing set, the bot rejects it, or it
keeps asking after the paste budget):

| `referral_failure_action` | Behaviour |
|---|---|
| `"stop"` (default) | 🛑 Stop the automation, send a max-priority alert with the screen, its buttons and the reason, cancel the number, and **verify the refund tallied** — nothing was submitted to Meesho, so no money may leak. Nothing new is purchased until you fix the link and `/run` again. |
| `"skip"` | Tap the bot's own **🚫 I don't have a refer code** option and continue the login without a referral. |

When the bot does **not** show the referral screen, the flow continues normally in
both modes — a missing link never blocks a login that never asks for one.

* `max_referral_pastes` (default **2**) — the bot usually asks twice per login
  (save-the-link screen, then the per-account prompt), so the link is pasted once
  for each prompt. A **rejected** link is never retried.
* `max_referral_events` (default 4) — how many times the referral screen may
  interrupt one login before the flow stops rather than guessing.
* **Change Number recovery never involves the referral screen** (the bot goes
  straight back to the number prompt), and the flow is verified for that path:
  the replacement number is only sent once the bot actually shows the number
  prompt, so it can never land in a referral field.

The OTP notification reports which referral action was taken
(`Referral: pasted referral link` / `Referral: tapped '🚫 I don't have a refer
code'`), and `/status` shows the configured link plus the failure action.

Check what the bot currently shows, and how the flow classifies it, without
sending any number:

```
python meesho_bot_client.py
```

## Safety: refund / balance tally

After every cancellation the balance must return to its pre-purchase value
(`balance_guard`). If the refund is not credited within
`refund_wait_seconds`, **all workers stop** and you get a 🛑 critical alert with
the activation id, number, expected vs actual balance, and any late OTP code
that arrived during the cancel race — so no money can silently leak and no new
numbers are bought while the ledger is off.

A final salvage check runs both at OTP timeout and right after the cancel call:
if the SMS lands in that window, the code is surfaced immediately (and used in
auto mode) instead of being lost.

## One-time setup

1. Create `api_id` / `api_hash` at <https://my.telegram.org> → API development
   tools. Use a **dedicated/burner Telegram account** (user automation carries
   account-ban risk; do not use your personal account).
2. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

3. From that dedicated account, open the PRIMES Meesho bot once and tap Start
   (so Telegram resolves it).
4. Run the interactive login and follow the prompts (login code arrives inside
   that Telegram account; 2FA password if enabled):

   ```
   python login_userbot.py
   ```

   This writes `userbot.session.txt` (chmod 600). It grants full account access —
   never commit or share it (already in `.gitignore`).

5. Fill `config.json` → `meesho_bot`:

   ```json
   "meesho_bot": {
     "enabled": true,
     "api_id": 1234567,
     "api_hash": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
     "session_file": "userbot.session.txt",
     "bot_username": "@THE_BOT_USERNAME",
     "login_mode": "Normal",
     "referral_link": "https://app.meesho.com/...?via=...",
     "referral_failure_action": "stop",
     "max_referral_pastes": 2,
     "max_referral_events": 4,
     "target_upi_price": 47,
     "max_offer_rerolls": 30,
  "warmup_max_offer_rerolls": 0,
  "offer_warm_refresh_seconds": 90,
     "max_change_number": 5,
     "change_number_retries": 2,
     "change_number_timeout_seconds": 0,
     "change_number_budget_seconds": 0,
     "change_number_variant_taps": 3,
     "working_screen_waits": 2,
     "number_prompt_hints": [],
     "reuse_number_prompt": true,
     "reset_to_menu_on_change_failure": "auto",
     "step_timeout_seconds": 60,
     "flow_timeout_seconds": 0,
     "human_delay_seconds": [1.0, 2.5]
   }
   ```

   **The bot's @username is required** — it is not visible in the screenshots.
   `human_delay_seconds` randomizes pauses between taps for human-like timing.
   `step_timeout_seconds` is the per-step budget (one screen wait/settle);
   `flow_timeout_seconds` is the hang watchdog for a whole flow: a flow is
   aborted only when **no step finishes** for that long (a hung Telegram call).
   `0` (default) = auto: `max(180, 4 × step_timeout)`. Long offer-reroll
   sessions are never killed — every completed poll/tap resets the clock — and
   a timeout aborts the flow with a clear alert (stating whether the
   number/code had already been sent) instead of crashing the automation.
   `referral_link` may be left empty, but then the automation stops and reports
   if the bot asks for a referral link (`referral_failure_action: "stop"`, the
   default). Set it from Telegram with `/referral <link>` while the tool runs —
   no config edit or restart needed.

   **Change Number recovery knobs** (see *Change Number recovery* above):
   `change_number_retries` (default **2**) is how many extra taps are tried when
   the bot ignores **Change Number**; `change_number_timeout_seconds`
   (`0` = auto: `min(step_timeout, 20)`) is the per-wait budget for the number
   prompt; `change_number_budget_seconds` (`0` = auto: 30–45s, whatever is
   closest to `step_timeout_seconds`) caps the whole recovery;
   `change_number_variant_taps` (default **3**) bounds
   **🔄 Try Again** variant taps during it; `working_screen_waits` (default **2**)
   is how many extra step-timeout-long rounds the bot's transient
   **"⏳ Setting things up…"** screen (between a reroll tap and the next offer)
   is waited out before the flow reports *"the bot stayed on its preparing
   screen … the next offer never appeared"* — it must never fail as
   "Offer screen has no reroll button". `number_prompt_hints` adds wording
   for the login number prompt when your bot revision's copy is not recognised
   (the recovery error prints the screen text and names this list).
   `reuse_number_prompt` (default **true**) lets a login send its number from a
   prompt the bot is already on instead of restarting from the main menu, and
   `reset_to_menu_on_change_failure` (`"auto"` / `"always"` / `"never"`) decides
   whether an unrecovered Change Number drops the bot back to the menu — by
   default only when the **bot checker** needs the menu.

6. Counters (`accounts_linked`, `otp_wrong`, `otp_expired`, `user_blocked`,
   `otp_timeout`, `change_number`, `bot_menu_resets` / `bot_flow_kept`,
   `offer_rerolls`, `referral_pasted/skipped`,
   `checker_api_checks` / `checker_bot_checks` / `checker_fallbacks`,
   `refunds_verified/missing`, `late_otp_salvaged`) persist in `stats.json` and
   are shown via `/status` and
   in every event notification. Set `automation.stop_after_success` to `true`
   for the old one-account-then-stop behavior; `false` keeps farming accounts.

## Failure handling

| Bot screen | Action |
|---|---|
| Referral link prompt (🎁 / 🔗 Set Refer Link) | paste `referral_link` for each prompt (up to `max_referral_pastes`), then continue the login |
| Referral link rejected, or asked again after the paste budget | `"stop"`: 🛑 stop, report, cancel the number + verify the refund. `"skip"`: tap **🚫 I don't have a refer code** and continue |
| Referral prompt with no link configured | `"stop"`: 🛑 stop, report, cancel the number + verify the refund. `"skip"`: tap the skip option and continue |
| Referral screen never appears | nothing to do — the login continues normally (verified for the Change Number path too) |
| Wrong / incorrect OTP | count `otp_wrong`, Change Number, verify refund, continue |
| "🔎 Verifying your code…" after the code | transient — waited out until "Account linked!" / the error screen; stuck > `step_timeout_seconds` → alert stating the **code WAS submitted** |
| OTP expired | count `otp_expired`, Change Number, continue |
| OTP timeout (no code in the window) | **cancel first, move the bot second**: final salvage probes → provider cancel + refund tally → only a clean, refunded cancellation taps Change Number. If the SMS lands inside the cancel race, the code is **auto-submitted while the bot still waits on its OTP screen** (immediate alert with the code either way — manual entry is still possible); the charge stands, no false refund alarm |
| Number blocked / banned / already registered | count `user_blocked`, cancel/reset, continue |
| Unknown/unexpected screen | alert with the screen text **and the buttons**, cancel the number (refund verified — nothing was submitted), reset flow |
| Telegram side hangs (no screen/tap progress for `flow_timeout_seconds`) | ⏱️ `MeeshoBotTimeout` — alert (with the stage it hung at), cancel the number (refund verified — the tally flags it if the number had already reached the bot), reset flow, **automation continues** |
| Change Number used > `max_change_number` in a row | reset to main menu (full flow restart) |
| Change Number tap ignored by the bot | retried (`change_number_retries` extra taps) inside `change_number_timeout_seconds` / `change_number_budget_seconds` — no menu restart |
| Reroll lands on **⏳ Setting things up…** | the bot's preparing transient: classified as its own `working` state and waited out (`working_screen_waits` extra rounds) so a throttled bot holding it past one step timeout no longer fails with "Offer screen has no reroll button" (and never cancels a paid number for it). If it *never* resolves the failure says exactly that — give the bot a rest before the next number |
| Change Number lands on a number prompt with copy `classify()` does not know | recognised as the prompt (built-in wording + `number_prompt_hints`) → the replacement number is sent from it: no "got unknown" failure, no menu restart, no offer reroll |
| Change Number unrecoverable (dead-end screen) | `reset_to_menu_on_change_failure`: `"auto"` resets to the main menu **only when the bot checker needs it** (`checker.mode: "bot"`, or `"auto"` while the API is down / cooling down / keyless); with the checker API answering the bot is **left in-flow** and the next login reuses its prompt (or walks back to the menu itself if the bot really is lost) |
| Bot left the login flow by itself (main menu / link choice / login mode) | reported as `needs_full_flow` — no extra reset, the next number runs the full flow |
| Bot no longer on the remembered prompt when a number is ready (e.g. a bot check reset it to the menu) | read-only re-check → the full flow runs instead of typing into the wrong screen; the number is **not** cancelled for it |
| Change Number recovery | never shows the referral screen; the replacement number is sent only at the number prompt |
| Refund not credited | 🛑 stop everything + critical alert; the bot is **left on its OTP screen** so the OTP can still be entered manually if it shows up |
