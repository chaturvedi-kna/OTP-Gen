"""
OTPSell (otpsell.com) client implementing the SMS-Activate handler_api protocol.

All requests go to https://otpsell.com/stubs/handler_api.php via GET with the
API key passed as the api_key query parameter.

Endpoints used by the automation:
- getBalance   -> ACCESS_BALANCE:<amount>
- getOperators -> JSON map of operator display names -> operator ids
- getCountries -> JSON map of country ids -> display names
- getServices  -> JSON map of service codes -> display names
- getNumber    -> ACCESS_NUMBER:<activation_id>:<number>
                  (service + country required; operator and maxPrice optional -
                   maxPrice is mandatory for operators 6 & 9)
- getStatus    -> STATUS_WAIT_CODE / STATUS_OK:<sms> / STATUS_CANCEL / NO_ACTIVATION
- setStatus    -> status 3 = request another SMS (ACCESS_RETRY_GET),
                  status 8 = cancel (ACCESS_CANCEL)

Two OTPSell quirks the client hides from the rest of the tool:

1) setStatus needs the order's service / country / operator, and they must
   MATCH what getNumber used - otherwise otpsell answers BAD_STATUS. So a
   SPECIFIC operator must be used at getNumber (never "any" or omitted: the
   provider then assigns an operator we cannot see, and the order can never be
   cancelled/refunded). The context is recorded per activation at getNumber and
   replayed on every setStatus (see get_number / set_status).

2) Cancel window (like OTPIndia): a cancel (status 8) that arrives before
   ~cancel_wait_seconds (default 120) have elapsed since getNumber issued the
   number is refused with BAD_STATUS. set_status() translates that into the
   ACCESS_CANCEL_WAIT contract the coordinator understands (carrying the seconds
   left), so the cancel is deferred and retried once the window has passed, and
   the OTP wait is stretched to cover the window (cancel_window_remaining) - a
   number is only abandoned once it can actually be refunded. Set
   cancel_wait_seconds to 0 if the provider ever cancels immediately.
"""

import json
import math
import time

from base_otp import (
    BaseOTPClient,
    OTPError
)


