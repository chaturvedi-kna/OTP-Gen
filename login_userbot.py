"""
One-time login for the PRIMES Meesho userbot.

Run this once on the device/account that will drive the Telegram bot:

    python login_userbot.py

You will be asked for:
  - api_id and api_hash from https://my.telegram.org (API development tools)
  - the phone number of the DEDICATED Telegram account (use a burner,
    not your personal account)
  - the login code Telegram sends inside that account
  - the 2FA password, if the account has one

A reusable StringSession is written to the file configured in config.json
(meesho_bot.session_file, default userbot.session.txt). Keep that file secret:
it grants full access to the Telegram account. Never commit it.
"""

import getpass
import json
import os


CONFIG_FILES = ["config.json", "config.jon"]


def load_config():
    for name in CONFIG_FILES:
        if os.path.exists(name):
            with open(name, "r", encoding="utf-8") as f:
                return json.load(f)
    return {}


def ask(prompt, default="", secret=False):
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{prompt}{suffix}: ").strip()
    return value or default


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="One-time login for the PRIMES Meesho userbot.")
    parser.add_argument("--instance", default=None,
                        help="config.json instance (e.g. tempora / vsimpro): "
                             "its instances block is merged in first, so the "
                             "session is written to THAT instance's "
                             "meesho_bot.session_file.")
    parser.add_argument("--session-file", default=None,
                        help="explicit path for the session file (wins over "
                             "config and instances).")
    args = parser.parse_args()

    try:
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession
        from telethon.errors import SessionPasswordNeededError
    except ImportError:
        print("telethon is not installed. Install it first:  pip install telethon")
        return 1

    config = load_config()
    if args.instance:
        from runtime import apply_instance_overrides
        config = apply_instance_overrides(config, args.instance)
        print(f"Instance '{args.instance}': its config overrides are applied.")
    bot_conf = config.get("meesho_bot", {})

    api_id_default = bot_conf.get("api_id") or ""
    api_hash_default = bot_conf.get("api_hash") or ""
    session_file = args.session_file or bot_conf.get("session_file", "userbot.session.txt")
    bot_username = (bot_conf.get("bot_username") or "").strip()

    print("=== PRIMES Meesho userbot login ===")
    print("Use a DEDICATED Telegram account for automation.\n")

    api_id = ask("api_id (from my.telegram.org)", str(api_id_default))
    api_hash = ask("api_hash", api_hash_default, secret=True)
    phone = ask("Telegram account phone number (with country code, e.g. +91...)")

    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        print("api_id must be a number.")
        return 1

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        client.connect()
        if not client.is_user_authorized():
            client.send_code_request(phone)
            try:
                code = ask("Login code Telegram sent to this account")
                client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                password = ask("Two-step verification (2FA) password", secret=True)
                client.sign_in(password=password)

        me = client.get_me()
        print(f"\nLogged in as: {me.first_name or ''} @{me.username or ''} (id {me.id})")

        if bot_username:
            try:
                entity = client.get_entity(bot_username)
                print(f"Target bot reachable: {bot_username} (id {entity.id})")
            except Exception as exc:
                print(f"WARNING: could not resolve {bot_username}: {exc}")
                print("Open the bot once from this account (/start) and re-run.")

        session_string = client.session.save()

    with open(session_file, "w", encoding="utf-8") as f:
        f.write(session_string)

    try:
        os.chmod(session_file, 0o600)
    except OSError:
        pass

    print(f"\nSession saved to {session_file}")
    print('Set "enabled": true under "meesho_bot" in config.json, then run main.py.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
