"""
Base OTP client defining shared abstractions, exceptions, and utility functions
for all SMS-Activate / handler_api compliant OTP providers.
"""

import re
import requests


class OTPError(Exception):
    """Base exception for OTP provider errors."""
    pass


class OTPProviderUnavailable(OTPError):
    """Raised when an OTP provider is unreachable or returns a server error (HTTP 5xx / connection error)."""
    pass


class OTPNoBalance(OTPError):
    """Raised when an OTP provider has insufficient balance."""
    pass


class OTPNoNumbers(OTPError):
    """Raised when no numbers are currently available from the provider."""
    pass


class BaseOTPClient:
    """
    Abstract base client providing standardized request logic and response parsing.
    """

    def __init__(self, name, base_url, api_key, timeout=15):
        self.name = name
        self.base_url = (base_url or "").strip().rstrip("/").rstrip("?")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    def _request(self, action, params=None):
        req_params = {
            "action": action,
            "api_key": self.api_key
        }
        if params:
            for k, v in params.items():
                if v is not None:
                    req_params[k] = v

        try:
            response = requests.get(
                self.base_url,
                params=req_params,
                timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise OTPProviderUnavailable(f"[{self.name}] Network error: {exc}")

        if response.status_code >= 500:
            raise OTPProviderUnavailable(
                f"[{self.name}] Server error (HTTP {response.status_code}): {response.text}"
            )

        # Some providers return HTTP 402/404 for NO_BALANCE/NO_NUMBERS
        if response.status_code == 402:
            raise OTPNoBalance(f"[{self.name}] Insufficient balance (HTTP 402)")

        if response.status_code == 404:
            raise OTPNoNumbers(f"[{self.name}] No numbers available (HTTP 404)")

        if response.status_code >= 400:
            raise OTPError(
                f"[{self.name}] HTTP {response.status_code}: {response.text}"
            )

        return response.text.strip()

    def get_balance(self):
        """Retrieve account balance as float."""
        raise NotImplementedError

    def get_number(self, **kwargs):
        """Request a phone number. Returns dict with type ACCESS_NUMBER, activation_id, and number."""
        raise NotImplementedError

    def get_status(self, activation_id):
        """Retrieve SMS status for given activation ID."""
        raise NotImplementedError

    def set_status(self, activation_id, status):
        """Update activation status."""
        raise NotImplementedError

    def cancel(self, activation_id):
        """Cancel activation to request a refund."""
        return self.set_status(activation_id, 8)

    def finish(self, activation_id):
        """Complete activation once OTP is processed."""
        return self.set_status(activation_id, 6)

    def request_next_sms(self, activation_id):
        """Request next SMS (status 3)."""
        return self.set_status(activation_id, 3)

    @staticmethod
    def extract_code(sms_text):
        """Extract a 4 to 8 digit OTP code from SMS text."""
        if not sms_text:
            return None
        match = re.search(r"\b(\d{4,8})\b", sms_text)
        return match.group(1) if match else sms_text

    @staticmethod
    def balance_difference(before, after):
        return round(after - before, 6)

