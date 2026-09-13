"""
VSImpro (api.vsimpro.com) client implementing the SMS-Activate handler_api protocol.

All requests go to https://api.vsimpro.com/stubs/handler_api.php via GET (POST also
accepted by the provider) with the API key passed as the api_key query parameter.

Endpoints used by the automation:
- getBalance   -> ACCESS_BALANCE:<amount>
- getNumber    -> ACCESS_NUMBER:<activation_id>:<number> / NO_NUMBERS / NO_BALANCE / ...
- getStatus    -> STATUS_WAIT_CODE / STATUS_OK:<code> / STATUS_CANCEL
- setStatus    -> status 3 = request next SMS, 6 = finish, 8 = cancel
                  (ACCESS_CANCEL / ACCESS_CANCEL_ALREADY)
- getOperators -> JSON map of operator display names -> operator ids (VSImpro specific)
- getServices  -> JSON map of service codes -> display names (operator optional)

Operator routing supported by VSImpro:
- operator=smart : eligible operators for this exact service/country, picked from
                   live and historical delivery evidence (VSImpro recommended default)
- operator=auto  : lowest final price first
- operator=cheap : only the single lowest-priced eligible operator
- operator=best  : highest success-rate operator first
- numeric id (1, 2, 3, 4, 7, 8, 9, 10): one specific operator

As with Tempora, numeric operators map service names to short codes (e.g. "meesho"
-> its short code); that mapping is resolved automatically via getServices and can
be pinned manually through the operator_services config override.
"""

import json

from base_otp import OTPError
from tempora_client import TemporaClient


class VSImproClient(TemporaClient):
    """handler_api compliant client for api.vsimpro.com (protocol identical to TemporaSMS)."""

    def __init__(
        self,
        base_url="https://api.vsimpro.com/stubs/handler_api.php",
        api_key="",
        default_service="meesho",
        default_country="22",
        default_operator="smart",
        max_price=None,
        timeout=15,
        operator_services=None
    ):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            default_service=default_service,
            default_country=default_country,
            default_operator=default_operator,
            max_price=max_price,
            timeout=timeout,
            operator_services=operator_services
        )
        # TemporaClient hardcodes its name in the base initializer; override it.
        self.name = "vsimpro"

    def get_operators(self):
        """
        Query action=getOperators.
        Response: {"Operator 1": "1", "Operator 2": "2", ...}
        """
        raw = self._request("getOperators")
        try:
            return json.loads(raw)
        except Exception:
            raise OTPError(f"[{self.name}] Invalid getOperators response: {raw}")
