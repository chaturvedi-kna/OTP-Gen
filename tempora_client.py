"""
TemporaSMS client implementing SMS-Activate handler_api protocol for TemporaSMS.
Features automatic service code resolution for numeric operators via getServices.
"""

import json
from base_otp import (
    BaseOTPClient,
    OTPError,
    OTPProviderUnavailable,
    OTPNoBalance,
    OTPNoNumbers
)


class TemporaClient(BaseOTPClient):

    def __init__(
        self,
        base_url="https://api.temporasms.com/stubs/handler_api.php",
        api_key="",
        default_service="meesho",
        default_country="22",
        default_operator="auto",
        max_price=None,
        timeout=15,
        operator_services=None
    ):
        super().__init__(
            name="tempora",
            base_url=base_url,
            api_key=api_key,
            timeout=timeout
        )
        self.default_service = default_service or "meesho"
        self.default_country = default_country or "22"
        self.default_operator = default_operator or "auto"
        self.max_price = max_price
        self.operator_services = operator_services or {}
        self._service_code_cache = {}

    def get_services_for_operator(self, operator):
        """
        Query action=getServices&operator=$operator to fetch the dictionary of
        service_code -> service_name for that operator.
        """
        raw = self._request("getServices", {"operator": operator})
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def resolve_service_code(self, service_name, operator=None):
        """
        Resolves the appropriate service code for the given operator.
        - For numeric operators (e.g. '1', '2', '3', '4'), TemporaSMS maps services
          to specific 3-letter codes (e.g. op 4 -> 'mfo' for Meesho).
        - For non-numeric operators ('auto', 'best', 'cheap'), the global name ('meesho') is used.
        - Results are cached to avoid querying repeatedly during runs.
        """
        op = str(operator if operator is not None else self.default_operator).strip()
        svc = (service_name or self.default_service).strip()

        # Check manual mapping first
        if op in self.operator_services and svc in self.operator_services[op]:
            return self.operator_services[op][svc]

        # Non-numeric operators use the service name directly (e.g. 'meesho')
        if not op.isdigit():
            return svc

        # Check cache
        cache_key = f"{op}:{svc.lower()}"
        if cache_key in self._service_code_cache:
            return self._service_code_cache[cache_key]

        # Query operator's available services
        services_map = self.get_services_for_operator(op)
        matched_code = None

        # Look for exact or substring match in service display names
        for code, name in services_map.items():
            if svc.lower() == name.lower() or svc.lower() in name.lower():
                matched_code = code
                break

        if matched_code:
            self._service_code_cache[cache_key] = matched_code
            return matched_code

        # If already a short code or no match found, fallback to svc
        return svc

    def get_balance(self):
        raw = self._request("getBalance")

        if raw.startswith("ACCESS_BALANCE:"):
            value = raw.split(":", 1)[1].strip()
            try:
                return float(value)
            except ValueError:
                raise OTPError(f"[{self.name}] Invalid balance response: {raw}")

        known_errors = {
            "BAD_KEY": "Incorrect or missing API key",
            "BAD_ACTION": "Invalid action requested",
            "USER_BANNED": "Account has been banned",
            "ERROR": "Generic server error",
            "TOO_MANY_REQUESTS": "Rate limit exceeded. Please slow down requests",
            "UNDER_DEVELOPMENT": "Feature under development"
        }

        if raw in known_errors:
            raise OTPError(f"[{self.name}] {raw}: {known_errors[raw]}")

        raise OTPError(f"[{self.name}] Unexpected balance response: {raw}")

    def get_number(
        self,
        service=None,
        country=None,
        operator=None,
        max_price=None,
        provider_ids=None,
        **kwargs
    ):
        op = operator if operator is not None else self.default_operator
        raw_service = service or self.default_service
        resolved_service = self.resolve_service_code(raw_service, op)
        target_country = country or self.default_country
        approved_price = max_price if max_price is not None else self.max_price

        params = {
            "service": resolved_service,
            "country": target_country
        }

        if op:
            params["operator"] = op
        if approved_price is not None:
            params["maxPrice"] = approved_price
        if provider_ids is not None:
            params["providerIds"] = provider_ids

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
            "BAD_ACTION",
            "BAD_SERVICE",
            "NO_BALANCE",
            "BAD_OPERATOR",
            "BAD_COUNTRY",
            "WRONG_MAX_PRICE",
            "BAD_KEY",
            "SERVICE_BANNED",
            "USER_BANNED",
            "ERROR",
            "TOO_MANY_REQUESTS",
            "UNDER_DEVELOPMENT",
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
        Status codes according to TemporaSMS documentation:
        3 = requests another SMS
        6 = finish activation
        8 = cancels the activation
        """
        raw = self._request("setStatus", {
            "id": str(activation_id),
            "status": int(status)
        })

        if raw.startswith("ACCESS_CANCEL") or raw.startswith("ACCESS_CANCEL_ALREADY") or raw.startswith("ACCESS_ACTIVATION"):
            return {"type": raw.split(":", 1)[0]}

        if raw in {"NO_ACTIVATION", "BAD_STATUS", "BAD_ACTION", "BAD_KEY", "USER_BANNED", "ERROR"}:
            return {"type": raw}

        raise OTPError(f"[{self.name}] Unexpected setStatus response: {raw}")

    def cancel(self, activation_id):
        """Cancel activation (status 8). Handles ACCESS_CANCEL and ACCESS_CANCEL_ALREADY."""
        res = self.set_status(activation_id, 8)
        # TemporaSMS cancels immediately without wait cooldown
        return res

