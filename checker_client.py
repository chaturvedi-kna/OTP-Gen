import re
import requests


class CheckerError(Exception):
    pass


class CheckerUnavailable(CheckerError):
    pass


class CheckerClient:

    def __init__(
        self,
        base_url,
        api_key,
        timeout=15
    ):
        base = base_url.strip().rstrip("/")
        if base.endswith("/api/v1/check"):
            self.endpoint_url = base
        elif base.endswith("/api/v1"):
            self.endpoint_url = f"{base}/check"
        else:
            self.endpoint_url = f"{base}/api/v1/check"

        self.api_key = api_key
        self.timeout = timeout

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

    def check(self, service, number):
        formatted_number = self.format_number(number)

        try:
            response = requests.post(
                self.endpoint_url,
                headers={
                    "x-api-key": self.api_key,
                    "accept": "application/json",
                    "Content-Type": "application/json"
                },
                json={
                    "service": service,
                    "number": formatted_number
                },
                timeout=self.timeout
            )

        except requests.RequestException as exc:
            raise CheckerUnavailable(f"Checker network error: {exc}")

        if response.status_code >= 500:
            raise CheckerUnavailable(
                f"HTTP {response.status_code}: {response.text}"
            )

        if response.status_code in (401, 403):
            raise CheckerError(
                f"Checker authentication failed (HTTP {response.status_code}): Invalid or missing API key"
            )

        if response.status_code >= 400:
            raise CheckerError(
                f"HTTP {response.status_code}: {response.text}"
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
            raise CheckerUnavailable(
                "Checker reports service is_down=true"
            )

        # Handle unsuccessful response
        if not data.get("success", False):
            msg = data.get("message") or data.get("error") or "Checker returned success=false"
            raise CheckerError(f"Checker error: {msg}")

        # Ensure required field is present
        if "is_registered" not in data:
            raise CheckerError(
                f"Checker response missing required 'is_registered' field: {data}"
            )

        return data