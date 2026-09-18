"""
Mode-aware number checker: HTTP checker API and/or the PRIMES bot checker.

config.json -> "checker" -> "mode" picks the strategy:

  * "api"  - the superassets.in checker API only (the original behaviour).
  * "bot"  - the PRIMES Meesho bot's own number checker only, driven through
             the Telethon userbot (see meesho_bot_client.py). Requires
             meesho_bot to be enabled + configured.
  * "auto" - the API is used while it works; when it is down
             (`is_down: true`), too slow (read/connect timeout), unreachable
             (network / 5xx), has no keys left / rejects every key, the check
             is retried through the PRIMES bot instead of cancelling a paid
             number. Which API failures trigger the fallback is configurable
             (`checker.fallback`), and after a failure the API is skipped for
             a cooldown (doubling up to `max_cooldown_seconds`) so a dead API
             is not re-tried on every single number.

Both paths return the same dict shape {"success": True, "is_registered": bool,
..., "source": "api"|"bot"}, so the coordinator does not care which one
answered. Errors are always CheckerError subclasses (see checker_client.py) -
"auto" only swallows the API failure when the bot path produced a verdict,
otherwise the API error is raised as before, so the existing cancel-and-tally
safety net stays intact.
"""

import time
import threading

from checker_client import (
    CheckerClient,
    CheckerError,
    CheckerUnavailable,
    CheckerRateLimited,
    CheckerTimeout,
    CheckerServerError,
    CheckerServiceDown,
    CheckerAuthError,
)

MODE_API = "api"
MODE_BOT = "bot"
MODE_AUTO = "auto"

# Tolerant aliases, so e.g. "primes", "bot_checker" or "fallback" also work.
MODE_ALIASES = {
    "api": MODE_API, "api_checker": MODE_API, "api-only": MODE_API, "http": MODE_API,
    "superassets": MODE_API, "server": MODE_API,
    "bot": MODE_BOT, "primes": MODE_BOT, "primes_bot": MODE_BOT, "primesbot": MODE_BOT,
    "bot_checker": MODE_BOT, "telegram": MODE_BOT, "userbot": MODE_BOT,
    "auto": MODE_AUTO, "automatic": MODE_AUTO, "fallback": MODE_AUTO,
    "hybrid": MODE_AUTO, "both": MODE_AUTO,
}

# What "auto" does when the API fails. true = use the PRIMES bot for this
# check; false = raise the API error (and the worker cancels the number).
DEFAULT_FALLBACK = {
    "is_down": True,        # API answered {"is_down": true}
    "timeout": True,        # read/connect timeout - "takes too long to respond"
    "network": True,        # connection errors
    "http_5xx": True,       # server-side errors
    "auth": True,           # every API key rejected (401/403)
    "rate_limit": False,    # 429 retry budget spent (opt-in: the API is alive)
    "unknown": False,       # success=false / unexpected payload: not a fallback
    "cooldown_seconds": 60,      # skip the API this long after a fallback
    "max_cooldown_seconds": 600,  # cap for the doubling cooldown
}

_FALLBACK_ALIASES = {
    "is_down": "is_down", "service_down": "is_down", "down": "is_down",
    "timeout": "timeout", "read_timeout": "timeout", "timed_out": "timeout",
    "network": "network", "network_error": "network", "connection": "network",
    "http_5xx": "http_5xx", "5xx": "http_5xx", "server_error": "http_5xx",
    "auth": "auth", "auth_error": "auth", "bad_keys": "auth", "no_keys": "auth",
    "rate_limit": "rate_limit", "429": "rate_limit", "rate_limited": "rate_limit",
    "unknown": "unknown", "other": "unknown",
    "cooldown_seconds": "cooldown_seconds", "cooldown": "cooldown_seconds",
    "max_cooldown_seconds": "max_cooldown_seconds",
}
_FALLBACK_BOOLS = ("is_down", "timeout", "network", "http_5xx", "auth",
                   "rate_limit", "unknown")


