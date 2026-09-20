"""
Offline checks for checker_router.py (checker.mode = api / bot / auto).

No network: the HTTP checker and the PRIMES bot are both replaced by scripted
fakes, so the mode selection, the API->bot fallback, the cooldown and the
error handling are exercised deterministically.

    python test_checker_router.py
"""

import sys
import time
import types

# `requests` may be absent in a bare sandbox; stub it before checker_client
# imports it.
if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        _requests = types.ModuleType("requests")

        class _RequestException(Exception):
            pass

        class _ConnectionError(_RequestException):
            pass

        class _Timeout(_RequestException):
            pass

        def _no_network(*_args, **_kwargs):
            raise RuntimeError("network disabled in this offline check")

        _requests.Session = object
        _requests.RequestException = _RequestException
        _requests.ConnectionError = _ConnectionError
        _requests.Timeout = _Timeout
        _requests.HTTPError = _RequestException
        _requests.get = _no_network
        _requests.post = _no_network
        sys.modules["requests"] = _requests

from checker_client import (
    CheckerError,
    CheckerUnavailable,
    CheckerRateLimited,
    CheckerTimeout,
    CheckerServerError,
    CheckerServiceDown,
    CheckerAuthError,
    CheckerProxyError,
)
from checker_router import (
    CheckerRouter,
    BotChecker,
    MODE_API,
    MODE_AUTO,
    MODE_BOT,
    classify_api_error,
    normalize_mode,
    resolve_fallback,
)


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


class FakeResponse(object):
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        import json
        return json.loads(self.text)


class FakeAPI(object):
    """Scripted stand-in for CheckerClient."""

    def __init__(self, results=None, keys=1):
        self.results = list(results or [])
        self.keys = keys
        self.calls = []

    def key_count(self):
        return self.keys

    max_retry_wait_seconds = 45.0

    def check(self, service, number):
        self.calls.append((service, number))
        if not self.results:
            raise AssertionError("FakeAPI: unexpected extra check")
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)


class HealthFakeAPI(FakeAPI):
    """FakeAPI with a scripted liveness probe."""

    def __init__(self, results=None, keys=1, health=None):
        super().__init__(results=results, keys=keys)
        self.health_results = list(health or [])
        self.health_calls = 0

    def health_status(self):
        self.health_calls += 1
        if not self.health_results:
            raise AssertionError("HealthFakeAPI: unexpected extra probe")
        return self.health_results.pop(0)


class FakeBotClient(object):
    """Stands in for MeeshoBotClient (as checker_router sees it)."""

    def __init__(self, results=None, enabled=True, ready=True, bot_username="@primesbot"):
        self.results = list(results or [])
        self.enabled = enabled
        self.ready = ready
        self.bot_username = bot_username
        self.start_error = None
        self.calls = []

    def check_registration(self, number):
        self.calls.append(number)
        if not self.results:
            raise AssertionError("FakeBotClient: unexpected extra check")
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)


class FakeStats(object):
    def __init__(self):
        self.counters = {}

    def increment(self, name, amount=1):
        self.counters[name] = self.counters.get(name, 0) + amount
        return self.counters[name]


def make_router(mode, api=None, bot=None, fallback=None, keys=1, conf=None):
    checker_conf = dict(conf or {})
    checker_conf["mode"] = mode
    if fallback is not None:
        checker_conf["fallback"] = fallback
    api = api if api is not None else FakeAPI(keys=keys)
    bot_client = bot if bot is not None else FakeBotClient()
    stats = FakeStats()
    router = CheckerRouter(
        {"checker": checker_conf},
        bot_getter=lambda: bot_client,
        log_fn=None,
        stats=stats,
        api_client=api,
    )
    return router, api, bot_client, stats


# ---------------------------------------------------------------------------

