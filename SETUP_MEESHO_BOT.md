# PRIMES Meesho Bot Automation — Setup

The automation can drive the **PRIMES Meesho** Telegram bot itself, using a
regular Telegram *user* account (a "userbot"). This is MTProto/API automation —
it clicks the bot's real inline buttons and sends messages directly to
Telegram's servers; there is no screen tapping.

When disabled or not configured, the tool behaves exactly as before (manual
"OTP Triggered" gate in your notification bot).

## What it does automatically

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
     "max_change_number": 5,
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

6. Counters (`accounts_linked`, `otp_wrong`, `otp_expired`, `user_blocked`,
   `otp_timeout`, `change_number`, `offer_rerolls`, `referral_pasted/skipped`,
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
| Change Number recovery | never shows the referral screen; the replacement number is sent only at the number prompt |
| Refund not credited | 🛑 stop everything + critical alert; the bot is **left on its OTP screen** so the OTP can still be entered manually if it shows up |
