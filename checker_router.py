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


class BotBusy(Exception):
    """
    The PRIMES bot conversation is owned by somebody else right now.

    Raised by the coordinator's bot claim when a number check wants the bot
    while a login flow is using it (or is about to). It is turned into a
    CheckerUnavailable by BotChecker, so the number is cancelled with a refund
    instead of being typed into a screen that is waiting for an OTP - the
    classic "number A was waiting for its code and number B got typed into the
    bot" waste.
    """


class CheckerBotBusy(CheckerUnavailable):
    """A checker failure that only means "the bot is busy with a login"."""

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


def _norm_username(value):
    """'@SomeBot', 'SomeBot', ' somebot ' -> 'somebot' ('' for nothing)."""
    return str(value or "").strip().lstrip("@").strip().lower()


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


class _BotBatch:
    """
    One batch window: worker threads join with their number, the first one
    (the leader) closes the window at size/wait, sends ALL numbers in a
    single comma-separated message to the checker bot, and publishes a
    per-number result (the bot's reply order differs from the input order,
    so results are matched by number, never by position).
    """

    def __init__(self, size):
        self.size = size
        self.lock = threading.Lock()
        self.entries = {}   # number -> threading.Event
        self.results = {}   # number -> result dict
        self.error = None   # failure shared by the whole batch
        self.full = False

    def join(self, number):
        with self.lock:
            event = self.entries.get(number)
            leader = not self.entries
            if event is None:
                event = threading.Event()
                self.entries[number] = event
                if len(self.entries) >= self.size:
                    self.full = True
            return event, leader

    def finish(self):
        for event in self.entries.values():
            event.set()

    def result_for(self, number):
        key = str(number)
        if key in self.results:
            return self.results[key]
        if self.error is not None:
            if isinstance(self.error, CheckerError):
                raise self.error
            raise CheckerUnavailable(
                f"Batched checker request failed: {self.error}") from self.error
        raise CheckerUnavailable(
            "The dedicated checker bot's batch reply had no verdict for "
            f"{key}.")