def test_mode_aliases():
    check("mode: 'api'", normalize_mode("api") == MODE_API)
    check("mode: 'bot'", normalize_mode("bot") == MODE_BOT)
    check("mode: 'primes' alias", normalize_mode("primes") == MODE_BOT)
    check("mode: 'primes bot' alias", normalize_mode("Primes Bot") == MODE_BOT)
    check("mode: 'auto'", normalize_mode("auto") == MODE_AUTO)
    check("mode: 'fallback' alias", normalize_mode("fallback") == MODE_AUTO)
    check("mode: unknown -> default", normalize_mode("nonsense") == MODE_API)
    check("mode: unknown -> None when asked", normalize_mode("nonsense", default=None) is None)


def test_classify():
    check("classify: is_down", classify_api_error(CheckerServiceDown("x")) == "is_down")
    check("classify: timeout", classify_api_error(CheckerTimeout("x")) == "timeout")
    check("classify: network", classify_api_error(CheckerUnavailable("x")) == "network")
    check("classify: 5xx", classify_api_error(CheckerServerError("x")) == "http_5xx")
    check("classify: 429", classify_api_error(CheckerRateLimited("x")) == "rate_limit")
    check("classify: auth", classify_api_error(CheckerAuthError("x")) == "auth")
    check("classify: proxy 403 counts as auth (bot fallback)",
          classify_api_error(CheckerProxyError("no verified proxy")) == "auth")
    check("classify: other", classify_api_error(CheckerError("success=false")) == "unknown")


def test_fallback_config():
    settings = resolve_fallback({"fallback": {"read_timeout": False, "429": True,
                                              "cooldown": 5}})
    check("fallback: alias read_timeout -> timeout", settings["timeout"] is False)
    check("fallback: alias 429 -> rate_limit", settings["rate_limit"] is True)
    check("fallback: alias cooldown", settings["cooldown_seconds"] == 5.0)
    check("fallback: is_down keeps its default", settings["is_down"] is True)

    flat = resolve_fallback({"fallback_on_timeout": False, "fallback_cooldown": 12})
    check("fallback: flat spelling", flat["timeout"] is False and flat["cooldown_seconds"] == 12.0)

    off = resolve_fallback({"fallback": False})
    check("fallback: false disables every trigger",
          all(off[key] is False for key in
              ("is_down", "timeout", "network", "http_5xx", "auth", "rate_limit", "unknown")))


def test_api_mode():
    router, api, bot, stats = make_router(MODE_API)
    api.results = [{"success": True, "is_registered": False}]
    result = router.check("meesho", "9876543210")
    check("api mode: api used", api.calls == [("meesho", "9876543210")], api.calls)
    check("api mode: bot untouched", bot.calls == [], bot.calls)
    check("api mode: source tagged api", result.get("source") == "api", result)
    check("api mode: api check counted", stats.counters.get("checker_api_checks") == 1, stats.counters)

    router, api, bot, stats = make_router(MODE_API)
    api.results = [CheckerServiceDown("is_down=true")]
    try:
        router.check("meesho", "9876543210")
        check("api mode: is_down propagates", False, "no exception")
    except CheckerServiceDown:
        check("api mode: is_down propagates", True)
    check("api mode: bot never used on is_down", bot.calls == [], bot.calls)


def test_bot_mode():
    router, api, bot, stats = make_router(MODE_BOT, bot=FakeBotClient(
        results=[{"success": True, "is_registered": True}]))
    result = router.check("meesho", "9876543210")
    check("bot mode: bot used", bot.calls == ["9876543210"], bot.calls)
    check("bot mode: api untouched", api.calls == [], api.calls)
    check("bot mode: source tagged bot", result.get("source") == "bot", result)
    check("bot mode: bot check counted", stats.counters.get("checker_bot_checks") == 1, stats.counters)

    router, api, bot, stats = make_router(MODE_BOT, bot=FakeBotClient(enabled=False))
    try:
        router.check("meesho", "9876543210")
        check("bot mode: unavailable bot raises", False, "no exception")
    except CheckerUnavailable as exc:
        check("bot mode: unavailable bot raises", "meesho_bot.enabled is false" in str(exc), str(exc))

    router, api, bot, stats = make_router(MODE_BOT, bot=FakeBotClient(
        results=[RuntimeError("userbot session died")]))
    try:
        router.check("meesho", "9876543210")
        check("bot mode: bot failure -> CheckerUnavailable", False, "no exception")
    except CheckerUnavailable as exc:
        check("bot mode: bot failure -> CheckerUnavailable",
              "userbot session died" in str(exc), str(exc))


