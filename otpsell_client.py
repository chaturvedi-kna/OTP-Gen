"""
OTPSell (otpsell.com) client implementing the SMS-Activate handler_api protocol.

All requests go to https://otpsell.com/stubs/handler_api.php via GET with the
API key passed as the api_key query parameter.

Endpoints used by the automation (per the published OTPSell API docs):
- getBalance   -> ACCESS_BALANCE:<amount>          (BAD_KEY / ERROR)
- getOperators -> {"Operator 1": "1", ..., "Any": "any"}
                  (BAD_ACTION / TOO_MANY_REQUESTS)
- getCountries -> {"1": "Ukraine", "91": "India", "21": "USA"}
- getServices  -> {"wa": "WhatsApp", "tg": "Telegram", "ig": "Instagram"}
- getNumber    -> ACCESS_NUMBER:<activation_id>:<number>
                  (NO_NUMBERS / NO_BALANCE / BAD_SERVICE); maxPrice is
                  mandatory for operators 6 and 9 and is sent whenever
                  configured.
- getStatus    -> STATUS_OK:<code> / STATUS_WAIT_CODE / STATUS_CANCEL
                  (NO_ACTIVATION for an unknown order id)
- setStatus    -> status 3 = request another SMS (ACCESS_RETRY_GET),
                  8 = cancel (ACCESS_CANCEL); 6 = finish is attempted
                  best-effort only (undocumented) - see finish()

Per-operator cancel windows
---------------------------
Like OTPIndia, OTPSell operators hold a number for a while before a cancel is
honoured - and the length of that window depends on the operator that served
the number (one operator may refund after 60s, another only after 120s). The
window is configured per operator in config.json:

    "otpsell": {
      "operator": "1",                       # or "any", or a rotation pool:
      "operators": ["1", "2"],               # getNumber rotates through them
      "cancel_wait_seconds": {"default": 120, "1": 120, "2": 60, "any": 120}
    }

Because an early cancel buys nothing (the provider keeps the money until the
window has passed), the client itself answers a cancel that arrives inside the
window with the same {"type": "ACCESS_CANCEL_WAIT", "seconds": ...} contract
the coordinator already knows from OTPIndia: the cancellation is deferred to
the background watcher, the worker keeps hunting the next number, the cancel
is retried the moment the window passes, and only then is the refund tally
run. Nothing is paid for and thrown away just because an operator was slow.

Operator rotation pools ("operators": [...]) give fast iterations a larger
number supply: each getNumber is placed with the next operator in the pool,
and the window that then applies to the activation is the one configured for
the operator that actually served it (for operator "any" the provider picks,
so the "any"/"default" window applies).
"""

import json
import math
import threading
import time

from base_otp import BaseOTPClient, OTPError

# Operators whose getNumber requires a maxPrice (per the OTPSell docs).
MAX_PRICE_OPERATORS = {"6", "9"}


def _parse_cancel_windows(spec, fallback=120.0):
    """
    Normalize the cancel_wait_seconds config into {operator: seconds}.

    Accepts a plain number (one window for every operator) or a mapping:
    {"default": 120, "1": 120, "2": 60, "any": 180}. "default" is used when
    the operator that served the activation has no own entry.
    """
    windows = {}
    if isinstance(spec, dict):
        for key, value in spec.items():
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            if seconds < 0:
                continue
            windows[str(key).strip().lower()] = seconds
    else:
        try:
            windows["default"] = max(0.0, float(spec))
        except (TypeError, ValueError):
            windows["default"] = float(fallback)
    if "default" not in windows:
        windows["default"] = float(fallback)
    return windows


