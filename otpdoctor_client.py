"""
OtpDoctor specific client implementing SMS-Activate handler_api protocol.
"""

from base_otp import (
    BaseOTPClient,
    OTPError,
    OTPProviderUnavailable,
    OTPNoBalance,
    OTPNoNumbers
)


class OTPDoctorClient(BaseOTPClient):

    def __init__(self, base_url="https://otpdoctor.in/stubs/handler_api.php", api_key="", timeout=15):
        super().__init__(
            name="otpdoctor",
            base_url=base_url,
            api_key=api_key,
            timeout=timeout
        )

    def get_balance(self):
        raw = self._request("getBalance")

        if raw.startswith("ACCESS_BALANCE:"):
            value = raw.split(":", 1)[1].strip()
            try:
                return float(value)
            except ValueError:
                raise OTPError(f"[{self.name}] Invalid balance response: {raw}")

        if raw == "BAD_KEY":
            raise OTPError(f"[{self.name}] BAD_KEY: Incorrect API key")

        if raw == "ACCOUNT_BLOCKED":
            raise OTPError(f"[{self.name}] ACCOUNT_BLOCKED: Account is disabled")

        raise OTPError(f"[{self.name}] Unexpected balance response: {raw}")

    def get_number(self, service, max_price=None, country=None, **kwargs):
        params = {"service": service}
        if max_price is not None:
            params["maxPrice"] = max_price
        if country is not None:
            params["country"] = country

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
            "BAD_SERVICE",
            "NO_BALANCE",
            "NO_NUMBERS",
            "PRICE_TOO_HIGH",
            "TRY_AGAIN"
        }

        if raw in known:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected getNumber response: {raw}")

    def get_status(self, activation_id):
        raw = self._request("getStatus", {"id": activation_id})

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

        if raw in {"BAD_KEY", "NO_ACTIVATION"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected status response: {raw}")

    def set_status(self, activation_id, status):
        """
        Status codes:
        3 = Request next SMS
        6 = Finish / complete activation (ACCESS_ACTIVATION)
        8 = Cancel activation (STATUS_CANCEL)
        """
        raw = self._request("setStatus", {"id": activation_id, "status": status})

        if raw.startswith("STATUS_CANCEL") or raw.startswith("ACCESS_ACTIVATION") or raw.startswith("ACCESS_CANCEL"):
            return {"type": raw.split(":", 1)[0]}

        if raw.startswith("WAIT_CANCEL"):
            seconds_val = 120
            if ":" in raw:
                try:
                    seconds_val = int(raw.split(":", 1)[1].strip())
                except ValueError:
                    seconds_val = 120
            return {
                "type": "WAIT_CANCEL",
                "seconds": seconds_val
            }

        if raw in {"BAD_KEY", "NO_ACTIVATION", "EARLY_CANCEL_DENIED"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected setStatus response: {raw}")

