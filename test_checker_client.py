"""
Offline check of the checker client's rate-limit handling (checker_client.py).

Runs without network access by stubbing requests.post, and drives
CheckerClient.check against scripted responses:

    python test_checker_client.py

Verifies:
  * HTTP 429 is retried with the server-honoured wait instead of failing the
    check (so a rate limit never cancels a paid number),
  * multiple api_keys rotate: a rate-limited key is skipped for a free one
    without waiting out the full backoff, and 401/403 keys are retired,
  * the retry budget (max_retries / max_retry_wait_seconds) bounds the wait
    and raises CheckerRateLimited when exhausted,
  * learned pacing: after a 429 the client spaces later checks by the
    demanded interval,
  * transient network errors / HTTP 5xx are retried,
  * the legacy single "api_key" argument still works,
  * definitive failures (success=false, is_down, 4xx, non-JSON) are not
    retried, same as before.
"""

import sys
import time

import checker_client
from checker_client import (
    CheckerClient,
    CheckerError,
    CheckerUnavailable,
    CheckerRateLimited,
)


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


class FakeResponse:
    def __init__(self, status_code=200, body="", headers=None):
        self.status_code = status_code
        self.text = body
        self.headers = headers or {}

    def json(self):
        import json
        return json.loads(self.text)


OK_BODY = '{"success": true, "is_registered": false}'


def ok(is_registered=False):
    return FakeResponse(200, '{"success": true, "is_registered": %s}' % ("true" if is_registered else "false"))


def rate_limited(wait):
    body = '{"detail": "Rate limit exceeded. Please wait %.1fs for service \'meesho\'"}' % wait
    return FakeResponse(429, body)


