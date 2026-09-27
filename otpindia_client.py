"""
OTPIndia (otpindia.org) client implementing the SMS-Activate handler_api protocol.

All requests go to https://otpindia.org/api/stubs/handler_api.php via GET with
the API key passed as the api_key query parameter.

Endpoints used by the automation:
- getBalance   -> ACCESS_BALANCE:<amount>
- getNumber    -> ACCESS_NUMBER:<activation_id>:<number> / NO_NUMBERS / NO_BALANCE / ...
                  (OTPIndia-specific: requires the service code AND the server
                   code listed for that service, e.g. service=wa&server=SERVER_CODE)
- getStatus    -> STATUS_WAIT_CODE / STATUS_OK:<sms> / STATUS_CANCEL
- setStatus    -> status 3 = request next SMS, 6 = finish, 8 = cancel
                  (ACCESS_CANCEL / WAIT_CANCEL:<seconds> / EARLY_CANCEL_DENIED)
"""

from base_otp import (
    BaseOTPClient,
    OTPError
)


class OtpIndiaClient(BaseOTPClient):
    """handler_api compliant client for otpindia.org."""

    def __init__(
        self,
        base_url="https://otpindia.org/api/stubs/handler_api.php",
        api_key="",
        default_service="meesho",
        default_server="",
        max_price=None,
        timeout=15
    ):
        super().__init__(
            name="otpindia",
            base_url=base_url,
            api_key=api_key,
            timeout=timeout
        )
        self.default_service = (default_service or "meesho").strip()
        self.default_server = (default_server or "").strip()
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
            "BAD_KEY": "Incorrect or missing API key",
            "BAD_ACTION": "Invalid action requested",
            "USER_BANNED": "Account has been banned",
            "ERROR": "Generic server error",
            "TOO_MANY_REQUESTS": "Rate limit exceeded"
        }

        if raw in known:
            raise OTPError(f"[{self.name}] {raw}: {known[raw]}")

        raise OTPError(f"[{self.name}] Unexpected balance response: {raw}")

    def get_number(self, service=None, server=None, max_price=None, **kwargs):
        """
        Purchase a number.

        OTPIndia's getNumber takes the service code and the server code listed
        for that service (both configured in config.json under "otpindia").
        An optional maxPrice cap is sent only when configured.
        """
        svc = (service or "").strip() or self.default_service
        srv = (server if server is not None else self.default_server)
        srv = (srv or "").strip()

        params = {"service": svc}
        if srv:
            params["server"] = srv

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
            "BAD_KEY",
            "BAD_ACTION",
            "BAD_SERVICE",
            "BAD_SERVER",
            "NO_BALANCE",
            "NO_NUMBERS",
            "PRICE_TOO_HIGH",
            "SERVICE_BANNED",
            "USER_BANNED",
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

        if raw in {"BAD_KEY", "NO_ACTIVATION", "USER_BANNED", "ERROR"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected getStatus response: {raw}")

    def set_status(self, activation_id, status):
        """
        Status codes:
        3 = Request next SMS
        6 = Finish / complete activation (ACCESS_ACTIVATION)
        8 = Cancel activation (ACCESS_CANCEL)
        """
        raw = self._request("setStatus", {
            "id": str(activation_id),
            "status": int(status)
        })

        if raw.startswith("ACCESS_CANCEL") or raw.startswith("ACCESS_ACTIVATION"):
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
                   "BAD_STATUS", "BAD_ACTION", "ERROR"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected setStatus response: {raw}")
