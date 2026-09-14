# PRIMES Meesho Bot Automation — Setup

The automation can drive the **PRIMES Meesho** Telegram bot itself, using a
regular Telegram *user* account (a "userbot"). This is MTProto/API automation —
it clicks the bot's real inline buttons and sends messages directly to
Telegram's servers; there is no screen tapping.

When disabled or not configured, the tool behaves exactly as before (manual
"OTP Triggered" gate in your notification bot).

## What it does automatically

1. `/start` → **Add Account** → **Login with Number** → **Normal**
2. Reads the offer's `UPI · ₹` price and taps **Try Another Offer** until
   UPI ≤ `target_upi_price` (default ₹47; configurable), before any number is spent
3. Sends the found (unregistered) 10-digit number → the bot triggers the OTP
4. Polls the OTP provider (tempora/vsimpro) exactly as before
5. Sends the OTP to the bot; on **Account linked** records User ID / account #,
   sends a 🎉 Telegram notification, and finishes the provider activation
6. If the OTP never arrives / is wrong / expired / number blocked, taps
   **Change Number**, verifies the provider refund tallied, and continues with
   the next found number — no full menu restart

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
     "target_upi_price": 47,
     "max_offer_rerolls": 30,
     "max_change_number": 5,
     "step_timeout_seconds": 60,
     "human_delay_seconds": [1.0, 2.5]
   }
   ```

   **The bot's @username is required** — it is not visible in the screenshots.
   `human_delay_seconds` randomizes pauses between taps for human-like timing.

6. Counters (`accounts_linked`, `otp_wrong`, `otp_expired`, `user_blocked`,
   `otp_timeout`, `change_number`, `refunds_verified/missing`,
   `late_otp_salvaged`) persist in `stats.json` and are shown via `/status` and
   in every event notification. Set `automation.stop_after_success` to `true`
   for the old one-account-then-stop behavior; `false` keeps farming accounts.

## Failure handling

| Bot screen | Action |
|---|---|
| Wrong / incorrect OTP | count `otp_wrong`, Change Number, verify refund, continue |
| OTP expired | count `otp_expired`, Change Number, continue |
| Number blocked / banned / already registered | count `user_blocked`, cancel/reset, continue |
| Unknown/unexpected screen | alert with the screen text, cancel the number, reset flow |
| Change Number used > `max_change_number` in a row | reset to main menu (full flow restart) |
| Refund not credited | 🛑 stop everything + critical alert |