class ScriptedPoster:
    """Stands in for checker_client.requests.post; pops scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []          # (headers x-api-key, json payload)

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append((headers.get("x-api-key"), json))
        if not self.responses:
            raise AssertionError("ScriptedPoster ran out of responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(keys, **kw):
    kw.setdefault("min_interval_seconds", 0.0)
    kw.setdefault("rate_limit_buffer_seconds", 0.05)
    kw.setdefault("network_backoff_seconds", 0.05)
    kw.setdefault("log_fn", lambda msg: print(f"    [checker] {msg}"))
    if isinstance(keys, (list, tuple)):
        return CheckerClient("https://checker.example", api_keys=list(keys), **kw)
    return CheckerClient("https://checker.example", api_key=keys, **kw)


def test_429_retried_with_server_wait():
    poster = ScriptedPoster([rate_limited(0.2), ok(is_registered=False)])
    checker_client.requests.post = poster
    client = make_client("key-single")

    start = time.monotonic()
    data = client.check("meesho", "919876543210")
    elapsed = time.monotonic() - start

    check("429: check eventually succeeds", data.get("is_registered") is False)
    check("429: two requests made", len(poster.calls) == 2, str(len(poster.calls)))
    check("429: same number retried", poster.calls[1][1] == {"service": "meesho", "number": "9876543210"})
    check("429: honoured the server wait", elapsed >= 0.2, f"{elapsed:.2f}s")


def test_multi_key_rotation_skips_limited_key():
    # key-one is always rate limited for a long time; key-two is free.
    poster = ScriptedPoster([rate_limited(30.0), ok(is_registered=True)])
    checker_client.requests.post = poster
    client = make_client(["key-one", "key-two"])

    start = time.monotonic()
    data = client.check("meesho", "9876543210")
    elapsed = time.monotonic() - start

    check("rotation: succeeds on the second key", data.get("is_registered") is True)
    check("rotation: keys used in order", [c[0] for c in poster.calls] == ["key-one", "key-two"],
          str([c[0] for c in poster.calls]))
    check("rotation: did not wait out the first key's 30s", elapsed < 5.0, f"{elapsed:.2f}s")


def test_retry_budget_exhaustion():
    poster = ScriptedPoster([rate_limited(0.4)] * 50)
    checker_client.requests.post = poster
    client = make_client("key-single", max_retries=50, max_retry_wait_seconds=0.25)

    start = time.monotonic()
    try:
        client.check("meesho", "9876543210")
        check("budget: raises when exhausted", False, "no exception raised")
    except CheckerRateLimited as exc:
        check("budget: raises when exhausted", True)
        check("budget: message mentions 429", "429" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("budget: raises when exhausted", False, repr(exc))
    check("budget: bounded wait", time.monotonic() - start < 2.0)
    check("budget: still a CheckerError (caller cancel path intact)",
          issubclass(CheckerRateLimited, CheckerError) and issubclass(CheckerRateLimited, CheckerUnavailable))


def test_auth_failure_rotates_and_retires_key():
    poster = ScriptedPoster([
        FakeResponse(401, '{"detail": "Invalid API key"}'),
        ok(is_registered=False),
        ok(is_registered=True),
    ])
    checker_client.requests.post = poster
    client = make_client(["bad-key", "good-key"])

    first = client.check("meesho", "9876543210")
    second = client.check("meesho", "9876543211")

    check("auth: first check succeeded on good key", first.get("is_registered") is False)
    check("auth: bad key retired (not reused)", [c[0] for c in poster.calls] == ["bad-key", "good-key", "good-key"],
          str([c[0] for c in poster.calls]))


def test_all_keys_rejected():
    poster = ScriptedPoster([FakeResponse(403, '{"detail": "forbidden"}')] * 5)
    checker_client.requests.post = poster
    client = make_client(["k1", "k2"])

    try:
        client.check("meesho", "9876543210")
        check("all-rejected: raises auth error", False, "no exception raised")
    except CheckerError as exc:
        check("all-rejected: raises auth error", True)
        check("all-rejected: message mentions all keys", "all 2 API keys" in str(exc), str(exc))


def test_5xx_retried():
    poster = ScriptedPoster([FakeResponse(502, "bad gateway"), ok(is_registered=False)])
    checker_client.requests.post = poster
    client = make_client("key-single")

    data = client.check("meesho", "9876543210")
    check("5xx: retried and succeeded", data.get("is_registered") is False)
    check("5xx: two requests made", len(poster.calls) == 2, str(len(poster.calls)))


def test_network_error_retried():
    poster = ScriptedPoster([
        checker_client.requests.ConnectionError("boom"),
        ok(is_registered=False),
    ])
    checker_client.requests.post = poster
    client = make_client("key-single")

    data = client.check("meesho", "9876543210")
    check("network: retried and succeeded", data.get("is_registered") is False)


def test_learned_pacing_gaps_successive_checks():
    poster = ScriptedPoster([rate_limited(0.3), ok(False), ok(True)])
    checker_client.requests.post = poster
    client = make_client("key-single")

    first = client.check("meesho", "9876543210")     # 429 then retry-ok
    start = time.monotonic()
    second = client.check("meesho", "9876543211")    # must pace itself
    elapsed = time.monotonic() - start

    check("pacing: both checks succeed", first.get("is_registered") is False and second.get("is_registered") is True)
    check("pacing: second check waited the learned interval", elapsed >= 0.3, f"{elapsed:.2f}s")
    check("pacing: only one request for the paced check", len(poster.calls) == 3, str(len(poster.calls)))


def test_legacy_single_api_key_argument():
    poster = ScriptedPoster([ok(is_registered=True)])
    checker_client.requests.post = poster
    client = CheckerClient("https://checker.example", api_key="legacy-key", min_interval_seconds=0.0)

    data = client.check("meesho", "9876543210")
    check("legacy: works with single api_key", data.get("is_registered") is True)
    check("legacy: key sent in header", poster.calls[0][0] == "legacy-key")


def test_definitive_failures_not_retried():
    # success=false
    poster = ScriptedPoster([FakeResponse(200, '{"success": false, "message": "bad service"}')])
    checker_client.requests.post = poster
    try:
        make_client("k").check("meesho", "9876543210")
        check("definitive: success=false raises CheckerError", False)
    except CheckerError:
        check("definitive: success=false raises CheckerError", True)
    check("definitive: success=false not retried", len(poster.calls) == 1, str(len(poster.calls)))

    # is_down
    poster = ScriptedPoster([FakeResponse(200, '{"success": true, "is_down": true}')])
    checker_client.requests.post = poster
    try:
        make_client("k").check("meesho", "9876543210")
        check("definitive: is_down raises CheckerUnavailable", False)
    except CheckerUnavailable:
        check("definitive: is_down raises CheckerUnavailable", True)
    check("definitive: is_down not retried", len(poster.calls) == 1, str(len(poster.calls)))

    # other 4xx
    poster = ScriptedPoster([FakeResponse(400, '{"detail": "bad request"}')])
    checker_client.requests.post = poster
    try:
        make_client("k").check("meesho", "9876543210")
        check("definitive: 400 raises CheckerError", False)
    except CheckerError:
        check("definitive: 400 raises CheckerError", True)
    check("definitive: 400 not retried", len(poster.calls) == 1, str(len(poster.calls)))


def test_error_taxonomy():
    # A read timeout is retried, then surfaced as CheckerTimeout (so a router
    # can switch to the bot checker instead of cancelling the number).
    poster = ScriptedPoster([
        checker_client.requests.Timeout("ReadTimeout(15)"),
        ok(is_registered=False),
    ])
    checker_client.requests.post = poster
    client = make_client("k-timeout")
    data = client.check("meesho", "9876543210")
    check("timeout: retried and succeeded", data.get("is_registered") is False)

    poster = ScriptedPoster([checker_client.requests.Timeout("ReadTimeout(15)")])
    checker_client.requests.post = poster
    make_poster_client = CheckerClient(
        "https://checker.example", api_keys=["k-timeout"], max_retries=1,
        min_interval_seconds=0.0, network_backoff_seconds=0.0,
    )
    try:
        make_poster_client.check("meesho", "9876543210")
        check("timeout: raises CheckerTimeout", False)
    except checker_client.CheckerTimeout as exc:
        check("timeout: raises CheckerTimeout", True)
        check("timeout: is a CheckerUnavailable (cancel path unchanged)",
              isinstance(exc, CheckerUnavailable), type(exc).__name__)

    # 5xx -> CheckerServerError, is_down -> CheckerServiceDown, dead keys ->
    # CheckerAuthError: each keeps the old base class, so existing handling
    # still works.
    poster = ScriptedPoster([FakeResponse(503, "unavailable")])
    checker_client.requests.post = poster
    try:
        CheckerClient("https://checker.example", api_keys=["k"], max_retries=1,
                      min_interval_seconds=0.0, network_backoff_seconds=0.0
                      ).check("meesho", "9876543210")
        check("5xx: raises CheckerServerError", False)
    except checker_client.CheckerServerError as exc:
        check("5xx: raises CheckerServerError", isinstance(exc, CheckerUnavailable),
              type(exc).__name__)

    poster = ScriptedPoster([FakeResponse(200, '{"success": true, "is_down": true}')])
    checker_client.requests.post = poster
    try:
        make_client("k").check("meesho", "9876543210")
        check("is_down: raises CheckerServiceDown", False)
    except checker_client.CheckerServiceDown as exc:
        check("is_down: raises CheckerServiceDown", isinstance(exc, CheckerUnavailable),
              type(exc).__name__)

    poster = ScriptedPoster([FakeResponse(401, "unauthorized")])
    checker_client.requests.post = poster
    try:
        make_client("k-revoked").check("meesho", "9876543210")
        check("auth: raises CheckerAuthError", False)
    except checker_client.CheckerAuthError as exc:
        check("auth: raises CheckerAuthError", isinstance(exc, CheckerError),
              type(exc).__name__)


def test_format_number_regression():
    cases = {
        "919876543210": "9876543210",
        "09876543210": "9876543210",
        "9876543210": "9876543210",
        "+91 98765 43210": "9876543210",
        "9580590310": "9580590310",
    }
    for raw, expected in cases.items():
        got = CheckerClient.format_number(raw)
        check(f"format_number({raw!r})", got == expected, got)


def main():
    print("=== Checker client rate-limit handling ===\n")
    test_429_retried_with_server_wait()
    test_multi_key_rotation_skips_limited_key()
    test_retry_budget_exhaustion()
    test_auth_failure_rotates_and_retires_key()
    test_all_keys_rejected()
    test_5xx_retried()
    test_network_error_retried()
    test_learned_pacing_gaps_successive_checks()
    test_legacy_single_api_key_argument()
    test_definitive_failures_not_retried()
    test_error_taxonomy()
    test_format_number_regression()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All checker client checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
