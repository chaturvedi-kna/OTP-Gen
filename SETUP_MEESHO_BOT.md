# PRIMES Meesho Bot Automation — Setup

The automation can drive the **PRIMES Meesho** Telegram bot itself, using a
regular Telegram *user* account (a "userbot"). This is MTProto/API automation —
it clicks the bot's real inline buttons and sends messages directly to
Telegram's servers; there is no screen tapping.

When disabled or not configured, the tool behaves exactly as before (manual
"OTP Triggered" gate in your notification bot).

## What it does automatically

1. `/start` → **Add Account** → **Login with Number**
2. **Referral step** — the bot asks for a Meesho referral link here
   (🔗 *Set Refer Link* / 🎁 *Referral link?*), **before** the Normal/Auto login
   mode. With `referral_link` configured the link is pasted once per login;
   without one the bot's own **🚫 I don't have a refer code** option is tapped.
   The step is never Cancel-ed and never gets the phone number typed into it
3. **Normal** login mode
4. Reads the offer's `UPI · ₹` price and taps **Try Another Offer** until
   UPI ≤ `target_upi_price` (default ₹47; configurable), before any number is spent
5. Sends the found (unregistered) 10-digit number → the bot triggers the OTP
6. Polls the OTP provider (tempora/vsimpro) exactly as before
7. Sends the OTP to the bot; on **Account linked** records User ID / account #,
   sends a 🎉 Telegram notification, and finishes the provider activation
8. If the OTP never arrives / is wrong / expired / number blocked, taps
   **Change Number**, verifies the provider refund tallied, and continues with
   the next found number — no full menu restart. A referral prompt re-appearing
   during recovery is answered the same way, so a paid number is never typed
   into the referral field or lost behind that screen

## Referral link (optional but recommended)

Paste your Meesho referral link once and the bot can attach it to every new
account. Two ways to save it:

```
python main.py --set-referral-link "https://app.meesho.com/...?via=..."
```

or edit `config.json` → `meesho_bot.referral_link` directly. Remove it again
with `python main.py --no-referral-link`.

Behaviour is the same either way from the coordinator's point of view; the
notification for a triggered OTP reports which referral action was taken
(`Referral: pasted referral link` / `Referral: tapped '🚫 I don't have a refer
code'`).

* The `🔗 Set Refer Link` variant shows **🏠 Main Menu** but no skip button, so
  a configured link is what keeps that login alive. The `🎁 Referral link?`
  variant always offers a skip option, so it works with or without a link.
* A rejected/expired link is detected (invalid / expired / already used) and the
  bot's skip option is used for the rest of that login, so the account creation
  still goes through.
* A link is pasted **at most once per login** (`max_referral_pastes`); if the bot
  asks again the skip option is used instead.
* The referral prompt may re-appear up to `max_referral_events` times per login
  (default 3). Beyond that the flow stops with an alert rather than guessing.

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
     "max_referral_pastes": 1,
     "max_referral_events": 3,
     "target_upi_price": 47,
     "max_offer_rerolls": 30,
     "max_change_number": 5,
     "step_timeout_seconds": 60,
     "human_delay_seconds": [1.0, 2.5]
   }
   ```

   **The bot's @username is required** — it is not visible in the screenshots.
   `human_delay_seconds` randomizes pauses between taps for human-like timing.
   `referral_link` may be left empty: the bot's own "I don't have a refer code"
   button is then used at the referral step (see the section above).

6. Counters (`accounts_linked`, `otp_wrong`, `otp_expired`, `user_blocked`,
   `otp_timeout`, `change_number`, `referral_pasted/skipped`,
   `refunds_verified/missing`, `late_otp_salvaged`) persist in `stats.json` and
   are shown via `/status` and
   in every event notification. Set `automation.stop_after_success` to `true`
   for the old one-account-then-stop behavior; `false` keeps farming accounts.

## Failure handling

| Bot screen | Action |
|---|---|
| Referral link prompt (🎁 / 🔗 Set Refer Link) | paste `referral_link` once, else tap **🚫 I don't have a refer code**; continue the login |
| Referral link rejected (invalid / expired) | fall back to the skip option for that login, continue |
| Referral prompt that cannot be answered (no link configured and no skip button) | stop with an alert naming the config key, the screen text and the buttons offered — the number is cancelled with its refund verified, never typed into the referral field |
| Wrong / incorrect OTP | count `otp_wrong`, Change Number, verify refund, continue |
| OTP expired | count `otp_expired`, Change Number, continue |
| Number blocked / banned / already registered | count `user_blocked`, cancel/reset, continue |
| Unknown/unexpected screen | alert with the screen text **and the buttons**, cancel the number (refund verified — nothing was submitted), reset flow |
| Change Number used > `max_change_number` in a row | reset to main menu (full flow restart) |
| Refund not credited | 🛑 stop everything + critical alert |
