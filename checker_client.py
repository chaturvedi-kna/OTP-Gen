"""
Client for the Meesho registration checker API (Speedz Checker API v2.0.0).

Base URL: https://tubesave.in  (the old superassets.in base URL is dead)
Docs:     https://tubesave.in/docs

Plan rules (v2):
  * Free plan: NO monthly limit, 1 request / 5s / service / key. A personal
    Indian proxy in the Speedz Checker bot's Profile was briefly required,
    but the Sep 2026 update removed that ("No Proxy Needed For API") - if
    the server ever refuses a key with a proxy 403 again, CheckerProxyError
    still explains the fix. Paid plans (Starter / Plus / Pro) raise the
    rate (30 / 45 / 60 per minute per key) plus a monthly quota.
    Over the limit (or quota) the API answers HTTP 429.
  * Responses carry the number's operator (Jio / Airtel / Vi / BSNL) and may
    include plan / validity / expiry / True-5G details; every extra field is
    passed through untouched for the caller to log.

The checker rate limits per API key per service: HTTP 429 with a body like
{"detail": "Rate limit exceeded. Please wait 4.6s for service 'meesho'"}.
Several parallel provider workers validate through this one client, so it is
built to absorb that instead of failing activations:

  1. Retries a check on HTTP 429, honouring the wait the server asks for
     (parsed from the body hint or the Retry-After header) - a rate limit is
     never worth cancelling a paid number over.
  2. Rotates across multiple API keys (config "checker"."api_keys", with the
     legacy single "api_key" still supported); each key has independent
     rate-limit state, so N keys give roughly N times the throughput.
  3. Paces itself per (api_key, service): after a 429 it remembers the
     spacing the server demanded, so later workers wait their turn instead of
     bursting into more 429s.
  4. Retries transient failures (network errors, HTTP 5xx) with a short
     backoff.
  5. All of the above is bounded by a retry budget (max_retries and
     max_retry_wait_seconds); only when the budget is genuinely exhausted
     does it raise, and the caller's cancel path takes over as a last resort.

API keys rejected with 401/403 are retired for the rest of the run; the client
keeps rotating through the remaining ones.

Failures are typed so a caller (see checker_router.py) can tell a temporary
problem from a definitive one:

  * CheckerTimeout      - the API took too long to answer (read/connect timeout)
  * CheckerServerError  - HTTP 5xx
  * CheckerServiceDown  - the API answered {"is_down": true}
  * CheckerUnavailable  - network errors / anything else transient
  * CheckerRateLimited  - retry budget spent under HTTP 429
  * CheckerAuthError    - every API key rejected (401/403)
  * CheckerProxyError   - a Free-plan key has no verified Indian proxy
                          (HTTP 403 mentioning the proxy); add one in the
                          Speedz Checker bot's Profile, or upgrade to a paid
                          plan. A subclass of CheckerAuthError, so "auto"
                          mode falls back to the bot checker for it.
  * CheckerError        - definitive failures (bad request, success=false, ...)
"""

import re
import threading
import time

import requests


class CheckerError(Exception):
    pass


class CheckerUnavailable(CheckerError):
    """The checker service could not answer usefully right now."""


class CheckerRateLimited(CheckerUnavailable):
    """Raised when the retry budget is exhausted under HTTP 429 rate limiting."""


class CheckerTimeout(CheckerUnavailable):
    """
    The API did not answer within the HTTP timeout (connect or read timeout).

    Distinguished from a generic network error so a router can decide that the
    service "takes too long to respond" and switch to another checker (e.g. the
    PRIMES bot) instead of cancelling a paid number.
    """


class CheckerServerError(CheckerUnavailable):
    """HTTP 5xx from the checker API (server-side problem, worth retrying)."""


class CheckerServiceDown(CheckerUnavailable):
    """The checker API answered `is_down: true` for this service."""


class CheckerAuthError(CheckerError):
    """Every configured API key was rejected (HTTP 401/403)."""


