"""
OTP Cart (otpcart.xyz) API Client.

Protocol:
- Bearer JWT token authentication
- Balance check: GET /payment/balance
- Generate number: POST /mobile/generate with {"serviceId": "...", "isDeepCheck": true}
- Cancel number: POST /otp/cancelOtp with {"serialNumber": "...", "mobileId": "...", "serviceId": "..."}
- OTP status check: WebSocket wss://api.otpcart.xyz/check-otp?token=<token>
"""

import json
import re
import time
import requests
import websocket

from base_otp import (
    BaseOTPClient,
    OTPError,
    OTPProviderUnavailable,
    OTPNoBalance,
    OTPNoNumbers
)


class OTPCartClient(BaseOTPClient):

    def __init__(self, token, service_id="68b18f42980e8cf480b1dda8", is_deep_check=True,
                 max_price=None, balance_wait_seconds=2.0, base_url="https://api.otpcart.xyz",
                 ws_url="wss://api.otpcart.xyz/check-otp", timeout=15):
        self.token = str(token).strip()
        if self.token.startswith("Bearer "):
            self.token = self.token[7:].strip()

        super().__init__(name="otpcart", base_url=base_url.rstrip("/"), api_key=self.token, timeout=timeout)

        self.service_id = str(service_id).strip()
        self.is_deep_check = bool(is_deep_check)
        self.max_price = float(max_price) if max_price is not None else None
        self.balance_wait_seconds = float(balance_wait_seconds)
        self.ws_url = ws_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }

    # -- HTTP Request Helper ------------------------------------------------

    def _request(self, method, endpoint, payload=None):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            if method.upper() == "GET":
                resp = requests.get(url, headers=self.headers, timeout=self.timeout)
            else:
                resp = requests.post(url, headers=self.headers, json=payload or {}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise OTPProviderUnavailable(f"[OTPCART] Connection error on {endpoint}: {exc}")

        if resp.status_code in (401, 403):
            raise OTPError(f"[OTPCART] Authentication failed (HTTP {resp.status_code}): {resp.text}")

        try:
            data = resp.json()
        except Exception:
            raise OTPError(f"[OTPCART] Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}")

        return data

    # -- Balance ------------------------------------------------------------

    def get_balance(self):
        """
        Query GET /payment/balance.
        Response: {"message": "user balance!", "balance": 11, ...}
        """
        data = self._request("GET", "payment/balance")
        if "balance" not in data:
            raise OTPError(f"[OTPCART] Unexpected balance response: {data}")

        try:
            bal = float(data["balance"])
            self.last_balance = bal
            return bal
        except (ValueError, TypeError):
            raise OTPError(f"[OTPCART] Invalid balance value: {data.get('balance')}")

    # -- Get Number ---------------------------------------------------------

    def get_number(self, service=None, country=None, max_price=None, operator=None):
        """
        Requests mobile number via POST /mobile/generate.
        Payload: {"serviceId": self.service_id, "isDeepCheck": self.is_deep_check}
        """
        srv_id = service or self.service_id
        target_max_price = max_price if max_price is not None else self.max_price

        payload = {
            "serviceId": srv_id,
            "isDeepCheck": self.is_deep_check
        }

        data = self._request("POST", "mobile/generate", payload)

        is_generated = data.get("isNumberGenerated", False)
        if not is_generated:
            msg = data.get("message", "Number unavailable")
            msg_lower = msg.lower()
            if "balance" in msg_lower or "insufficient" in msg_lower or "low" in msg_lower:
                return {"type": "NO_BALANCE", "raw": msg}
            return {"type": "NO_NUMBERS", "raw": msg}

        mobile = data.get("mobile")
        if not mobile or not isinstance(mobile, dict):
            return {"type": "NO_NUMBERS", "raw": data.get("message", "No mobile object in response")}

        raw_number = str(mobile.get("mobileno", "")).strip()
        serial_number = str(mobile.get("serialNumber", "")).strip()
        mobile_id = str(mobile.get("_id", "")).strip()
        price = float(mobile.get("price", 0))

        # Composite activation ID so cancel/check have all required IDs
        activation_id = f"{serial_number}:{mobile_id}:{srv_id}"

        # Check price limit
        if target_max_price is not None and price > target_max_price:
            # Auto-cancel immediately for refund
            try:
                self.set_status(activation_id, 8)
            except Exception:
                pass
            return {
                "type": "PRICE_TOO_HIGH",
                "price": price,
                "max_price": target_max_price,
                "activation_id": activation_id
            }

        return {
            "type": "ACCESS_NUMBER",
            "activation_id": activation_id,
            "number": raw_number,
            "price": price,
            "serial_number": serial_number,
            "mobile_id": mobile_id,
            "service_id": srv_id
        }

    def wait_for_usable_balance(self, min_balance=5.0, timeout=None, poll_interval=3.0):
        """
        Polls GET /payment/balance until balance >= min_balance or timeout expires.
        Returns True if balance is sufficient, False if timed out.
        """
        max_wait = self.balance_wait_seconds if timeout is None else float(timeout)
        if max_wait <= 0:
            return True

        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                bal = self.get_balance()
                if bal >= min_balance:
                    return True
            except Exception:
                pass
            time.sleep(poll_interval)

        return False

    # -- Status & Actions ---------------------------------------------------

    def set_status(self, activation_id, status):
        """
        Status 8 -> Cancel number (POST /otp/cancelOtp)
        Status 6 -> Mark completed
        """
        parts = str(activation_id).split(":")
        serial_number = parts[0] if len(parts) > 0 else ""
        mobile_id = parts[1] if len(parts) > 1 else ""
        service_id = parts[2] if len(parts) > 2 else self.service_id

        if status == 8:
            # Cancel number for refund
            payload = {
                "serialNumber": serial_number,
                "mobileId": mobile_id,
                "serviceId": service_id
            }
            try:
                res = self._request("POST", "otp/cancelOtp", payload)
            except Exception as exc:
                return {"type": "ERROR", "message": str(exc)}

            if "balance" in res:
                try:
                    self.last_balance = float(res["balance"])
                except Exception:
                    pass

            # Smart balance check: if balance is low, poll until it updates up to balance_wait_seconds
            if self.balance_wait_seconds > 0:
                try:
                    curr = self.get_balance()
                    if curr < 5.0:
                        self.wait_for_usable_balance(min_balance=5.0, timeout=self.balance_wait_seconds)
                except Exception:
                    pass

            return {
                "type": "ACCESS_CANCEL",
                "raw": res.get("message", "Otp canceled successfully"),
                "balance": res.get("balance")
            }

        elif status == 6:
            # Complete
            return {"type": "ACCESS_ACTIVATION"}

        return {"type": "OK"}

    def get_status(self, activation_id):
        """
        Queries OTP status via WebSocket: wss://api.otpcart.xyz/check-otp?token=<token>
        Payload: {"serialNumber": serial_number, "mobileId": mobile_id, "resend": False}
        """
        parts = str(activation_id).split(":")
        serial_number = parts[0] if len(parts) > 0 else ""
        mobile_id = parts[1] if len(parts) > 1 else ""

        ws_endpoint = f"{self.ws_url}?token={self.token}"

        try:
            ws = websocket.create_connection(ws_endpoint, timeout=5)
        except Exception as exc:
            # Connection failed or timed out, retry on next cycle
            return {"type": "STATUS_WAIT", "detail": f"WS connect: {exc}"}

        try:
            payload = {
                "serialNumber": serial_number,
                "mobileId": mobile_id,
                "resend": False
            }
            ws.send(json.dumps(payload))
            ws.settimeout(3.0)
            raw = ws.recv()
            ws.close()
        except Exception as exc:
            try:
                ws.close()
            except Exception:
                pass
            return {"type": "STATUS_WAIT", "detail": f"WS recv: {exc}"}

        try:
            data = json.loads(raw)
        except Exception:
            return {"type": "STATUS_WAIT", "raw": raw}

        # Check for cancelled / used
        msg = str(data.get("message", "")).lower()
        if "cancelled" in msg or "used" in msg:
            return {"type": "STATUS_CANCEL", "raw": data}

        # Check for OTP in response
        # Could be in 'otp', 'otpCode', 'code', or inside 'message' or 'sms'
        code = (
            data.get("otp")
            or data.get("otpCode")
            or data.get("code")
            or BaseOTPClient.extract_code(str(data))
        )

        if code:
            return {
                "type": "STATUS_OK",
                "code": str(code),
                "sms": str(data.get("sms") or data.get("message") or code)
            }

        return {"type": "STATUS_WAIT", "raw": data}