def normalize_mode(value, default=MODE_API):
    """Map a config/CLI value onto one of api / bot / auto."""
    if value is None:
        return default
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return MODE_ALIASES.get(text, default)


def classify_api_error(exc):
    """Bucket an API failure, matching the keys of the fallback config."""
    if isinstance(exc, CheckerServiceDown):
        return "is_down"
    if isinstance(exc, CheckerTimeout):
        return "timeout"
    if isinstance(exc, CheckerRateLimited):
        return "rate_limit"
    if isinstance(exc, CheckerAuthError):
        return "auth"
    if isinstance(exc, CheckerServerError):
        return "http_5xx"
    if isinstance(exc, CheckerUnavailable):
        return "network"
    return "unknown"


def resolve_fallback(conf):
    """Normalize the "fallback" config block (dict or bool) into a dict."""
    settings = dict(DEFAULT_FALLBACK)
    raw = conf.get("fallback")
    if raw is None:
        # Flat spelling: "fallback_on_timeout": true, "fallback_cooldown": 30...
        flat = {}
        for key, value in conf.items():
            if key.startswith("fallback_"):
                flat[key[len("fallback_"):]] = value
        raw = flat or None
    if isinstance(raw, bool):
        for key in _FALLBACK_BOOLS:
            settings[key] = raw
    elif isinstance(raw, dict):
        for key, value in raw.items():
            name = str(key).strip().lower()
            canonical = _FALLBACK_ALIASES.get(name)
            if canonical is None and name.startswith("on_"):
                # e.g. "on_timeout": true / "on_read_timeout": true
                canonical = _FALLBACK_ALIASES.get(name[len("on_"):])
            if canonical:
                settings[canonical] = value
    for key in _FALLBACK_BOOLS:
        settings[key] = bool(settings[key])
    try:
        settings["cooldown_seconds"] = max(0.0, float(settings["cooldown_seconds"]))
    except (TypeError, ValueError):
        settings["cooldown_seconds"] = DEFAULT_FALLBACK["cooldown_seconds"]
    try:
        settings["max_cooldown_seconds"] = max(0.0, float(settings["max_cooldown_seconds"]))
    except (TypeError, ValueError):
        settings["max_cooldown_seconds"] = DEFAULT_FALLBACK["max_cooldown_seconds"]
    return settings


class BotChecker:
    """
    Adapter that calls MeeshoBotClient.check_registration and re-raises bot
    failures as CheckerUnavailable, so the caller only sees checker errors.
    """

    def __init__(self, bot_getter, conf=None, log_fn=None):
        self._bot_getter = bot_getter
        self.conf = conf or {}
        self._log_fn = log_fn

    def _log(self, message):
        if self._log_fn:
            try:
                self._log_fn(message)
            except Exception:
                pass

    @property
    def client(self):
        if self._bot_getter is None:
            return None
        try:
            return self._bot_getter()
        except Exception:
            return None

    @property
    def ready(self):
        client = self.client
        return bool(
            client is not None
            and getattr(client, "enabled", False)
            and getattr(client, "ready", False)
            and getattr(client, "bot_username", "")
            and callable(getattr(client, "check_registration", None))
        )

    @property
    def unavailable_reason(self):
        client = self.client
        if client is None:
            return "no PRIMES userbot client configured"
        if not getattr(client, "enabled", False):
            return "meesho_bot.enabled is false"
        if not getattr(client, "ready", False):
            return getattr(client, "start_error", "") or "userbot not connected"
        if not callable(getattr(client, "check_registration", None)):
            return "userbot client does not support number checks"
        return ""

    def check(self, number, service=None):
        if not self.ready:
            raise CheckerUnavailable(
                f"PRIMES bot checker unavailable: {self.unavailable_reason}"
            )
        try:
            data = self.client.check_registration(number)
        except CheckerError:
            raise
        except Exception as exc:
            raise CheckerUnavailable(f"PRIMES bot checker failed: {exc}") from exc
        if not isinstance(data, dict) or "is_registered" not in data:
            raise CheckerUnavailable(
                f"PRIMES bot checker returned an unusable result: {data!r}"
            )
        data.setdefault("source", "bot")
        return data


