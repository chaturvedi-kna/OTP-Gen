# ---------------------------------------------------------------------------
# Reproducible runtime for the OTP-Gen automation.
#
# Why a container: this tool drives a Telegram USERBOT (Telethon) on an asyncio
# loop plus three SMS providers and a checker API. Running the same code on
# Termux (Python 3.12) and on anaconda/Windows (Python 3.10) is exactly how a
# whole bug class got in: `concurrent.futures.TimeoutError` was NOT the builtin
# `TimeoutError` before Python 3.11, so `_run()`'s first watchdog slice escaped
# as a bare, message-less error and left the flow running (see
# _is_timeout_error() in meesho_bot_client.py). The image pins the interpreter
# AND the three dependencies, so every host behaves the same.
#
# Nothing mutable lives in the image: docker-compose bind-mounts the project
# directory over /app, so config.json, userbot.session.txt, the state/stats
# files, .signals-*/ and the code itself (via `git pull`) change with no
# rebuild. See SETUP_DOCKER.md.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first: this layer is cached while only the code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# `docker compose run --rm <service> python login_userbot.py` for the one-time
# session, `--balance` / `--checker-status` for one-shot checks, plain
# `docker compose up` for a run.
CMD ["python", "main.py"]
