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

Cancellation is IMMEDIATE: a cancel (status 8) is answered with ACCESS_CANCEL
right away - the documentation lists no wait/cooldown variant (unlike
OTPIndia's ACCESS_CANCEL_WAIT). The base client's cancel_window_remaining()
therefore returns 0.0, so the coordinator keeps the configured OTP timeout and
never has to stretch the wait for a cancel window to pass - a number can be
abandoned (and refunded) the moment the timeout fires.
"""

import json

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
        timeout=15
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
        (maxPrice is mandatory for operators 6 & 9). The operator is only sent
        when configured - omitting it lets the provider pick the operator.
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
            return {
                "type": "ACCESS_NUMBER",
                "activation_id": parts[1].strip(),
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
            return {"type": "STATUS_CANCEL"}

        if raw in {"BAD_KEY", "NO_ACTIVATION", "ERROR", "TOO_MANY_REQUESTS"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected getStatus response: {raw}")

    def set_status(self, activation_id, status):
        """
        Status codes (OTPSell):
        3 = request another SMS  -> ACCESS_RETRY_GET
        8 = cancel               -> ACCESS_CANCEL (immediate)

        The cancel is answered with ACCESS_CANCEL straight away - there is no
        wait window, so the base cancel_window_remaining() stays 0.0 and the
        coordinator refunds/abandons a number as soon as it cancels it.
        """
        raw = self._request("setStatus", {
            "id": str(activation_id),
            "status": int(status)
        })

        if raw.startswith("ACCESS_CANCEL"):
            return {"type": "ACCESS_CANCEL"}

        if raw.startswith("ACCESS_RETRY_GET"):
            return {"type": "ACCESS_RETRY_GET"}

        # status 6 (finish) is not documented for OTPSell; tolerate the
        # standard handler_api answer if the provider happens to support it.
        if raw.startswith("ACCESS_ACTIVATION"):
            return {"type": "ACCESS_ACTIVATION"}

        if raw in {"NO_ACTIVATION", "BAD_STATUS", "BAD_ACTION", "BAD_KEY",
                   "ERROR", "TOO_MANY_REQUESTS"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected setStatus response: {raw}")

    def cancel(self, activation_id):
        """Cancel activation (status 8). OTPSell cancels immediately."""
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
        cancel (status 8) moves money back, and that one is immediate.
        """
        res = self.set_status(activation_id, 6)
        if res.get("type") in {"BAD_STATUS", "BAD_ACTION", "ERROR", "NO_ACTIVATION"}:
            return {"type": "FINISH_UNSUPPORTED", "rejected_as": res.get("type")}
        return res

    # -- Catalog endpoints (operator/country/service discovery) ---------------

    def get_operators(self):
        """
        action=getOperators.
        Response: {"Operator 1": "1", "Operator 2": "2", "Any": "any"}
        """
        raw = self._request("getOperators")
        try:
            return json.loads(raw)
        except Exception:
            raise OTPError(f"[{self.name}] Invalid getOperators response: {raw}")

    def get_countries(self):
        """
        action=getCountries.
        Response: {"1": "Ukraine", "91": "India", "21": "USA"}
        """
        raw = self._request("getCountries")
        try:
            return json.loads(raw)
        except Exception:
            raise OTPError(f"[{self.name}] Invalid getCountries response: {raw}")

    def get_services(self):
        """
        action=getServices.
        Response: {"wa": "WhatsApp", "tg": "Telegram", "ig": "Instagram"}
        """
        raw = self._request("getServices")
        try:
            return json.loads(raw)
        except Exception:
            raise OTPError(f"[{self.name}] Invalid getServices response: {raw}")