class CheckerRouter:
    """
    Drop-in replacement for CheckerClient: same check(service, number) API,
    but honours checker.mode (api / bot / auto).
    """

    def __init__(self, config=None, bot_getter=None, log_fn=None, stats=None,
                 api_client=None):
        config = config or {}
        conf = config.get("checker", {}) or {}
        self.conf = conf
        self.mode = normalize_mode(conf.get("mode", MODE_API))
        self.fallback = resolve_fallback(conf)
        self._log_fn = log_fn
        self._stats = stats

        if api_client is not None:
            self.api = api_client
        else:
            self.api = CheckerClient(
                base_url=conf.get("base_url", "https://superassets.in"),
                api_key=conf.get("api_key", ""),
                api_keys=conf.get("api_keys"),
                timeout=conf.get("timeout", 15),
                max_retries=conf.get("max_retries", 10),
                max_retry_wait_seconds=conf.get("max_retry_wait_seconds", 45.0),
                min_interval_seconds=conf.get("min_interval_seconds", 1.0),
                rate_limit_buffer_seconds=conf.get("rate_limit_buffer_seconds", 0.5),
                network_backoff_seconds=conf.get("network_backoff_seconds", 1.5),
                log_fn=log_fn,
            )
        self.bot = BotChecker(bot_getter, conf.get("bot") or {}, log_fn=log_fn)

        # "API is broken right now" state. While it is cooling down, checks go
        # straight to the bot instead of waiting for the API to fail again.
        self._state_lock = threading.Lock()
        self._cooldown = 0.0
        self._down_until = 0.0

    # -- state helpers -------------------------------------------------------

    def _log(self, message):
        if self._log_fn:
            try:
                self._log_fn(message)
            except Exception:
                pass

    def _count(self, name, amount=1):
        if self._stats is not None:
            try:
                self._stats.increment(name, amount)
            except Exception:
                pass

    @property
    def api_ready(self):
        try:
            return self.api.key_count() > 0
        except Exception:
            return False

    @property
    def bot_ready(self):
        return self.bot.ready

    @property
    def mode_wants_bot(self):
        return self.mode in (MODE_BOT, MODE_AUTO)

    @property
    def bot_check_needed(self):
        """
        True when the NEXT number check has to go through the PRIMES bot - i.e.
        when the bot must be sitting on its main menu (check_registration walks
        back to the menu itself, taps the checker button and returns there).

        False when the checker API can answer the check: the login flow may then
        stay where it is, so a failed Change Number does not have to be paid for
        with a main-menu restart and a fresh offer reroll.
        """
        if self.mode == MODE_BOT:
            return True
        if self.mode == MODE_API:
            return False
        # auto: the bot is only needed while the API cannot answer.
        if not self.api_ready:
            return True
        return self.cooldown_remaining() > 0

    @property
    def bot_check_reason(self):
        """Why bot_check_needed is what it is (for logs and notifications)."""
        if self.mode == MODE_BOT:
            return "checker.mode is 'bot' - every check runs through the PRIMES bot"
        if self.mode == MODE_API:
            return "checker.mode is 'api' - the checker API answers every check"
        if not self.api_ready:
            return "no checker API keys configured - checks fall back to the PRIMES bot"
        remaining = self.cooldown_remaining()
        if remaining > 0:
            return (f"the checker API is cooling down ({remaining:.0f}s left) - "
                    f"checks currently go through the PRIMES bot")
        return "the checker API is answering - the PRIMES bot is not needed for checks"

    @property
    def key_count(self):
        try:
            return self.api.key_count()
        except Exception:
            return 0

    @property
    def max_retry_wait_seconds(self):
        return getattr(self.api, "max_retry_wait_seconds", 0.0)

    @property
    def stop_after_failures(self):
        """
        How many consecutive checker failures may cancel numbers before the
        coordinator stops the run (checker.bot.stop_after_failures, 0 = never).
        Only used when the bot is (part of) the checking path.
        """
        try:
            return int((self.conf.get("bot") or {}).get("stop_after_failures", 3))
        except (TypeError, ValueError):
            return 3

    def cooldown_remaining(self):
        with self._state_lock:
            return max(0.0, self._down_until - time.monotonic())

    def _enter_cooldown(self):
        with self._state_lock:
            base = self.fallback["cooldown_seconds"]
            cap = max(base, self.fallback["max_cooldown_seconds"])
            nxt = base if self._cooldown <= 0 else min(cap, self._cooldown * 2)
            self._cooldown = max(0.0, nxt)
            self._down_until = time.monotonic() + self._cooldown
            return self._cooldown

    def _clear_cooldown(self):
        with self._state_lock:
            had = self._cooldown > 0
            self._cooldown = 0.0
            self._down_until = 0.0
        if had:
            self._log("Checker API answered again - back to API-first checking.")

    def describe(self):
        """One-line summary for startup logs and /status."""
        if self.mode == MODE_API:
            return f"Checker API only ({self.key_count} key(s))"
        if self.mode == MODE_BOT:
            state = "ready" if self.bot_ready else f"NOT ready ({self.bot.unavailable_reason})"
            return f"PRIMES bot only ({state})"
        bot_state = "ready" if self.bot_ready else f"NOT ready ({self.bot.unavailable_reason})"
        api_state = f"{self.key_count} key(s)" if self.api_ready else "no API keys"
        return f"AUTO - API first ({api_state}), PRIMES bot fallback ({bot_state})"

    # -- checking ------------------------------------------------------------

    @staticmethod
    def _tag(data, source):
        data["source"] = source
        return data

    def _bot_check(self, number, reason, api_error=None):
        self._log(f"Checker: using the PRIMES bot checker for {number} ({reason})")
        data = self.bot.check(number)
        data["source"] = "bot"
        if api_error is not None:
            data["api_error"] = str(api_error)
        return data

    def check(self, service, number):
        """
        Check one number. Returns the checker dict (with "source" set) or
        raises a CheckerError subclass - exactly like CheckerClient.check.
        """
        if self.mode == MODE_API:
            data = self.api.check(service, number)
            self._count("checker_api_checks")
            return self._tag(data, "api")

        if self.mode == MODE_BOT:
            data = self._bot_check(number, "checker.mode is 'bot'")
            self._count("checker_bot_checks")
            return data

        # ---- auto ----------------------------------------------------------
        if not self.api_ready:
            data = self._bot_check(number, "no checker API keys configured")
            self._count("checker_bot_checks")
            self._count("checker_fallbacks")
            return data

        remaining = self.cooldown_remaining()
        if remaining > 0:
            data = self._bot_check(
                number, f"API cooling down after a previous failure ({remaining:.0f}s left)"
            )
            self._count("checker_bot_checks")
            self._count("checker_fallbacks")
            return data

        try:
            data = self.api.check(service, number)
        except (CheckerUnavailable, CheckerError) as api_exc:
            reason = classify_api_error(api_exc)
            if not self.fallback.get(reason, False):
                raise
            if not self.bot_ready:
                self._log(
                    f"Checker API failed ({reason}: {api_exc}) and the PRIMES bot "
                    f"checker is not available ({self.bot.unavailable_reason}); "
                    f"cancelling the number as before."
                )
                raise
            cooldown = self._enter_cooldown()
            self._log(
                f"Checker API failed ({reason}: {api_exc}); falling back to the "
                f"PRIMES bot checker for this and the next {cooldown:.0f}s."
            )
            self._count("checker_fallbacks")
            try:
                data = self._bot_check(
                    number, f"API error: {reason}", api_error=api_exc
                )
            except CheckerError as bot_exc:
                raise CheckerUnavailable(
                    f"Checker API failed ({api_exc}) and the PRIMES bot checker "
                    f"failed too ({bot_exc})"
                ) from api_exc
            self._count("checker_bot_checks")
            return data

        self._clear_cooldown()
        self._count("checker_api_checks")
        return self._tag(data, "api")