class BotChecker:
    """
    Adapter that calls a Telethon userbot's own number checker.

    `bot_getter` is the PRIMES login bot; `preferred_getter` a DEDICATED
    checker bot's client from config "checker" -> "telegram_bot". The
    dedicated bot is used first (it never touches the PRIMES login
    conversation, so it cannot walk the bot out of a waiting OTP); the PRIMES
    bot is only reached when there is no dedicated checker bot configured or
    it is unavailable - and even then only when no login flow owns it.
    """

    def __init__(self, bot_getter, conf=None, log_fn=None, gate=None, claim=None,
                 preferred_getter=None, preferred_username=None, preferred_name="checker bot"):
        self._bot_getter = bot_getter
        self.conf = conf or {}
        self._log_fn = log_fn
        # `gate` answers (claimed, reason) - "may the shared login bot be used?";
        # `claim` holds it. The dedicated checker bot is its own conversation -
        # it never takes the login claim, so it can run alongside a login.
        self._gate = gate
        self._claim = claim
        self._preferred_getter = preferred_getter
        self._preferred_username = (preferred_username or "").lstrip("@").strip()
        self._preferred_name = (preferred_name or "checker bot").strip()
        # Batch-check bookkeeping (telegram_bot.batch_*): one shared window
        # that several worker threads' checks join so ONE bot visit answers
        # them all (the dedicated bot accepts comma-separated numbers).
        self._batch_lock = threading.Lock()
        self._batch = None  # the currently open _BotBatch, or None

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
    def preferred_client(self):
        if self._preferred_getter is None:
            return None
        try:
            return self._preferred_getter()
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
    def has_preferred(self):
        return bool(self._preferred_getter is not None or self._preferred_username)

    @property
    def preferred_ready(self):
        client = self.preferred_client
        return bool(
            client is not None
            and getattr(client, "ready", False)
            and getattr(client, "check_registration", None)
        )

    @property
    def preferred_unavailable_reason(self):
        if not self.has_preferred:
            return "no dedicated checker bot configured"
        client = self.preferred_client
        if client is None:
            return "the dedicated checker bot client is not configured"
        if not getattr(client, "ready", False):
            return getattr(client, "start_error", "") or "the checker bot userbot is not connected"
        if not callable(getattr(client, "check_registration", None)):
            return "the checker bot userbot does not support number checks"
        return ""

    def describe_preferred(self):
        """One-line summary for logs/status."""
        if not self.has_preferred:
            return ("no dedicated checker bot configured "
                    "(checker.telegram_bot.username)")
        if self.preferred_ready:
            return f"{self._preferred_name} (@{self._preferred_username}) ready"
        return (f"{self._preferred_name} (@{self._preferred_username or '?'}) "
                f"NOT ready ({self.preferred_unavailable_reason})")

    def check_preferred(self, number, service=None):
        """Ask the dedicated checker bot; raises CheckerUnavailable on failure."""
        if not self.preferred_ready:
            raise CheckerUnavailable(
                f"Dedicated checker bot unavailable: {self.preferred_unavailable_reason}"
            )
        try:
            data = self.preferred_client.check_registration(number)
        except CheckerError:
            raise
        except Exception as exc:
            raise CheckerUnavailable(
                f"Dedicated checker bot failed: {exc}") from exc
        if not isinstance(data, dict) or "is_registered" not in data:
            raise CheckerUnavailable(
                f"Dedicated checker bot returned an unusable result: {data!r}")
        data.setdefault("source", "telegram_checker")
        data.setdefault("checker_name", self._preferred_name)
        data.setdefault("checker_username", self._preferred_username)
        return data

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

    def gate_state(self):
        """(allowed, reason) - whether the bot may be used for a check now."""
        if self._gate is None:
            return True, ""
        try:
            allowed, reason = self._gate()
        except Exception as exc:  # a broken gate must not disable the checker
            return True, f"gate error ({exc})"
        return bool(allowed), str(reason or "")

    def _batch_conf(self):
        """(size, wait_seconds) when telegram_bot batching is on, else None."""
        if not bool(self.conf.get("batch_enabled", False)):
            return None
        try:
            size = int(self.conf.get("batch_size", 3))
        except (TypeError, ValueError):
            size = 3
        size = max(2, min(size, 10))
        try:
            wait = float(self.conf.get("batch_wait_seconds", 6.0))
        except (TypeError, ValueError):
            wait = 6.0
        return size, max(1.0, wait)

    def check_preferred_batched(self, number, service=None):
        """
        Ask the dedicated checker bot, batching several workers' pending
        numbers into ONE comma-separated request when telegram_bot.batching
        is enabled. Verdicts come back keyed by number (the bot's reply is
        not ordered), so every participant gets exactly its own result.
        """
        conf = self._batch_conf()
        client = self.preferred_client
        many = getattr(client, "check_registration_many", None)
        if conf is None or client is None or not callable(many):
            return self.check_preferred(number, service=service)

        size, wait = conf
        my_key = str(number)
        with self._batch_lock:
            batch = self._batch
            if batch is None or batch.full:
                batch = _BotBatch(size)
                self._batch = batch
            event, leader = batch.join(my_key)

        if leader:
            deadline = time.monotonic() + wait
            while not batch.full and time.monotonic() < deadline:
                time.sleep(0.05)
            with self._batch_lock:
                batch.full = True  # seal the window
                numbers = list(batch.entries)
            if len(numbers) < 2:
                # Nobody joined: a normal single check costs the same.
                try:
                    data = self.check_preferred(numbers[0], service=service)
                    batch.results[numbers[0]] = data
                except Exception as exc:
                    batch.error = exc
            else:
                self._log(f"Batched checker request: {len(numbers)} numbers "
                          f"in one message to {self._preferred_name}.")
                if not self.preferred_ready:
                    batch.error = CheckerUnavailable(
                        "Dedicated checker bot unavailable: "
                        f"{self.preferred_unavailable_reason}")
                else:
                    try:
                        data = many(numbers)
                        verdicts = (data or {}).get("verdicts") or {}
                        for digits, verdict in verdicts.items():
                            batch.results[str(digits)] = {
                                "success": True,
                                "is_registered": bool(verdict),
                                "source": "telegram_checker",
                                "checker_name": self._preferred_name,
                                "checker_username": self._preferred_username,
                                "number": str(digits),
                                "batched": len(numbers),
                            }
                        missing = [n for n in numbers if n not in batch.results]
                        if missing:
                            batch.error = batch.error or CheckerUnavailable(
                                "The dedicated checker bot's batch reply was "
                                f"missing a verdict for {', '.join(missing)}.")
                    except Exception as exc:
                        batch.error = exc
            batch.finish()
            with self._batch_lock:
                if self._batch is batch:
                    self._batch = None
            return batch.result_for(my_key)

        # Not the leader: wait for the batch leader to publish the verdict.
        margin = 30.0 + wait
        try:
            step_timeout = float(self.conf.get("step_timeout_seconds", 25.0))
        except (TypeError, ValueError):
            step_timeout = 25.0
        ok = event.wait(wait + step_timeout + margin)
        if not ok:
            raise CheckerUnavailable(
                "Batched checker request timed out waiting for the bot's reply.")
        return batch.result_for(my_key)

    def preferred_shares_login(self, client):
        """
        True when the "dedicated" checker bot is really the PRIMES LOGIN bot
        (same @handle configured). Then there is no second conversation, so
        the check must take the login claim like the PRIMES checker does -
        otherwise it taps in the same chat as a running login / offer pre-warm.
        """
        if client is None:
            return False
        try:
            return bool(client.shares_login_conversation)
        except Exception:
            return _norm_username(self._preferred_username) == _norm_username(
                getattr(self.client, "bot_username", ""))

    def _log_conversation_conflict(self):
        """
        Say it out loud when a check and the offer pre-warm/login are about to
        fight over the same PRIMES chat: both drive ONE conversation, so the
        check waits for it (or is skipped) instead of walking the bot away.
        """
        client = self.client
        if client is None:
            return
        holder, since = getattr(client, "conversation_holder", (None, 0.0)) or (None, 0.0)
        if not holder or holder == "check":
            return
        age = max(0.0, time.time() - since) if since else 0.0
        held = f" ({age:.0f}s so far)" if age >= 1 else ""
        self._log(
            f"Checker: the PRIMES bot chat is busy with the {holder} flow{held}. "
            f"A number check and the {holder} drive the SAME conversation, so "
            f"this check waits for it instead of tapping over it (a dedicated "
            f"checker bot - checker.telegram_bot - would avoid the wait).")

    def _is_floodwait(self, exc):
        """True if exc is Telegram FloodWait (same account, both bots share limit)."""
        msg = str(exc).lower()
        return ("wait of" in msg and "seconds is required" in msg) or "floodwait" in msg or ("flood" in msg and "wait" in msg)

    @property
    def fallback_to_primes(self):
        """
        Whether a dedicated checker failure may fall back to PRIMES.
        Config: checker.telegram_bot.fallback_to_primes (bool, default False).
        When False, PRIMES is never used as a fallback if a dedicated bot is
        configured - this prevents the 2622s FloodWait cascade (same account).
        """
        # conf is merged bot + telegram_bot overrides, so check both keys.
        val = self.conf.get("fallback_to_primes")
        if val is None:
            val = self.conf.get("fallback_to_primes_bot", False)
        # Explicit False by default: no PRIMES fallback.
        return bool(val)

    @property
    def floodwait_remaining(self):
        """Seconds left in FloodWait cooldown from either client, or 0."""
        remaining = 0.0
        for getter in (self.preferred_client, self.client):
            try:
                if getter is None:
                    continue
                # MeeshoBotClient exposes floodwait_remaining() / _floodwait_until
                fn = getattr(getter, "floodwait_remaining", None)
                if callable(fn):
                    r = fn()
                    remaining = max(remaining, float(r or 0))
                else:
                    until = getattr(getter, "_floodwait_until", 0)
                    if until:
                        import time as _time
                        r = max(0.0, until - _time.time())
                        remaining = max(remaining, r)
            except Exception:
                continue
        return remaining

    def check(self, number, service=None):
        # Dedicated checker bot first: it has its own conversation and never
        # touches the PRIMES login flow, so a mid-flight OTP is never at risk
        # (unless it is configured to BE the login bot - then there is no
        # second conversation and the claim below has to guard it).
        has_dedicated = self.has_preferred
        use_primes_fallback = self.fallback_to_primes

        if self.preferred_ready and not self.preferred_shares_login(self.preferred_client):
            try:
                return self.check_preferred_batched(number, service=service)
            except CheckerError as exc:
                if self._is_floodwait(exc):
                    self._log(
                        f"Dedicated checker bot hit FloodWait ({exc}); NOT trying "
                        f"PRIMES bot as fallback (same Telegram account shares the limit) - "
                        f"cancelling with refund.")
                    raise
                if not use_primes_fallback and has_dedicated:
                    self._log(
                        f"Dedicated checker bot failed ({exc}); PRIMES fallback is DISABLED "
                        f"(checker.telegram_bot.fallback_to_primes=false) - cancelling with refund, "
                        f"no PRIMES attempt.")
                    raise
                # Fall through: try PRIMES as the last resort when allowed.
                self._log(
                    f"Dedicated checker bot failed to answer ({exc}); "
                    f"trying the PRIMES bot as the last resort.")
            except Exception as exc:
                if self._is_floodwait(exc):
                    self._log(
                        f"Dedicated checker bot hit FloodWait ({exc}); NOT trying "
                        f"PRIMES bot as fallback (same account) - cancelling with refund.")
                    raise CheckerUnavailable(
                        f"Dedicated checker bot FloodWait: {exc}") from exc
                if not use_primes_fallback and has_dedicated:
                    self._log(
                        f"Dedicated checker bot failed ({exc}); PRIMES fallback is DISABLED "
                        f"(checker.telegram_bot.fallback_to_primes=false) - cancelling with refund.")
                    raise CheckerUnavailable(
                        f"Dedicated checker bot failed: {exc}") from exc
                self._log(
                    f"Dedicated checker bot failed ({exc}); "
                    f"trying the PRIMES bot as the last resort.")

        # If a dedicated bot is configured as a SEPARATE conversation and
        # PRIMES fallback is disabled, never use PRIMES - even when dedicated
        # is not ready. If the dedicated bot IS the login bot (misconfig, same
        # handle), it shares the login conversation and must still go through
        # the gate/claim path so a busy login is reported as CheckerBotBusy.
        if has_dedicated and not use_primes_fallback:
            pref = self.preferred_client
            shares = False
            try:
                shares = self.preferred_shares_login(pref) if pref is not None else False
            except Exception:
                shares = False
            if not shares:
                reason = self.preferred_unavailable_reason or self.unavailable_reason
                raise CheckerUnavailable(
                    f"Dedicated checker bot unavailable and PRIMES fallback is disabled "
                    f"(checker.telegram_bot.fallback_to_primes=false): {reason}. "
                    f"Enable fallback_to_primes or fix the dedicated bot."
                )

        if not self.ready:
            raise CheckerUnavailable(
                f"PRIMES bot checker unavailable: {self.unavailable_reason}"
            )
        allowed, reason = self.gate_state()
        if not allowed:
            raise CheckerBotBusy(
                f"PRIMES bot checker is not available for this check: {reason}"
            )
        self._log_conversation_conflict()
        if self._claim is None:
            return self._run_check(number)
        try:
            with self._claim():
                return self._run_check(number)
        except BotBusy as exc:
            raise CheckerBotBusy(str(exc)) from exc

    def _run_check(self, number):
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
                 api_client=None, gate=None, claim=None,
                 checker_bot_getter=None, checker_bot_username=None) :
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
        tg_config = (conf.get("telegram_bot") or {})
        username = tg_config.get("username") or tg_config.get("bot_username") or checker_bot_username
        username = (username or "") if isinstance(username, str) else ""

        # The dedicated checker bot gets its own conf (a union of checker.bot
        # defaults + checker.telegram_bot overrides), so its screen wording can
        # differ from the PRIMES login bot without touching the login hints.
        check_conf = dict(conf.get("bot") or {})
        overrides = {k: v for k, v in tg_config.items() if k not in ("username", "bot_username", "name")}
        check_conf.update(overrides)

        self.bot = BotChecker(
            bot_getter, check_conf, log_fn=log_fn,
            gate=gate, claim=claim,
            preferred_getter=checker_bot_getter,
            preferred_username=username,
            preferred_name=tg_config.get("name") or "checker bot",
        )

        # "API is broken right now" state. While it is cooling down, checks go
        # straight to the bot instead of waiting for the API to fail again.
        self._state_lock = threading.Lock()
        self._cooldown = 0.0
        self._down_until = 0.0

        # Bot checker FloodWait cooldown (self-heal mode). Same account drives
        # both PRIMES and dedicated checker, so FloodWait applies to both.
        self._bot_cooldown = 0.0
        self._bot_down_until = 0.0

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

    # -- bot checker FloodWait cooldown (self-heal) --------------------------

    def bot_floodwait_remaining(self):
        """Seconds left in bot FloodWait cooldown (from BotChecker or local)."""
        # Local bot cooldown (set via _enter_bot_floodwait)
        with self._state_lock:
            local = max(0.0, self._bot_down_until - time.monotonic())
        # Plus MeeshoBotClient's own _floodwait_until tracking
        try:
            client_remaining = float(self.bot.floodwait_remaining or 0)
        except Exception:
            client_remaining = 0.0
        return max(local, client_remaining)

    def _enter_bot_floodwait(self, seconds):
        """Enter bot FloodWait cooldown for `seconds` (self-heal pause)."""
        seconds = max(0.0, float(seconds or 0))
        if seconds <= 0:
            return 0.0
        with self._state_lock:
            # Use the larger of existing and new, so we don't shorten a 2622s wait.
            self._bot_cooldown = max(self._bot_cooldown, seconds)
            self._bot_down_until = max(self._bot_down_until, time.monotonic() + seconds)
            return self._bot_cooldown

    def _clear_bot_floodwait(self):
        with self._state_lock:
            had = self._bot_down_until > 0
            self._bot_cooldown = 0.0
            self._bot_down_until = 0.0
        if had:
            self._log("Bot checker FloodWait cooldown cleared - resuming checks.")

    @property
    def self_heal_enabled(self):
        """Whether self-heal mode is on (auto-pause on FloodWait and resume)."""
        # Check telegram_bot config first, then bot config, then top-level checker.
        tg = self.conf.get("telegram_bot") or {}
        for src in (tg, self.conf.get("bot") or {}, self.conf):
            if "self_heal_enabled" in src:
                return bool(src["self_heal_enabled"])
            if "self_heal" in src:
                return bool(src["self_heal"])
        return True  # default on

    @property
    def self_heal_max_wait(self):
        """Max seconds to pause for self-heal (0 = wait full FloodWait)."""
        tg = self.conf.get("telegram_bot") or {}
        for src in (tg, self.conf.get("bot") or {}, self.conf):
            for key in ("self_heal_max_wait_seconds", "self_heal_max_wait", "max_self_heal_wait"):
                if key in src:
                    try:
                        return max(0.0, float(src[key]))
                    except (TypeError, ValueError):
                        continue
        return 3600.0  # default 1h cap, so a 2622s wait is honored but not infinite

    @property
    def fallback_to_primes(self):
        """Whether PRIMES fallback is allowed when dedicated fails (default False)."""
        tg = self.conf.get("telegram_bot") or {}
        # Check telegram_bot first, then bot, then top-level.
        for src in (tg, self.conf.get("bot") or {}, self.conf):
            if "fallback_to_primes" in src:
                return bool(src["fallback_to_primes"])
        return False  # default: NO PRIMES fallback

    def describe(self):
        """One-line summary for startup logs and /status."""
        if self.mode == MODE_API:
            return f"Checker API only ({self.key_count} key(s))"
        if self.mode == MODE_BOT:
            state = "ready" if self.bot_ready else f"NOT ready ({self.bot.unavailable_reason})"
            fb = "with PRIMES fallback" if self.fallback_to_primes else "NO PRIMES fallback"
            heal = "self-heal ON" if self.self_heal_enabled else "self-heal OFF"
            return f"PRIMES bot only ({state}, {fb}, {heal})"
        bot_state = "ready" if self.bot_ready else f"NOT ready ({self.bot.unavailable_reason})"
        api_state = f"{self.key_count} key(s)" if self.api_ready else "no API keys"
        dedicated = self.bot.describe_preferred()
        fb = "PRIMES fallback ON" if self.fallback_to_primes else "PRIMES fallback OFF"
        heal = "self-heal ON" if self.self_heal_enabled else "self-heal OFF"
        return (f"AUTO - API first ({api_state}), dedicated checker bot "
                f"({dedicated}), {fb}, {heal}, PRIMES bot ({bot_state})")

    # -- checking ------------------------------------------------------------

    @staticmethod
    def _tag(data, source):
        data["source"] = source
        return data

    def _bot_check(self, number, reason, api_error=None):
        self._log(self._bot_check_message(number, reason))
        data = self.bot.check(number)
        data["source"] = "bot"
        if data.get("checker_username"):
            # The dedicated checker bot answered in its own conversation.
            self._count("checker_dedicated_checks")
        if api_error is not None:
            data["api_error"] = str(api_error)
        return data

    @property
    def bot_checker_name(self):
        """
        Which bot answers a bot check right now: "the dedicated checker bot
        @x" (checker.telegram_bot) or "the PRIMES bot checker".
        """
        if self.bot.preferred_ready and not self.bot.preferred_shares_login(
                self.bot.preferred_client):
            return f"the dedicated checker bot @{self.bot._preferred_username or '?'}"
        return "the PRIMES bot checker"

    def _bot_check_message(self, number, reason):
        """
        "who is answering this check and why" - a bare "using the PRIMES bot
        checker" hid that the DEDICATED checker bot was the one answering
        (and, before the entity fix, that it was answering in the PRIMES
        conversation). Say which bot, and - when the dedicated one is
        configured but unusable - why.
        """
        why = ""
        if (self.bot.has_preferred
                and self.bot_checker_name == "the PRIMES bot checker"):
            why = (f" - the dedicated checker bot "
                   f"@{self.bot._preferred_username or '?'} cannot answer "
                   f"({self.bot.preferred_unavailable_reason})")
        return (f"Checker: using {self.bot_checker_name} for {number} "
                f"({reason}){why}")

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
                    f"Checker API failed ({reason}: {api_exc}) and the bot "
                    f"checker is not available "
                    f"({self.bot.unavailable_reason}); cancelling the number "
                    f"as before."
                )
                raise
            cooldown = self._enter_cooldown()
            self._log(
                f"Checker API failed ({reason}: {api_exc}); falling back to "
                f"{self.bot_checker_name} for this and the next "
                f"{cooldown:.0f}s."
            )
            self._count("checker_fallbacks")
            try:
                data = self._bot_check(
                    number, f"API error: {reason}", api_error=api_exc
                )
            except CheckerBotBusy:
                # The bot is mid-login: report it as such (the worker cancels
                # this number with a refund rather than counting it as a
                # broken checker).
                raise
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
