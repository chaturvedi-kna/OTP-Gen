# Running in a container (no host-Python surprises)

**Short answer:** the bug that produced the bare `concurrent.futures._base.
TimeoutError` was a *code* bug that only Python ≤ 3.10 exposed — the fix in
`meesho_bot_client.py` (`_is_timeout_error()`) removes it on any interpreter, so
the container is **not required** to run safely. Containerising is still the
better long-term answer to "it works on Termux, it broke on Windows": it pins
Python **and** telethon **and** the asyncio/threading behaviour instead of
hoping every host matches.

| | fix only (current PR) | fix + container |
|---|---|---|
| Python-version class of bugs | fixed for the bugs known today | structurally impossible |
| Telethon version drift | whatever your host has | the pinned set from the image |
| Two machines behave identically | no | yes |
| Editing `config.json` | direct | direct (bind mount) — **no rebuild** |
| Telegram login session | host file | host file (bind mount) |
| `python login_userbot.py` (interactive) | direct | `docker compose run --rm otp python login_userbot.py` |
| Termux Android notifications | work | off inside the container (use Telegram) |
| Extra moving parts | none | Docker Desktop / engine |

**Use both:** merge the fix *and* run the container. The fix is what stops the
paid-number waste; the container is what stops the next version-specific
mystery.

## What the container looks like

`Dockerfile` (pinned `python:3.11-slim`, `pip install -r requirements.txt`) and
`docker-compose.yml`. The compose file **bind-mounts the project directory over
`/app`**, which is what answers your config question:

```
volumes:
  - .:/app
```

* `config.json` is the file on your disk. `/referral <link>`, `/checker <mode>`
  and `--checker-mode` write to it exactly like they do today — no rebuild, no
  `docker cp`, no image baking.
* `userbot.session.txt` is on your disk (chmod 600) — `login_userbot.py` writes
  it into the mounted directory, and keep it out of Git as always.
* `stats*.json`, `state*.json`, `pending_cancels*.json`, `cancel_refused_otp*.
  jsonl` and `.signals-*/` are also host-side, so parallel instances
  (`runtime.py`) and the manual-trigger signal files behave as documented.
* Code changes: `git pull` on the host, restart the container. Only a
  `requirements.txt` change needs `docker compose build`.

Nothing is baked into the image that is worth baking: the image contains the
interpreter + the three dependencies + a copy of the code that the volume
immediately overrides. That also means no secret ever lands in an image layer —
`userbot.session.txt` and the runtime state files are in `.dockerignore`, and
`config.json` (API keys, Telegram token) stays a host file.

## Running it

```bash
# 1. config.json on the host: fill "meesho_bot" (api_id/api_hash/bot_username),
#    providers, checker, telegram - exactly as SETUP_MEESHO_BOT.md describes.

# 2. one-time Telegram login (interactive: type the code / 2FA password)
docker compose build
docker compose run --rm otp python login_userbot.py
#    or, when the session must live in a specific config.json instance:
docker compose run --rm otp python login_userbot.py --instance tempora

# 3. one-shot checks
docker compose run --rm otp python main.py --balance
docker compose run --rm otp python main.py --checker-status

# 4. a real run (one instance)
docker compose run --rm otp python main.py --provider tempora
#    long-running, detached, logs to the host:
docker compose up -d && docker compose logs -f otp
docker compose stop            # SIGTERM -> the normal shutdown path runs
```

Reaching the Telegram command bot (`/status`, `/run`, `/referral`, …) is
unchanged — it is an outbound long poll from the container, no ports to publish
and no inbound access needed.

### Second parallel instance

`config.json` → `instances` already namespaces state/stats/signals per run
(`runtime.py`). Use the commented `otp-vsimpro` service in `docker-compose.yml`
(two containers, one bind mount, separate `--instance`/`--provider`), or a
single process with `--provider all`.

### Windows notes

* Interactive login from PowerShell works with the compose file as written
  (`stdin_open: true` + `tty: true`). If your terminal swallows the prompt, run
  the same command with `winpty` in Git Bash:
  `winpty docker compose run --rm otp python login_userbot.py`.
* The repo can live on `C:\Users\chatu\Desktop\...` — the path in the error
  trace is fine; Docker Desktop shares it by default.
* Do **not** put `.signal` files or write `config.json` from two hosts at once
  while a run is going: the mount is shared, and a half-written config is a
  half-written config (the tool reads it once at startup, so editing between
  runs is what you want).

## Does it change behaviour anywhere?

* **Termux notifications**: unavailable in a container (`shutil.which(
  "termux-notification")` finds nothing) — the notifier already reports
  "Not running inside Termux, so Android notifications are unavailable." Use
  the Telegram notifications (`telegram.enabled` / `bot_token` / `chat_id`),
  which work fine from a container.
* **Timezone**: the container is UTC; log timestamps are UTC today anyway
  (`datetime.now(timezone.utc)` in `main.py`'s `log()`). Provider/cancel
  windows are measured with monotonic clocks, so nothing depends on the host
  timezone.
* **Manual trigger flow**: unchanged — the signal files are on the mounted
  directory, written from the host or through Telegram.
* **Balance/refund guard**: unchanged, it is provider HTTP + local state.
* **CPU/RAM**: the process is I/O bound (Telegram long poll + provider HTTP);
  no special limits needed.

## Verifying the image is what you think it is

```bash
docker compose run --rm otp python -c "import sys, telethon; print(sys.version); print('telethon', telethon.__version__)"
docker compose run --rm otp python main.py --checker-status
docker compose run --rm otp python test_run_watchdog_compat.py   # the regression suite
```