def test_auto_api_healthy():
    router, api, bot, stats = make_router(MODE_AUTO)
    api.results = [{"success": True, "is_registered": False}]
    result = router.check("meesho", "9876543210")
    check("auto/healthy: api used", api.calls == [("meesho", "9876543210")], api.calls)
    check("auto/healthy: bot untouched", bot.calls == [], bot.calls)
    check("auto/healthy: source api", result.get("source") == "api", result)
    check("auto/healthy: no fallback counted", stats.counters.get("checker_fallbacks") is None, stats.counters)


def test_auto_fallbacks():
    triggers = [
        ("is_down", CheckerServiceDown("Checker reports service is_down=true")),
        ("read timeout", CheckerTimeout("Checker timed out: ReadTimeout(15)")),
        ("network error", CheckerUnavailable("Checker network error: conn refused")),
        ("http 5xx", CheckerServerError("HTTP 502: bad gateway")),
        ("auth failure", CheckerAuthError("all 1 API keys invalid or missing")),
    ]
    for label, error in triggers:
        router, api, bot, stats = make_router(
            MODE_AUTO, bot=FakeBotClient(results=[{"success": True, "is_registered": False}]))
        api.results = [error]
        result = router.check("meesho", "9876543210")
        check(f"auto: {label} -> bot used", bot.calls == ["9876543210"], bot.calls)
        check(f"auto: {label} -> verdict from bot",
              result.get("source") == "bot" and result.get("is_registered") is False, result)
        check(f"auto: {label} -> api error recorded", "api_error" in result, result)
        check(f"auto: {label} -> fallback counted",
              stats.counters.get("checker_fallbacks") == 1, stats.counters)


def test_auto_no_fallback_cases():
    # A 429 means the API is alive: not a fallback by default.
    router, api, bot, stats = make_router(MODE_AUTO)
    api.results = [CheckerRateLimited("HTTP 429 rate limited")]
    try:
        router.check("meesho", "9876543210")
        check("auto: rate limit does not fall back by default", False, "no exception")
    except CheckerRateLimited:
        check("auto: rate limit does not fall back by default", True)
    check("auto: rate limit leaves the bot alone", bot.calls == [], bot.calls)

    # ... unless configured.
    router, api, bot, stats = make_router(
        MODE_AUTO, fallback={"rate_limit": True},
        bot=FakeBotClient(results=[{"success": True, "is_registered": True}]))
    api.results = [CheckerRateLimited("HTTP 429 rate limited")]
    result = router.check("meesho", "9876543210")
    check("auto: rate limit falls back when enabled",
          result.get("source") == "bot" and bot.calls == ["9876543210"], result)

    # Definitive API errors (success=false, bad request) never fall back.
    router, api, bot, stats = make_router(MODE_AUTO)
    api.results = [CheckerError("Checker error: invalid number")]
    try:
        router.check("meesho", "9876543210")
        check("auto: definitive error propagates", False, "no exception")
    except CheckerError as exc:
        check("auto: definitive error propagates", "invalid number" in str(exc), str(exc))
    check("auto: definitive error leaves the bot alone", bot.calls == [], bot.calls)