class OtpSellClient(BaseOTPClient):
    """handler_api compliant client for otpsell.com."""

    # Capability flag used by the coordinator/cancel watcher: cancels made
    # before this provider's window are routine (defer quietly, retry when the
    # window passes) instead of alert-worthy.
    has_cancel_window = True

    def __init__(
        self,
        base_url="https://otpsell.com/stubs/handler_api.php",
        api_key="",
        default_service="wa",
        default_country="91",
        default_operator="any",
        operators=None,
        max_price=None,
        timeout=15,
        cancel_wait_seconds=120,
        min_request_interval_seconds=0.25,
    ):
        super().__init__(
            name="otpsell",
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
        self.default_service = (default_service or "wa").strip()
        self.default_country = (default_country or "91").strip()
        self.default_operator = (default_operator or "any").strip()
        # Optional rotation pool: getNumber walks it round-robin so parallel
        # fast iterations pull from every operator's inventory, not just one.
        self.operator_pool = [
            str(op).strip() for op in (operators or []) if str(op).strip()
        ]
        self.max_price = max_price
        # Per-operator cancel windows (see module docstring); the scalar
        # cancel_wait_seconds attribute is kept for status/log messages.
        self._cancel_windows = _parse_cancel_windows(cancel_wait_seconds)
        self.cancel_wait_seconds = float(self._cancel_windows["default"])
        self.min_request_interval = max(0.0, float(min_request_interval_seconds or 0.0))
        self._lock = threading.RLock()
        self._acquired_at = {}           # activation_id -> epoch issued
        self._activation_operator = {}   # activation_id -> operator requested
        self._pool_index = 0
        self._last_request_at = 0.0

    # -- request plumbing -----------------------------------------------------

    def _request(self, action, params=None):
        """Polite client-side pacing on top of the base request helper."""
        if self.min_request_interval > 0:
            with self._lock:
                gap = time.time() - self._last_request_at
                if gap < self.min_request_interval:
                    time.sleep(self.min_request_interval - gap)
                self._last_request_at = time.time()
        return super()._request(action, params)

    # -- catalogs -------------------------------------------------------------

    def _catalog(self, action):
        raw = self._request(action)
        known = {
            "BAD_KEY": "Incorrect or missing API key",
            "BAD_ACTION": "Invalid action requested",
            "TOO_MANY_REQUESTS": "Rate limit exceeded",
            "USER_BANNED": "Account has been banned",
            "ERROR": "Generic server error",
        }
        if raw in known:
            raise OTPError(f"[{self.name}] {raw}: {known[raw]}")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise OTPError(f"[{self.name}] Invalid {action} response: {raw}")
        if not isinstance(data, dict):
            raise OTPError(f"[{self.name}] Unexpected {action} payload: {raw}")
        return data

    def get_operators(self):
        """Available network operators, e.g. {"Operator 1": "1", "Any": "any"}."""
        return self._catalog("getOperators")

    def get_countries(self):
        """Available countries, e.g. {"1": "Ukraine", "91": "India"}."""
        return self._catalog("getCountries")

    def get_services(self):
        """Available services, e.g. {"wa": "WhatsApp", "tg": "Telegram"}."""
        return self._catalog("getServices")

    # -- balance --------------------------------------------------------------

    def get_balance(self):
        raw = self._request("getBalance")

        if raw.startswith("ACCESS_BALANCE:"):
            value = raw.split(":", 1)[1].strip()
            try:
                return float(value)
            except ValueError:
                raise OTPError(f"[{self.name}] Invalid balance response: {raw}")

        known = {
            "BAD_KEY": "Incorrect or missing API key",
            "BAD_ACTION": "Invalid action requested",
            "USER_BANNED": "Account has been banned",
            "ERROR": "Generic server error",
            "TOO_MANY_REQUESTS": "Rate limit exceeded",
        }
        if raw in known:
            raise OTPError(f"[{self.name}] {raw}: {known[raw]}")

        raise OTPError(f"[{self.name}] Unexpected balance response: {raw}")

    # -- numbers --------------------------------------------------------------

    def _next_operator(self, operator=None):
        """
        Which operator to ask for on this getNumber.

        A rotation pool ("operators") wins so fast, parallel iterations spread
        across every operator's number inventory; otherwise the explicit
        argument / configured default is used ("any" lets the provider pick).
        """
        with self._lock:
            if self.operator_pool:
                op = self.operator_pool[self._pool_index % len(self.operator_pool)]
                self._pool_index += 1
                return op
        op = operator if operator is not None else self.default_operator
        return (op or "any").strip() or "any"

    def get_number(self, service=None, country=None, operator=None,
                   max_price=None, **kwargs):
        svc = (service or "").strip() or self.default_service
        ctry = (country or "").strip() or self.default_country
        op = self._next_operator(operator)
        approved_price = max_price if max_price is not None else self.max_price

        params = {"service": svc, "country": ctry}
        if op:
            params["operator"] = op
        if approved_price is not None:
            # Mandatory for operators 6 & 9 per the docs; harmless for the
            # rest (the last number under the cap is served first).
            params["maxPrice"] = approved_price
        elif op in MAX_PRICE_OPERATORS:
            raise OTPError(
                f"[{self.name}] operator {op} requires maxPrice - set "
                f"otpsell.max_price in config.json"
            )

        raw = self._request("getNumber", params)

        if raw.startswith("ACCESS_NUMBER:"):
            parts = raw.split(":", 2)
            if len(parts) != 3:
                raise OTPError(f"[{self.name}] Malformed ACCESS_NUMBER: {raw}")
            activation_id = parts[1].strip()
            with self._lock:
                # The cancel window is counted from the moment the number is
                # issued; the window that applies is the serving operator's.
                self._acquired_at[activation_id] = time.time()
                self._activation_operator[activation_id] = op
            return {
                "type": "ACCESS_NUMBER",
                "activation_id": activation_id,
                "number": parts[2].strip(),
                "operator": op,
            }

        known = {
            "NO_NUMBERS",
            "NO_BALANCE",
            "BAD_SERVICE",
            "BAD_COUNTRY",
            "BAD_OPERATOR",
            "WRONG_MAX_PRICE",
            "BAD_KEY",
            "BAD_ACTION",
            "USER_BANNED",
            "SERVICE_BANNED",
            "ERROR",
            "TOO_MANY_REQUESTS",
            "TRY_AGAIN",
        }
        if raw in known:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected getNumber response: {raw}")

    # -- status ---------------------------------------------------------------

    def get_status(self, activation_id):
        raw = self._request("getStatus", {"id": str(activation_id)})

        if raw == "STATUS_WAIT_CODE":
            return {"type": "STATUS_WAIT_CODE"}

        if raw.startswith("STATUS_OK:"):
            sms_text = raw.split(":", 1)[1].strip()
            return {
                "type": "STATUS_OK",
                "sms": sms_text,
                "code": self.extract_code(sms_text),
            }

        if raw == "STATUS_CANCEL":
            self._forget(activation_id)
            return {"type": "STATUS_CANCEL"}

        if raw in {"BAD_KEY", "NO_ACTIVATION", "USER_BANNED", "ERROR",
                   "TOO_MANY_REQUESTS"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected getStatus response: {raw}")

    def set_status(self, activation_id, status):
        """
        Status codes per the OTPSell docs:
        3 = request another SMS (ACCESS_RETRY_GET)
        8 = cancel (ACCESS_CANCEL)

        Status 6 (finish) is the handler_api convention used by the other
        providers; the OTPSell docs only publish 3 and 8, so a finish is
        attempted but never required - see finish().
        """
        raw = self._request("setStatus", {
            "id": str(activation_id),
            "status": int(status),
        })

        # Same "not yet" contract as OTPIndia: the coordinator defers and the
        # watcher retries once the window has passed.
        if raw == "ACCESS_CANCEL_WAIT" or raw.startswith("ACCESS_CANCEL_WAIT:"):
            seconds = None
            if ":" in raw:
                try:
                    seconds = max(1, int(raw.split(":", 1)[1].strip()))
                except ValueError:
                    seconds = None
            return {
                "type": "ACCESS_CANCEL_WAIT",
                "seconds": seconds or max(1, int(math.ceil(
                    self.cancel_window_remaining(activation_id)
                    or self.cancel_wait_seconds_for(activation_id)))),
            }

        if raw.startswith("ACCESS_CANCEL"):
            self._forget(activation_id)
            return {"type": raw.split(":", 1)[0]}

        if raw.startswith("ACCESS_RETRY_GET") or raw.startswith("ACCESS_ACTIVATION"):
            return {"type": raw.split(":", 1)[0]}

        if raw.startswith("WAIT_CANCEL"):
            seconds_val = 120
            if ":" in raw:
                try:
                    seconds_val = int(raw.split(":", 1)[1].strip())
                except ValueError:
                    seconds_val = 120
            return {"type": "WAIT_CANCEL", "seconds": seconds_val}

        if raw in {"EARLY_CANCEL_DENIED", "BAD_KEY", "NO_ACTIVATION",
                   "BAD_STATUS", "BAD_ACTION", "ERROR", "TOO_MANY_REQUESTS"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected setStatus response: {raw}")

    def cancel(self, activation_id):
        """
        Cancel the activation (status 8) - but every operator holds the money
        for its cancel window first.

        A cancel that arrives inside the window is refused WITHOUT a provider
        call and reported as ACCESS_CANCEL_WAIT (the contract the coordinator
        already knows from OTPIndia): the cancellation is deferred, the worker
        keeps hunting, and the watcher retries the moment the window passes.
        Refusing client-side matters here because OTPSell answers an early
        cancel with a plain ERROR (the money then sits in limbo until the
        activation expires) instead of a machine-readable wait time.
        """
        remaining = self.cancel_window_remaining(activation_id)
        if remaining > 0:
            return {
                "type": "ACCESS_CANCEL_WAIT",
                "seconds": max(1, int(math.ceil(remaining))),
                "local": True,
            }
        return self.set_status(activation_id, 8)

    def finish(self, activation_id):
        """
        Complete the activation once the OTP has been processed.

        The docs publish only status 3 (request another SMS) and 8 (cancel),
        so the standard finish (status 6) is attempted but never required: a
        rejection comes back as FINISH_UNSUPPORTED instead of an error. After
        the OTP is consumed the charge stands either way - only a cancel
        (status 8, documented) moves money back.
        """
        res = self.set_status(activation_id, 6)
        if res.get("type") in {"BAD_STATUS", "BAD_ACTION", "ERROR", "NO_ACTIVATION"}:
            return {"type": "FINISH_UNSUPPORTED", "rejected_as": res.get("type")}
        self._forget(activation_id)
        return res

    # -- per-operator cancel windows ------------------------------------------

    def _forget(self, activation_id):
        with self._lock:
            self._acquired_at.pop(str(activation_id), None)
            self._activation_operator.pop(str(activation_id), None)

    def _window_for_operator(self, operator):
        """Cancel window configured for one operator (default when unset)."""
        key = str(operator or "").strip().lower()
        with self._lock:
            windows = dict(self._cancel_windows)
        if key and key in windows:
            return float(windows[key])
        return float(windows["default"])

    def cancel_wait_seconds_for(self, activation_id):
        """
        The cancel window that applies to this activation: the window of the
        operator that served it (recorded at getNumber), or the default window
        when the serving operator is unknown ("any" / issued by another run).
        """
        with self._lock:
            operator = self._activation_operator.get(str(activation_id))
        return self._window_for_operator(operator)

    def cancel_window_remaining(self, activation_id):
        """
        Seconds until this activation may really be cancelled (0.0 = now).

        Counted from the moment getNumber issued the number against the window
        of the operator that served it. The coordinator uses this to keep
        waiting for the OTP until the number can actually be refunded (a
        number abandoned inside its window is paid for and thrown away), and
        cancel() refuses client-side while it has not passed.
        """
        with self._lock:
            acquired = self._acquired_at.get(str(activation_id))
            operator = self._activation_operator.get(str(activation_id))
        if acquired is None:
            return 0.0
        window = self._window_for_operator(operator)
        remaining = window - (time.time() - acquired)
        return max(0.0, float(remaining))