class CheckerProxyError(CheckerAuthError):
    """
    A Free-plan key was refused because it has no verified Indian proxy.

    Fix: add your own Indian proxy in the Speedz Checker bot's Profile, or
    upgrade to Starter / Plus / Pro (paid plans use the admin proxy).
    """


# Matches the server hint: "Please wait 4.6s for service 'meesho'"
_RATE_LIMIT_WAIT_RE = re.compile(r"wait(?:ing)?\s+([\d.]+)\s*s", re.IGNORECASE)

# Fallback when a 429 carries no parseable wait hint.
_DEFAULT_RATE_LIMIT_WAIT = 1.0


class CheckerClient:

    def __init__(
        self,
        base_url,
        api_key=None,
        api_keys=None,
        timeout=15,
        max_retries=10,
        max_retry_wait_seconds=45.0,
        min_interval_seconds=5.0,
        rate_limit_buffer_seconds=0.5,
        network_backoff_seconds=1.5,
        log_fn=None
    ):
        base = base_url.strip().rstrip("/")
        if base.endswith("/api/v1/check"):
            self.endpoint_url = base
            root = base[: -len("/api/v1/check")]
        elif base.endswith("/api/v1"):
            self.endpoint_url = f"{base}/check"
            root = base[: -len("/api/v1")]
        else:
            self.endpoint_url = f"{base}/api/v1/check"
            root = base
        # Root for the account/catalog helpers (GET /api/v1/me, ...).
        self.base_url = root or base

        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.max_retry_wait_seconds = float(max_retry_wait_seconds)
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.rate_limit_buffer_seconds = max(0.0, float(rate_limit_buffer_seconds))
        self.network_backoff_seconds = max(0.0, float(network_backoff_seconds))
        self._log_fn = log_fn

        self._keys = self._resolve_keys(api_key, api_keys)
        self._bad_keys = set()
        # (api_key, service) -> {"available_at": monotonic, "pace": seconds}
        self._slots = {}
        self._lock = threading.Lock()

    # -- Setup helpers -------------------------------------------------------

    @staticmethod
    def _resolve_keys(api_key, api_keys):
        """Merge legacy single api_key and the multi-key list, dropping dupes."""
        keys = []
        if isinstance(api_keys, str):
            api_keys = [part.strip() for part in api_keys.split(",")]
        for key in (api_keys or []):
            key = (key or "").strip()
            if key and key not in keys:
                keys.append(key)
        single = (api_key or "").strip()
        if single and single not in keys:
            keys.append(single)
        return keys

    def key_count(self):
        return len(self._keys)

    def usable_key_count(self):
        with self._lock:
            return sum(1 for key in self._keys if key not in self._bad_keys)

    def _log(self, message):
        if self._log_fn:
            try:
                self._log_fn(message)
            except Exception:
                pass

    # -- Rate-limit bookkeeping ----------------------------------------------

    def _slot(self, api_key, service):
        return self._slots.setdefault(
            (api_key, service), {"available_at": 0.0, "pace": 0.0}
        )

    def _acquire(self, service):
        """
        Pick the usable key that becomes free soonest and reserve it.

        Returns (api_key, ready_at_monotonic). The caller must not fire before
        ready_at. Reserving bumps the slot so a concurrent worker picks
        another key (or waits) instead of bursting onto the same one.
        """
        with self._lock:
            usable = [k for k in self._keys if k not in self._bad_keys]
            if not usable:
                raise CheckerError(
                    f"Checker authentication failed: all {len(self._keys)} "
                    "API keys were rejected (HTTP 401/403)"
                )
            best_key = None
            best_ready = None
            for key in usable:
                ready = self._slot(key, service)["available_at"]
                if best_ready is None or ready < best_ready:
                    best_key, best_ready = key, ready
            slot = self._slot(best_key, service)
            now = time.monotonic()
            ready = max(now, best_ready)
            slot["available_at"] = ready + max(slot["pace"], self.min_interval_seconds)
            return best_key, ready

    def _postpone(self, service, api_key, wait_seconds):
        """After a 429: push the slot out and remember the demanded spacing."""
        pause = max(0.0, float(wait_seconds)) + self.rate_limit_buffer_seconds
        with self._lock:
            slot = self._slot(api_key, service)
            slot["pace"] = max(slot["pace"], pause)
            slot["available_at"] = max(
                slot["available_at"], time.monotonic() + pause
            )

    def _disable_key(self, api_key):
        with self._lock:
            self._bad_keys.add(api_key)

    @staticmethod
    def _key_tag(api_key):
        return f"...{api_key[-4:]}" if api_key else "<empty key>"

    # -- HTTP ----------------------------------------------------------------

    @staticmethod
    def format_number(number):
        """
        Normalize phone number for Meesho checker API.
        Strips country codes, leading 0 or +91 prefixes for standard 10-digit Indian numbers.
        """
        cleaned = re.sub(r"\D", "", str(number))
        # If Indian 12-digit number starting with 91, extract 10 digits
        if len(cleaned) == 12 and cleaned.startswith("91"):
            return cleaned[2:]
        # If 11-digit number starting with 0, extract 10 digits
        if len(cleaned) == 11 and cleaned.startswith("0"):
            return cleaned[1:]
        if len(cleaned) >= 10:
            return cleaned[-10:]
        return cleaned

    @staticmethod
    def _parse_retry_after(response):
        """Best-effort wait hint: Retry-After header, else the 429 body text."""
        header = (response.headers or {}).get("Retry-After")
        if header:
            try:
                return max(0.0, float(header))
            except (TypeError, ValueError):
                pass
        match = _RATE_LIMIT_WAIT_RE.search(response.text or "")
        if match:
            try:
                return max(0.0, float(match.group(1)))
            except ValueError:
                pass
        return _DEFAULT_RATE_LIMIT_WAIT

    def check(self, service, number):
        """
        Check one number, absorbing rate limits and transient errors.

        Raises CheckerRateLimited only when the whole retry budget is spent
        under 429s, CheckerUnavailable for persistent network/5xx trouble,
        and CheckerError for definitive failures (bad request, dead keys).
        """
        formatted_number = self.format_number(number)
        deadline = time.monotonic() + self.max_retry_wait_seconds
        attempt = 0
        last_error = None

        while True:
            attempt += 1
            api_key, ready_at = self._acquire(service)

            delay = ready_at - time.monotonic()
            if delay > 0:
                if ready_at > deadline:
                    raise last_error or CheckerRateLimited(
                        f"Rate limited and retry window "
                        f"({self.max_retry_wait_seconds:.0f}s) too small to wait "
                        f"{delay:.1f}s for a free key"
                    )
                self._log(
                    f"Checker: pacing - waiting {delay:.1f}s for key "
                    f"{self._key_tag(api_key)} ({service})"
                )
                time.sleep(delay)

            try:
                response = requests.post(
                    self.endpoint_url,
                    headers={
                        "X-API-Key": api_key,
                        "accept": "application/json",
                        "Content-Type": "application/json"
                    },
                    json={
                        "service": service,
                        "number": formatted_number
                    },
                    timeout=self.timeout
                )
            except requests.Timeout as exc:
                # Read/connect timeout: the service is reachable but too slow.
                # Kept distinct (CheckerTimeout) so a router can fall back to
                # another checker instead of cancelling the number.
                last_error = CheckerTimeout(f"Checker timed out: {exc}")
                if attempt >= self.max_retries or time.monotonic() >= deadline:
                    raise last_error
                self._log(
                    f"Checker timed out ({exc}); retry "
                    f"{attempt}/{self.max_retries} in "
                    f"{self.network_backoff_seconds:.1f}s"
                )
                time.sleep(self.network_backoff_seconds)
                continue
            except requests.RequestException as exc:
                last_error = CheckerUnavailable(f"Checker network error: {exc}")
                if attempt >= self.max_retries or time.monotonic() >= deadline:
                    raise last_error
                self._log(
                    f"Checker network error ({exc}); retry "
                    f"{attempt}/{self.max_retries} in "
                    f"{self.network_backoff_seconds:.1f}s"
                )
                time.sleep(self.network_backoff_seconds)
                continue

            status = response.status_code

            if status == 429:
                wait = self._parse_retry_after(response)
                self._postpone(service, api_key, wait)
                last_error = CheckerRateLimited(
                    f"HTTP 429 rate limited: {response.text[:200]}"
                )
                if (
                    attempt >= self.max_retries
                    or time.monotonic() + wait >= deadline
                ):
                    raise last_error
                self._log(
                    f"Checker rate limited on key {self._key_tag(api_key)}: "
                    f"server asked to wait {wait:.1f}s; retry "
                    f"{attempt}/{self.max_retries}"
                )
                continue

            if status in (401, 403):
                self._disable_key(api_key)
                remaining = self.usable_key_count()
                # v2: a Free-plan key without a verified Indian proxy is
                # refused with HTTP 403 mentioning the proxy. Rotating to
                # another key can still help (it may have one), but when no
                # key is left the message must say what to actually fix.
                needs_proxy = status == 403 and "proxy" in (response.text or "").lower()
                if needs_proxy:
                    detail = (response.text or "").strip()[:200]
                    last_error = CheckerProxyError(
                        f"Checker key {self._key_tag(api_key)} needs a verified "
                        f"Indian proxy (HTTP 403): {detail}. Add your own Indian "
                        f"proxy in the Speedz Checker bot's Profile, or upgrade "
                        f"to Starter / Plus / Pro (paid plans use the admin proxy)."
                    )
                    if remaining == 0:
                        raise CheckerProxyError(
                            f"Checker proxy check failed (HTTP 403): all "
                            f"{len(self._keys)} API key(s) need a verified Indian "
                            f"proxy ({detail}). Free-plan keys only work after you "
                            f"add your own Indian proxy in the Speedz Checker bot's "
                            f"Profile; paid plans skip this."
                        )
                    self._log(
                        f"{last_error} Rotating - {remaining} usable key(s) left."
                    )
                    continue
                if remaining == 0:
                    raise CheckerAuthError(
                        f"Checker authentication failed (HTTP {status}): all "
                        f"{len(self._keys)} API keys invalid or missing"
                    )
                last_error = CheckerError(
                    f"Checker rejected key {self._key_tag(api_key)} "
                    f"(HTTP {status})"
                )
                self._log(
                    f"Checker key {self._key_tag(api_key)} rejected "
                    f"(HTTP {status}); rotating - {remaining} usable key(s) left"
                )
                continue

            if status >= 500:
                last_error = CheckerServerError(
                    f"HTTP {status}: {response.text}"
                )
                if attempt >= self.max_retries or time.monotonic() >= deadline:
                    raise last_error
                self._log(
                    f"Checker server error (HTTP {status}); retry "
                    f"{attempt}/{self.max_retries} in "
                    f"{self.network_backoff_seconds:.1f}s"
                )
                time.sleep(self.network_backoff_seconds)
                continue

            if status >= 400:
                raise CheckerError(
                    f"HTTP {status}: {response.text}"
                )

            try:
                data = response.json()
            except ValueError:
                raise CheckerError(
                    f"Checker returned non-JSON response: {response.text[:200]}"
                )

            if not isinstance(data, dict):
                raise CheckerError(
                    f"Checker returned unexpected format: {data}"
                )

            # Handle service down status
            if data.get("is_down") is True:
                raise CheckerServiceDown(
                    "Checker reports service is_down=true"
                )

            # Handle unsuccessful response. v2 defaults success to true and
            # only requires service/number/is_registered/in_database, so a
            # missing "success" with a verdict present is a GOOD answer -
            # only an explicit success=false (v1 style) is an error.
            if data.get("success") is False:
                msg = (
                    data.get("message") or data.get("error")
                    or data.get("detail") or "Checker returned success=false"
                )
                raise CheckerError(f"Checker error: {msg}")

            # Ensure required field is present
            if "is_registered" not in data:
                msg = (
                    data.get("message") or data.get("error")
                    or data.get("detail") or "Checker returned success=false"
                )
                if data.get("success") is not None and not data.get("success"):
                    raise CheckerError(f"Checker error: {msg}")
                raise CheckerError(
                    f"Checker response missing required 'is_registered' field: {data}"
                )

            return data

    # -- Account / catalog helpers (v2, single-shot, no retry budget) --------

    def _first_usable_key(self, api_key=None):
        """The given key, or the first configured key that is not retired."""
        if (api_key or "").strip():
            return api_key.strip()
        with self._lock:
            for key in self._keys:
                if key not in self._bad_keys:
                    return key
            if self._keys:
                return self._keys[0]
        raise CheckerError("No checker API key configured")

    def _get_json(self, path, api_key=None, auth=True, lax_body=False):
        """GET path under the API root; returns the decoded JSON dict."""
        headers = {"accept": "application/json"}
        if auth:
            headers["X-API-Key"] = self._first_usable_key(api_key)
        try:
            response = requests.get(
                f"{self.base_url}{path}", headers=headers, timeout=self.timeout
            )
        except requests.Timeout as exc:
            raise CheckerTimeout(f"Checker timed out on GET {path}: {exc}")
        except requests.RequestException as exc:
            raise CheckerUnavailable(f"Checker network error on GET {path}: {exc}")
        status = response.status_code
        if status in (401, 403):
            if status == 403 and "proxy" in (response.text or "").lower():
                raise CheckerProxyError(
                    f"GET {path} needs a verified Indian proxy (HTTP 403): "
                    f"{(response.text or '').strip()[:200]}. Add your own Indian "
                    f"proxy in the Speedz Checker bot's Profile, or upgrade to "
                    f"Starter / Plus / Pro."
                )
            raise CheckerAuthError(
                f"GET {path} rejected the API key (HTTP {status}): "
                f"{(response.text or '').strip()[:200]}"
            )
        if status == 429:
            raise CheckerRateLimited(
                f"GET {path} rate limited (HTTP 429): {(response.text or '').strip()[:200]}"
            )
        if status >= 500:
            raise CheckerServerError(
                f"GET {path} failed (HTTP {status}): {(response.text or '').strip()[:200]}"
            )
        if status >= 400:
            raise CheckerError(
                f"GET {path} failed (HTTP {status}): {(response.text or '').strip()[:200]}"
            )
        try:
            data = response.json()
        except ValueError:
            if lax_body:
                # A liveness probe's body is undocumented - plain "OK" counts.
                # HTTP 200 IS the signal, whatever the bytes are.
                return {"ok": True, "raw": (response.text or "")[:200]}
            raise CheckerError(
                f"GET {path} returned non-JSON: {(response.text or '')[:200]}"
            )
        if not isinstance(data, dict):
            if lax_body:
                return {"ok": True, "raw": str(data)[:200]}
            raise CheckerError(f"GET {path} returned unexpected format: {data!r}")
        return data

    def get_me(self, api_key=None):
        """
        GET /api/v1/me: plan, effective rate window, usage, proxy_required.

        Use it to confirm the key works AND whether the Free-plan proxy is
        still missing (proxy_required=true) before a run burns numbers.
        """
        return self._get_json("/api/v1/me", api_key=api_key)

    def list_services(self, api_key=None):
        """GET /api/v1/services: slugs the key may check (paid or proxied)."""
        data = self._get_json("/api/v1/services", api_key=api_key)
        return data.get("services", [])

    def health(self):
        """
        GET /health: liveness probe, no authentication.

        Lenient about the body (undocumented - plain "OK" counts): only the
        transport/status decides. Raises on anything but HTTP 200.
        """
        return self._get_json("/health", auth=False, lax_body=True)

    def health_status(self):
        """
        (up, detail): never raises. up is True only if GET /health answered
        HTTP 200. Anything else - connection refused, timeout, 5xx, wrong
        base URL (404) - means "do not trust this API right now".
        """
        try:
            data = self.health()
        except CheckerError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - a probe must never explode
            return False, f"health probe failed: {exc}"
        return True, str(data)[:200]