def test_auto_bot_unavailable():
    router, api, bot, stats = make_router(MODE_AUTO, bot=FakeBotClient(enabled=False))
    api.results = [CheckerServiceDown("is_down=true")]
    try:
        router.check("meesho", "9876543210")
        check("auto: api error kept when the bot is unavailable", False, "no exception")
    except CheckerServiceDown:
        check("auto: api error kept when the bot is unavailable", True)
    check("auto: failed bot never counted as a check",
          stats.counters.get("checker_bot_checks") is None, stats.counters)

    router, api, bot, stats = make_router(
        MODE_AUTO, bot=FakeBotClient(results=[CheckerUnavailable("bot checker failed")]))
    api.results = [CheckerTimeout("Checker timed out: ReadTimeout(15)")]
    try:
        router.check("meesho", "9876543210")
        check("auto: both checkers failing raises", False, "no exception")
    except CheckerUnavailable as exc:
        check("auto: both checkers failing raises",
              "Checker timed out" in str(exc) and "bot checker failed" in str(exc), str(exc))
        check("auto: both failing keeps the api error as the cause",
              isinstance(exc.__cause__, CheckerTimeout), repr(exc.__cause__))


def test_auto_no_keys():
    router, api, bot, stats = make_router(
        MODE_AUTO, keys=0, bot=FakeBotClient(results=[{"success": True, "is_registered": True}]))
    result = router.check("meesho", "9876543210")
    check("auto: no api keys -> bot", bot.calls == ["9876543210"] and api.calls == [], (api.calls, bot.calls))
    check("auto: no api keys -> source bot", result.get("source") == "bot", result)


def test_auto_cooldown():
    bot_results = [{"success": True, "is_registered": True},
                   {"success": True, "is_registered": True},
                   {"success": True, "is_registered": False}]
    router, api, bot, stats = make_router(MODE_AUTO, bot=FakeBotClient(results=bot_results),
                                          fallback={"cooldown_seconds": 60})
    api.results = [CheckerServiceDown("is_down=true")]
    first = router.check("meesho", "9876543210")
    check("cooldown: first failure falls back", first.get("source") == "bot", first)
    check("cooldown: a cooldown is running", router.cooldown_remaining() > 0, router.cooldown_remaining())

    second = router.check("meesho", "9876543211")
    check("cooldown: next check skips the API", api.calls == [("meesho", "9876543210")], api.calls)
    check("cooldown: next check uses the bot", second.get("source") == "bot" and len(bot.calls) == 2, bot.calls)
    check("cooldown: both fallbacks counted", stats.counters.get("checker_fallbacks") == 2, stats.counters)

    # Once the cooldown expires the API is probed again and, when it answers,
    # checking returns to API-first.
    router._down_until = time.monotonic() - 1
    api.results = [{"success": True, "is_registered": False}]
    third = router.check("meesho", "9876543212")
    check("cooldown: api probed again after expiry", len(api.calls) == 2, api.calls)
    check("cooldown: healthy api used again", third.get("source") == "api", third)
    check("cooldown: cleared after a healthy answer", router.cooldown_remaining() == 0, router.cooldown_remaining())

    # Cooldown 0 = always try the API first, even after a failure.
    router, api, bot, stats = make_router(
        MODE_AUTO, fallback={"cooldown_seconds": 0},
        bot=FakeBotClient(results=[{"success": True, "is_registered": True},
                                   {"success": True, "is_registered": True}]))
    api.results = [CheckerServiceDown("is_down=true"), {"success": True, "is_registered": False}]
    router.check("meesho", "9876543210")
    router.check("meesho", "9876543211")
    check("cooldown 0: api retried on the next check", len(api.calls) == 2, api.calls)


