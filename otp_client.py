"""
Unified OTP Client Facade providing backwards compatibility, client factories,
and multi-provider management for OtpDoctor and TemporaSMS.
"""

import sys
from base_otp import (
    BaseOTPClient,
    OTPError,
    OTPProviderUnavailable,
    OTPNoBalance,
    OTPNoNumbers
)
from otpdoctor_client import OTPDoctorClient
from tempora_client import TemporaClient
from otpcart_client import OTPCartClient
from vsimpro_client import VSImproClient

# Backward compatibility alias
OTPClient = OTPDoctorClient


def build_tempora_client(config):
    """Build a TemporaClient instance from config dictionary."""
    tempora_conf = config.get("tempora", {})
    return TemporaClient(
        base_url=tempora_conf.get("base_url", "https://api.temporasms.com/stubs/handler_api.php"),
        api_key=tempora_conf.get("api_key", ""),
        default_service=tempora_conf.get("service", "meesho"),
        default_country=tempora_conf.get("country", "22"),
        default_operator=tempora_conf.get("operator", "auto"),
        max_price=tempora_conf.get("max_price"),
        operator_services=tempora_conf.get("operator_services", {})
    )


def build_otpdoctor_client(config):
    """Build an OTPDoctorClient instance from config dictionary."""
    otp_conf = config.get("otp", {})
    return OTPDoctorClient(
        base_url=otp_conf.get("base_url", "https://otpdoctor.in/stubs/handler_api.php"),
        api_key=otp_conf.get("api_key", "")
    )


def build_otpcart_client(config):
    """Build an OTPCartClient instance from config dictionary."""
    cart_conf = config.get("otpcart", {})
    return OTPCartClient(
        token=cart_conf.get("token", ""),
        service_id=cart_conf.get("service_id", "68b18f42980e8cf480b1dda8"),
        is_deep_check=cart_conf.get("is_deep_check", True),
        max_price=cart_conf.get("max_price"),
        balance_wait_seconds=cart_conf.get("balance_update_delay_seconds", 2.0),
        base_url=cart_conf.get("base_url", "https://api.otpcart.xyz"),
        ws_url=cart_conf.get("ws_url", "wss://api.otpcart.xyz/check-otp"),
        timeout=cart_conf.get("timeout", 15)
    )


def build_vsimpro_client(config):
    """Build a VSImproClient instance from config dictionary."""
    vsi_conf = config.get("vsimpro", {})
    return VSImproClient(
        base_url=vsi_conf.get("base_url", "https://api.vsimpro.com/stubs/handler_api.php"),
        api_key=vsi_conf.get("api_key", ""),
        default_service=vsi_conf.get("service", "meesho"),
        default_country=vsi_conf.get("country", "22"),
        default_operator=vsi_conf.get("operator", "smart"),
        max_price=vsi_conf.get("max_price"),
        operator_services=vsi_conf.get("operator_services", {})
    )


def create_otp_clients(config, provider_override=None):
    """
    Returns a list of active OTP client instances based on config and override.
    Options for provider:
      - 'all': runs all enabled providers (otpdoctor, tempora, otpcart, vsimpro)
      - list of names: e.g. ['tempora', 'vsimpro']
      - specific name: 'vsimpro', 'otpcart', 'tempora', 'otpdoctor'
    """
    provider_val = (
        provider_override
        if provider_override is not None
        else config.get("active_otp_provider")
        or config.get("automation", {}).get("otp_provider")
        or "all"
    )

    if isinstance(provider_val, (list, tuple)):
        requested = [str(p).strip().lower() for p in provider_val]
    else:
        requested = [p.strip().lower() for p in str(provider_val).split(",") if p.strip()]

    clients = []

    tempora_conf = config.get("tempora", {})
    otp_conf = config.get("otp", {})
    cart_conf = config.get("otpcart", {})
    vsi_conf = config.get("vsimpro", {})

    tempora_enabled = tempora_conf.get("enabled", True) and bool(tempora_conf.get("api_key"))
    otp_enabled = otp_conf.get("enabled", True) and bool(otp_conf.get("api_key"))
    cart_enabled = cart_conf.get("enabled", True) and bool(cart_conf.get("token"))
    vsi_enabled = vsi_conf.get("enabled", True) and bool(vsi_conf.get("api_key"))

    if "all" in requested or "both" in requested:
        if tempora_enabled:
            clients.append(build_tempora_client(config))
        if otp_enabled:
            clients.append(build_otpdoctor_client(config))
        if cart_enabled:
            clients.append(build_otpcart_client(config))
        if vsi_enabled:
            clients.append(build_vsimpro_client(config))
    else:
        for p in requested:
            if p in ("tempora", "temporasms") and tempora_enabled:
                clients.append(build_tempora_client(config))
            elif p in ("otp", "otpdoctor", "doctor") and otp_enabled:
                clients.append(build_otpdoctor_client(config))
            elif p in ("otpcart", "cart") and cart_enabled:
                clients.append(build_otpcart_client(config))
            elif p in ("vsimpro", "vsi") and vsi_enabled:
                clients.append(build_vsimpro_client(config))

    if not clients:
        # Fallback to whatever has credentials enabled
        if tempora_enabled:
            clients.append(build_tempora_client(config))
        if otp_enabled:
            clients.append(build_otpdoctor_client(config))
        if cart_enabled:
            clients.append(build_otpcart_client(config))
        if vsi_enabled:
            clients.append(build_vsimpro_client(config))

    return clients


def create_otp_client(config, provider=None):
    """Convenience helper returning a single client."""
    clients = create_otp_clients(config, provider_override=provider)
    return clients[0] if clients else build_otpdoctor_client(config)


def test_diagnostics():
    import json
    from pathlib import Path

    print("=" * 60)
    print("OTP CLIENT DIAGNOSTICS")
    print("=" * 60)

    config_path = Path(__file__).resolve().parent / "config.json"
    if not config_path.exists():
        config_path = Path(__file__).resolve().parent / "config.jon"

    if not config_path.exists():
        print("[ERROR] config.json not found.")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    active_provider = config.get("active_otp_provider", "all")
    print(f"Configured active provider mode: '{active_provider}'\n")

    clients = create_otp_clients(config, provider_override=active_provider)
    for client in clients:
        print(f"-- Testing [{client.name.upper()}] --")
        print(f"Base URL: {client.base_url}")
        print(f"API Key:  {client.api_key[:6]}...{client.api_key[-4:] if len(client.api_key) > 10 else ''}")

        try:
            balance = client.get_balance()
            print(f"[ OK ] Balance: {balance:.4f}")
        except Exception as exc:
            print(f"[FAIL] Balance check error: {exc}")

        if isinstance(client, TemporaClient):
            op = client.default_operator
            resolved = client.resolve_service_code(client.default_service, op)
            print(f"[INFO] Service resolution (service='{client.default_service}', operator='{op}'): '{resolved}'")

        print()

    print("=" * 60)


if __name__ == "__main__":
    test_diagnostics()