class OtpSellClient(BaseOTPClient):
    """handler_api compliant client for otpsell.com."""

    def __init__(
        self,
        base_url="https://otpsell.com/stubs/handler_api.php",
        api_key="",
        default_service="meesho",
        default_country="91",
        default_operator="",
        max_price=None,
        timeout=15,
        cancel_wait_seconds=120
    ):
        super().__init__(
            name="otpsell",
            base_url=base_url,
            api_key=api_key,
            timeout=timeout
        )
        self.default_service = (default_service or "").strip()
        self.default_country = (default_country or "").strip()
        self.default_operator = (default_operator or "").strip()
        self.max_price = max_price
        # OTPSell refuses to cancel a number until this long after it was
        # issued (~2 minutes by default, same as OTPIndia) - see set_status().
        self.cancel_wait_seconds = float(cancel_wait_seconds or 0)
        # setStatus needs the order's service / country / operator (they must
        # match getNumber), so remember both what getNumber used and WHEN it
        # issued the number, keyed by activation id.
        self._orders = {}
        self._acquired_at = {}

    def get_balance(self):
        raw = self._request("getBalance")

        if raw.startswith("ACCESS_BALANCE:"):
            value = raw.split(":", 1)[1].strip()
            try:
                return float(value)
            except ValueError:
                raise OTPError(f"[{self.name}] Invalid balance response: {raw}")

        known = {
            "BAD_KEY": "Invalid API key",
            "BAD_ACTION": "Invalid action requested",
            "ERROR": "General server error",
            "TOO_MANY_REQUESTS": "Rate limit exceeded"
        }
        if raw in known:
            raise OTPError(f"[{self.name}] {raw}: {known[raw]}")

        raise OTPError(f"[{self.name}] Unexpected balance response: {raw}")

    def get_number(self, service=None, country=None, operator=None, max_price=None, **kwargs):
        """
        Purchase a number.

        service and country are required; operator and maxPrice are optional
        (maxPrice is mandatory for operators 6 & 9). A SPECIFIC operator should
        be configured - if it is omitted otpsell assigns one we cannot see and
        the order can never be cancelled (see set_status / module docstring).
        """
        svc = (service or "").strip() or self.default_service
        ctry = (country if country is not None else self.default_country)
        ctry = (ctry or "").strip()
        op = (operator if operator is not None else self.default_operator)
        op = (op or "").strip()

        if not svc:
            raise OTPError(f"[{self.name}] getNumber requires a service id")
        if not ctry:
            raise OTPError(f"[{self.name}] getNumber requires a country id")

        params = {
            "service": svc,
            "country": ctry
        }
        if op:
            params["operator"] = op

        approved_price = max_price if max_price is not None else self.max_price
        if approved_price is not None:
            params["maxPrice"] = approved_price

        raw = self._request("getNumber", params)

        if raw.startswith("ACCESS_NUMBER:"):
            parts = raw.split(":", 2)
            if len(parts) != 3:
                raise OTPError(f"[{self.name}] Malformed ACCESS_NUMBER: {raw}")
            activation_id = parts[1].strip()
            # Remember the exact service / country / operator this order was
            # created with (setStatus rejects anything else with BAD_STATUS) and
            # when it was issued (the cancel window counts from here).
            self._orders[activation_id] = {
                "service": svc,
                "country": ctry,
                "operator": op,
            }
            self._acquired_at[activation_id] = time.time()
            return {
                "type": "ACCESS_NUMBER",
                "activation_id": activation_id,
                "number": parts[2].strip()
            }

        known = {
            "NO_NUMBERS",
            "NO_BALANCE",
            "BAD_SERVICE",
            "BAD_COUNTRY",
            "BAD_OPERATOR",
            "BAD_KEY",
            "WRONG_MAX_PRICE",
            "ERROR",
            "TOO_MANY_REQUESTS",
            "TRY_AGAIN"
        }
        if raw in known:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected getNumber response: {raw}")

    def get_status(self, activation_id):
        raw = self._request("getStatus", {"id": str(activation_id)})

        if raw == "STATUS_WAIT_CODE":
            return {"type": "STATUS_WAIT_CODE"}

        if raw.startswith("STATUS_OK:"):
            sms_text = raw.split(":", 1)[1].strip()
            return {
                "type": "STATUS_OK",
                "sms": sms_text,
                "code": self.extract_code(sms_text)
            }

        if raw == "STATUS_CANCEL":
            self._acquired_at.pop(str(activation_id), None)
            self._orders.pop(str(activation_id), None)
            return {"type": "STATUS_CANCEL"}

        if raw in {"BAD_KEY", "NO_ACTIVATION", "ERROR", "TOO_MANY_REQUESTS"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected getStatus response: {raw}")

    def set_status(self, activation_id, status):
        """
        Status codes (OTPSell):
        3 = request another SMS  -> ACCESS_RETRY_GET
        8 = cancel               -> ACCESS_CANCEL

        The order's service / country / operator (recorded at getNumber, matched
        exactly) are sent alongside id + status, otherwise otpsell answers
        BAD_STATUS. A cancel that arrives inside the cancel window is also
        answered BAD_STATUS; it is translated into the ACCESS_CANCEL_WAIT
        contract (with the seconds left) so the coordinator defers and retries
        the cancel once the window has passed - exactly like OTPIndia.
        """
        ctx = self._orders.get(str(activation_id), {})
        service = (ctx.get("service") or self.default_service or "").strip()
        country = (ctx.get("country") or self.default_country or "").strip()
        operator = str(ctx.get("operator", self.default_operator) or "").strip()

        params = {
            "id": str(activation_id),
            "status": int(status)
        }
        if service:
            params["service"] = service
        if country:
            params["country"] = country
        if operator:
            params["operator"] = operator

        raw = self._request("setStatus", params)

        if raw.startswith("ACCESS_CANCEL"):
            self._acquired_at.pop(str(activation_id), None)
            self._orders.pop(str(activation_id), None)
            return {"type": "ACCESS_CANCEL"}

        if raw.startswith("ACCESS_RETRY_GET"):
            return {"type": "ACCESS_RETRY_GET"}

        # status 6 (finish) is not documented for OTPSell; tolerate the
        # standard handler_api answer if the provider happens to support it.
        if raw.startswith("ACCESS_ACTIVATION"):
            self._acquired_at.pop(str(activation_id), None)
            self._orders.pop(str(activation_id), None)
            return {"type": "ACCESS_ACTIVATION"}

        if raw == "BAD_STATUS":
            # A cancel before the cancel window has passed is refused with
            # BAD_STATUS. Surface it as ACCESS_CANCEL_WAIT so the coordinator
            # keeps the money held, waits out the window and retries (a plain
            # BAD_STATUS is not a retryable cancel type and would otherwise
            # critical-stop the run over money that is simply still pending).
            if int(status) == 8:
                return {
                    "type": "ACCESS_CANCEL_WAIT",
                    "seconds": self._cancel_wait_seconds(activation_id)
                }
            return {"type": "BAD_STATUS"}

        if raw in {"NO_ACTIVATION", "BAD_ACTION", "BAD_KEY",
                   "ERROR", "TOO_MANY_REQUESTS"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected setStatus response: {raw}")

    def cancel(self, activation_id):
        """Cancel activation (status 8). OTPSell cancels after its ~2 min window."""
        return self.set_status(activation_id, 8)

    def request_next_sms(self, activation_id):
        """Request the next SMS (status 3 -> ACCESS_RETRY_GET)."""
        return self.set_status(activation_id, 3)

    def finish(self, activation_id):
        """
        Complete the activation once the OTP has been processed.

        OTPSell documents only status 3 (request another SMS) and 8 (cancel),
        so the standard finish (status 6) is attempted best-effort and never
        required: a rejection comes back as FINISH_UNSUPPORTED instead of an
        error. After the OTP is consumed the charge stands either way - only a
        cancel (status 8) moves money back, and that one needs the cancel window
        to have passed.
        """
        res = self.set_status(activation_id, 6)
        if res.get("type") in {"BAD_STATUS", "BAD_ACTION", "ERROR", "NO_ACTIVATION"}:
            return {"type": "FINISH_UNSUPPORTED", "rejected_as": res.get("type")}
        return res

    def _cancel_wait_seconds(self, activation_id):
        """
        Seconds to wait before a cancel for this activation is accepted.

        The remainder of the cancel window, counted from the moment getNumber
        issued the number; a full window when the issue time is unknown (a
        pending cancel resumed after a restart).
        """
        acquired = self._acquired_at.get(str(activation_id))
        if acquired is not None:
            remaining = self.cancel_wait_seconds - (time.time() - acquired)
            if remaining > 0:
                return max(1, int(math.ceil(remaining)))
        return max(1, int(self.cancel_wait_seconds)) if self.cancel_wait_seconds else 1

    def cancel_window_remaining(self, activation_id):
        """
        Seconds left until OTPSell accepts a cancel for this activation.

        Counted from the moment getNumber issued the number (cancel_wait_seconds,
        ~2 minutes by default). 0.0 once the window has passed, and 0.0 for an
        activation this client did not issue. The coordinator uses this to keep
        waiting for the OTP until the number can actually be refunded - see
        wait_for_otp() / _otp_wait_timeout() in main.py.
        """
        acquired = self._acquired_at.get(str(activation_id))
        if acquired is None:
            return 0.0
        remaining = self.cancel_wait_seconds - (time.time() - acquired)
        return max(0.0, float(remaining))

    # -- Catalog endpoints (operator/country/service discovery) ---------------

    def get_operators(self):
        """
        action=getOperators.
        Response: {"server-mw3": "server-mw3", ..., "SERVER-62": "server-62", ...}
        (keys are display names, VALUES are the operator ids to send as operator=)
        """
        raw = self._request("getOperators")
        try:
            return json.loads(raw)
        except Exception:
            raise OTPError(f"[{self.name}] Invalid getOperators response: {raw}")

    def get_countries(self):
        """
        action=getCountries.
        Response: {"1": "USA", "91": "india", ...}
        """
        raw = self._request("getCountries")
        try:
            return json.loads(raw)
        except Exception:
            raise OTPError(f"[{self.name}] Invalid getCountries response: {raw}")

    def get_services(self):
        """
        action=getServices.
        Response: {"meesho": "Meesho", "hp": "Meesho", ...}
        """
        raw = self._request("getServices")
        try:
            return json.loads(raw)
        except Exception:
            raise OTPError(f"[{self.name}] Invalid getServices response: {raw}")