def test_bot_check_needed():
    """
    bot_check_needed tells the coordinator whether the PRIMES bot has to be able
    to reach its main menu for the next number check - which is what decides
    whether a failed Change Number resets the flow there or keeps it in place
    (no menu restart, no offer reroll).
    """
    router, _api, _bot, _stats = make_router(MODE_API)
    check("bot_check_needed: api mode -> False", router.bot_check_needed is False)
    check("bot_check_needed: api mode says the API answers",
          "checker API" in router.bot_check_reason, router.bot_check_reason)

    router, _api, _bot, _stats = make_router(MODE_BOT)
    check("bot_check_needed: bot mode -> True", router.bot_check_needed is True)
    check("bot_check_needed: bot mode says every check uses the bot",
          "PRIMES bot" in router.bot_check_reason, router.bot_check_reason)

    router, _api, _bot, _stats = make_router(MODE_AUTO)
    check("bot_check_needed: auto with a healthy API -> False",
          router.bot_check_needed is False)

    router, _api, _bot, _stats = make_router(MODE_AUTO, keys=0)
    check("bot_check_needed: auto without API keys -> True",
          router.bot_check_needed is True)
    check("bot_check_needed: no-keys reason mentions the keys",
          "keys" in router.bot_check_reason, router.bot_check_reason)

    router, _api, _bot, _stats = make_router(MODE_AUTO)
    router._enter_cooldown()
    check("bot_check_needed: auto while the API cools down -> True",
          router.bot_check_needed is True)
    check("bot_check_needed: cooldown reason mentions the cooldown",
          "cooling down" in router.bot_check_reason, router.bot_check_reason)
    router._clear_cooldown()
    check("bot_check_needed: auto again once the API answers -> False",
          router.bot_check_needed is False)


def test_describe():
    router, _, _, _ = make_router(MODE_API, keys=2)
    check("describe: api", "API only" in router.describe() and "2 key(s)" in router.describe(),
          router.describe())
    router, _, _, _ = make_router(MODE_BOT, bot=FakeBotClient())
    check("describe: bot ready", "ready" in router.describe(), router.describe())
    router, _, _, _ = make_router(MODE_BOT, bot=FakeBotClient(enabled=False))
    check("describe: bot not ready", "NOT ready" in router.describe(), router.describe())
    router, _, _, _ = make_router(MODE_AUTO)
    check("describe: auto", "AUTO" in router.describe() and "fallback" in router.describe(),
          router.describe())


def test_bot_checker_adapter():
    errors = []
    adapter = BotChecker(lambda: FakeBotClient(results=[{"is_registered": True}]), log_fn=errors.append)
    data = adapter.check("9876543210")
    check("BotChecker: tags source", data.get("source") == "bot", data)

    adapter = BotChecker(lambda: FakeBotClient(results=[{"is_registered": True}], ready=False))
    check("BotChecker: not ready", adapter.ready is False and "not connected" in adapter.unavailable_reason,
          adapter.unavailable_reason)

    adapter = BotChecker(lambda: FakeBotClient(results=[{"oops": True}]))
    try:
        adapter.check("9876543210")
        check("BotChecker: unusable result raises", False, "no exception")
    except CheckerUnavailable as exc:
        check("BotChecker: unusable result raises", "unusable result" in str(exc), str(exc))

    adapter = BotChecker(lambda: FakeBotClient(results=[CheckerUnavailable("bot is down")]))
    try:
        adapter.check("9876543210")
        check("BotChecker: checker errors pass through", False, "no exception")
    except CheckerUnavailable as exc:
        check("BotChecker: checker errors pass through", "bot is down" in str(exc), str(exc))


