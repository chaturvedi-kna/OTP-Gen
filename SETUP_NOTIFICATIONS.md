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
- **`/run`**: Starts searching for fresh numbers from Telegram if the script was stopped or idle.
- **`/balance`**: Retrieves live balances for TemporaSMS and OtpDoctor.
- **`/stop`**: Gracefully stops the active search.
- **`/start`**: Shows available bot commands.

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

  "automation": {
    "target_registered": false,
    "max_attempts": 200,
    "require_manual_trigger": true,
    "trigger_wait_seconds": 300
  }
}
```
