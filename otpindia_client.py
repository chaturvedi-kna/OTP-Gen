"""
OTPIndia (otpindia.org) client implementing the SMS-Activate handler_api protocol.

All requests go to https://otpindia.org/api/stubs/handler_api.php via GET with
the API key passed as the api_key query parameter. Rate limit: 900 requests
per minute.

Endpoints used by the automation:
- getBalance   -> ACCESS_BALANCE:<amount>
- getNumber    -> ACCESS_NUMBER:<activation_id>:<number>
                  (OTPIndia-specific: requires the service code AND a server
                   code listed for that service, e.g.
                   service=meesho&server=Operator-1 or service=wa&server=...)
                  Routes: IN / FR / MX / GB catalogs.
- getStatus    -> STATUS_WAIT_CODE / STATUS_OK:<sms> / STATUS_CANCEL
                  (standard handler_api polling action; see note below)
- setStatus    -> documented: 3 = request new SMS, 8 = cancel (ACCESS_CANCEL
                  / ACCESS_CANCEL_WAIT / WAIT_CANCEL:<seconds>); 6 = finish
                  is attempted best-effort only - see finish()

Cancel window (OTPIndia-specific, unlike TemporaSMS / VSImpro which cancel
immediately): a cancel that arrives before `cancel_wait_seconds` (default
120) have elapsed since getNumber issued the number is answered with
ACCESS_CANCEL_WAIT - the activation stays open and the money stays held
until the window has passed. See set_status() / _cancel_wait_seconds().

Because of that window the coordinator also keeps waiting for the OTP until
the number can actually be refunded: cancel_window_remaining() tells it how
long that is, and the OTP wait is stretched to cover it whenever the
configured automation.otp_timeout_seconds would end earlier (an SMS that
lands at 119s is then still used instead of paid for and thrown away).

Note: the published spec snippets cover getBalance / getNumber / setStatus;
getStatus follows the handler_api convention this protocol is based on
(action=getStatus&id=... -> STATUS_OK:<sms>).
"""

import math
import time

from base_otp import (
    BaseOTPClient,
    OTPError
)


class OtpIndiaClient(BaseOTPClient):
    """handler_api compliant client for otpindia.org."""

    # Capability flag (coordinator): cancels before the cancel window are
    # routine - defer quietly, retry when the window passes; NO_BALANCE with
    # pending window cancels is back-pressure, not a stop signal.
    has_cancel_window = True

    def __init__(
        self,
        base_url="https://otpindia.org/api/stubs/handler_api.php",
        api_key="",
        default_service="meesho",
        default_server="",
        max_price=None,
        timeout=15,
        cancel_wait_seconds=120
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
        # OTPIndia refuses to cancel a number until this long after it was
        # issued (2 minutes by default) - see _cancel_wait_seconds().
        self.cancel_wait_seconds = float(cancel_wait_seconds or 120)
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

            activation_id = parts[1].strip()
            # The cancel window is counted from the moment the number is
            # issued: remember when that was.
            self._acquired_at[activation_id] = time.time()
            return {
                "type": "ACCESS_NUMBER",
                "activation_id": activation_id,
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
            self._acquired_at.pop(str(activation_id), None)
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

        A cancel (8) issued before the cancel window has passed is refused
        with ACCESS_CANCEL_WAIT; the answer carries `seconds` = how long the
        caller should wait before the cancel will be accepted (see
        _cancel_wait_seconds).
        """
        raw = self._request("setStatus", {
            "id": str(activation_id),
            "status": int(status)
        })

        # Must be checked BEFORE the ACCESS_CANCEL prefix match below.
        if raw == "ACCESS_CANCEL_WAIT" or raw.startswith("ACCESS_CANCEL_WAIT:"):
            return {
                "type": "ACCESS_CANCEL_WAIT",
                "seconds": self._cancel_wait_seconds(activation_id, raw)
            }

        if raw.startswith("ACCESS_CANCEL") or raw.startswith("ACCESS_ACTIVATION"):
            self._acquired_at.pop(str(activation_id), None)
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

    def _cancel_wait_seconds(self, activation_id, raw):
        """
        How long until this activation may be cancelled.

        A wait reported by the server itself ("ACCESS_CANCEL_WAIT:<seconds>")
        wins; otherwise the remainder of the OTPIndia cancel window is used
        (cancel_wait_seconds, counted from the moment getNumber issued the
        number - the provider only accepts a cancel after that). A full
        window is assumed when the issue time is unknown (a pending cancel
        resumed after a restart) or when the provider still refuses after
        our own window has passed.
        """
        if ":" in raw:
            try:
                return max(1, int(raw.split(":", 1)[1].strip()))
            except ValueError:
                pass

        if str(activation_id) in self._acquired_at:
            remaining = self.cancel_window_remaining(activation_id)
            if remaining > 0:
                return max(1, int(math.ceil(remaining)))

        return max(1, int(self.cancel_wait_seconds))

    def cancel_window_remaining(self, activation_id):
        """
        Seconds left until OTPIndia accepts a cancel for this activation.

        Counted from the moment getNumber issued the number
        (cancel_wait_seconds, 2 minutes by default). 0.0 once the window has
        passed, and 0.0 for an activation this client did not issue (nothing
        is known about it, so nobody should wait on its account).

        The coordinator uses this to keep waiting for the OTP until the number
        can actually be refunded - see wait_for_otp() in main.py.
        """
        acquired = self._acquired_at.get(str(activation_id))
        if acquired is None:
            return 0.0
        remaining = self.cancel_wait_seconds - (time.time() - acquired)
        return max(0.0, float(remaining))

    def finish(self, activation_id):
        """
        Complete the activation once the OTP has been processed.

        OTPIndia documents only status 3 (request new SMS) and 8 (cancel),
        so the standard finish (status 6, used by the other handler_api
        providers) is attempted but never required: a rejection comes back
        as FINISH_UNSUPPORTED instead of an error. After the OTP is consumed
        the charge stands either way - only a cancel (status 8, documented)
        moves money back, and that one works normally once the cancel window
        has passed (ACCESS_CANCEL_WAIT - see set_status).
        """
        res = self.set_status(activation_id, 6)
        if res.get("type") in {"BAD_STATUS", "BAD_ACTION", "ERROR", "NO_ACTIVATION"}:
            return {"type": "FINISH_UNSUPPORTED", "rejected_as": res.get("type")}
        return res