def test_liveness_gate():
    # auto, API down per /health: the API is not asked, the bot answers,
    # and the cooldown starts immediately.
    api = HealthFakeAPI(results=[], health=[(False, "503 down for maintenance")])
    bot = FakeBotClient(results=[{"is_registered": False}])
    router, _, _, stats = make_router(MODE_AUTO, api=api, bot=bot)
    data = router.check("meesho", "9876543210")
    check("gate: down API is not asked", api.calls == [], api.calls)
    check("gate: bot answers instead",
          data.get("source") == "bot" and data.get("is_registered") is False, data)
    check("gate: cooldown entered", router.cooldown_remaining() > 0)
    check("gate: fallback counted", stats.counters.get("checker_fallbacks") == 1,
          stats.counters)

    # auto, ambiguous API answer + fresh probe says down: falls back instead
    # of acting on the garbage answer.
    api = HealthFakeAPI(
        results=[CheckerError("Checker error: bad service")],
        health=[(True, "ok"), (False, "connection refused")],
    )
    bot = FakeBotClient(results=[{"is_registered": True}])
    router, _, _, _ = make_router(MODE_AUTO, api=api, bot=bot)
    data = router.check("meesho", "9876543210")
    check("gate: bad answer + down service falls back to the bot",
          data.get("source") == "bot" and data.get("is_registered") is True, data)
    check("gate: pre-check probe cached, failure re-probed fresh",
          api.health_calls == 2, api.health_calls)

    # auto, ambiguous API answer but the service is up: the answer stands.
    api = HealthFakeAPI(
        results=[CheckerError("Checker error: bad service")],
        health=[(True, "ok"), (True, "still ok")],
    )
    bot = FakeBotClient(results=[{"is_registered": True}])
    router, _, _, _ = make_router(MODE_AUTO, api=api, bot=bot)
    try:
        router.check("meesho", "9876543210")
        check("gate: bad answer while up raises", False, "no exception")
    except CheckerError as exc:
        check("gate: bad answer while up raises",
              type(exc) is CheckerError and "bad service" in str(exc), str(exc)[:100])
    check("gate: bot never asked for a definitive answer", bot.calls == [], bot.calls)

    # api mode: a down API cancels with the liveness reason, without asking.
    api = HealthFakeAPI(results=[], health=[(False, "timeout")])
    router, _, _, _ = make_router(MODE_API, api=api)
    try:
        router.check("meesho", "9876543210")
        check("gate: api mode raises CheckerServiceDown", False, "no exception")
    except CheckerServiceDown as exc:
        check("gate: api mode raises CheckerServiceDown", "liveness" in str(exc),
              str(exc)[:100])
    check("gate: api mode never asked the down API", api.calls == [], api.calls)

    # caching: one probe serves every check inside the TTL.
    api = HealthFakeAPI(
        results=[{"success": True, "is_registered": False},
                 {"success": True, "is_registered": True}],
        health=[(True, "ok")],
    )
    router, _, _, _ = make_router(MODE_AUTO, api=api, conf={"health_cache_seconds": 60})
    router.check("meesho", "9876543210")
    router.check("meesho", "9876543211")
    check("gate: healthy verdict cached across checks", api.health_calls == 1,
          api.health_calls)

    # disabled gate / legacy client without a probe: behaviour unchanged.
    api = HealthFakeAPI(results=[{"success": True, "is_registered": False}], health=[])
    router, _, _, _ = make_router(MODE_AUTO, api=api, conf={"health_check_enabled": False})
    data = router.check("meesho", "9876543210")
    check("gate: disabled gate asks the API directly",
          data.get("is_registered") is False and api.calls != [] and api.health_calls == 0,
          (data, api.health_calls))

    legacy = FakeAPI(results=[{"success": True, "is_registered": True}])
    router, _, _, _ = make_router(MODE_AUTO, api=legacy)
    data = router.check("meesho", "9876543210")
    check("gate: client without a probe still works", data.get("is_registered") is True,
          data)

    # mark_api_down (used by the startup preflight) starts the cooldown.
    router, _, _, _ = make_router(MODE_AUTO)
    check("gate: no cooldown initially", router.cooldown_remaining() == 0)
    router.mark_api_down("preflight")
    check("gate: mark_api_down starts the cooldown", router.cooldown_remaining() > 0)


def main():
    test_mode_aliases()
    test_classify()
    test_fallback_config()
    test_api_mode()
    test_bot_mode()
    test_auto_api_healthy()
    test_auto_fallbacks()
    test_auto_no_fallback_cases()
    test_auto_bot_unavailable()
    test_auto_no_keys()
    test_auto_cooldown()
    test_bot_check_needed()
    test_describe()
    test_bot_checker_adapter()
    test_liveness_gate()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All checker router checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
