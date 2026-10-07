"""
Unified OTP Client Facade providing backwards compatibility, client factories,
and multi-provider management for OtpDoctor, TemporaSMS, VSImpro, OTPCart,
OTPIndia and OTPSell.
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
from otpindia_client import OtpIndiaClient
from otpsell_client import OtpSellClient

# Backward compatibility alias
OTPClient = OTPDoctorClient

# Every provider this tool knows, in display order, with its accepted aliases.
PROVIDER_ORDER = ("tempora", "otpdoctor", "otpcart", "vsimpro", "otpindia", "otpsell")
PROVIDER_ALIASES = {
    "tempora": "tempora",
    "temporasms": "tempora",
    "otp": "otpdoctor",
    "otpdoctor": "otpdoctor",
    "doctor": "otpdoctor",
    "otpcart": "otpcart",
    "cart": "otpcart",
    "vsimpro": "vsimpro",
    "vsi": "vsimpro",
    "otpindia": "otpindia",
    "india": "otpindia",
    "otpsell": "otpsell",
    "sell": "otpsell",
}
# --provider / "/run" values that mean "every enabled provider".
PROVIDER_WILDCARDS = ("all", "both")


def parse_provider_selection(provider_val):
    """Normalize a provider selection (string or list) to lowercase tokens."""
    if provider_val is None:
        return []
    if isinstance(provider_val, (list, tuple, set)):
        return [str(p).strip().lower() for p in provider_val if str(p).strip()]
    return [p.strip().lower() for p in str(provider_val).split(",") if p.strip()]


def canonical_provider(token):
    """Map an alias ("vsi", "doctor", "india", ...) to its canonical name."""
    return PROVIDER_ALIASES.get(str(token or "").strip().lower())


def provider_config(config, name):
    """Config block for a canonical provider name (OtpDoctor lives under "otp")."""
    config = config or {}
    if name == "otpdoctor":
        return config.get("otpdoctor") or config.get("otp") or {}
    return config.get(name) or {}


def provider_ready(config, name):
    """True when the provider is enabled and has credentials in config.json."""
    conf = provider_config(config, name)
    if not conf.get("enabled", True):
        return False
    if name == "otpcart":
        return bool(conf.get("token"))
    return bool(conf.get("api_key"))


def validate_provider_selection(config, provider_val):
    """
    Validate a --provider / Telegram "/run <provider>" selection.

    Returns (True, None) when the selection can be turned into clients, or
    (False, "<reason>") with a user-facing explanation otherwise. Unknown
    names and providers without credentials are rejected instead of silently
    falling back to "whatever is configured".
    """
    tokens = parse_provider_selection(provider_val)
    if not tokens:
        return True, None
    if any(t in PROVIDER_WILDCARDS for t in tokens):
        return True, None

    unknown = []
    not_ready = []
    for token in tokens:
        canonical = canonical_provider(token)
        if canonical is None:
            unknown.append(token)
        elif not provider_ready(config, canonical):
            not_ready.append(canonical)

    if unknown:
        return False, (
            f"Unknown provider '{unknown[0]}'. Known providers: "
            f"{', '.join(PROVIDER_ORDER)} "
            f"(aliases: tempora/temporasms, otp/otpdoctor/doctor, "
            f"otpcart/cart, vsimpro/vsi, otpindia/india, otpsell/sell)."
        )
    if not_ready:
        names = ", ".join(sorted(set(not_ready)))
        return False, (
            f"Provider(s) {names} not usable: set \"enabled\": true and an "
            f"api_key (otpcart: token) for it in config.json."
        )
    return True, None


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


def build_otpindia_client(config):
    """Build an OtpIndiaClient instance from config dictionary."""
    india_conf = config.get("otpindia", {})
    return OtpIndiaClient(
        base_url=india_conf.get("base_url", "https://otpindia.org/api/stubs/handler_api.php"),
        api_key=india_conf.get("api_key", ""),
        default_service=india_conf.get("service", "meesho"),
        default_server=india_conf.get("server", ""),
        max_price=india_conf.get("max_price"),
        timeout=india_conf.get("timeout", 15),
        cancel_wait_seconds=india_conf.get("cancel_wait_seconds", 120)
    )


def build_otpsell_client(config):
    """Build an OtpSellClient instance from config dictionary."""
    sell_conf = config.get("otpsell", {})
    return OtpSellClient(
        base_url=sell_conf.get("base_url", "https://otpsell.com/stubs/handler_api.php"),
        api_key=sell_conf.get("api_key", ""),
        default_service=sell_conf.get("service", "wa"),
        default_country=sell_conf.get("country", "91"),
        default_operator=sell_conf.get("operator", "any"),
        operators=sell_conf.get("operators"),
        max_price=sell_conf.get("max_price"),
        timeout=sell_conf.get("timeout", 15),
        cancel_wait_seconds=sell_conf.get("cancel_wait_seconds", 120),
        min_request_interval_seconds=sell_conf.get("min_request_interval_seconds", 0.25),
    )


def create_otp_clients(config, provider_override=None):
    """
    Returns a list of active OTP client instances based on config and override.
    Options for provider:
      - 'all': runs all enabled providers (otpdoctor, tempora, otpcart, vsimpro, otpindia, otpsell)
      - list of names: e.g. ['tempora', 'vsimpro']
      - specific name: 'vsimpro', 'otpcart', 'tempora', 'otpdoctor', 'otpindia', 'otpsell'
        (aliases accepted too: 'vsi', 'cart', 'doctor', 'india', 'sell', ...)
    """
    provider_val = (
        provider_override
        if provider_override is not None
        else config.get("active_otp_provider")
        or config.get("automation", {}).get("otp_provider")
        or "all"
    )

    requested = parse_provider_selection(provider_val)

    clients = []

    tempora_enabled = provider_ready(config, "tempora")
    otp_enabled = provider_ready(config, "otpdoctor")
    cart_enabled = provider_ready(config, "otpcart")
    vsi_enabled = provider_ready(config, "vsimpro")
    india_enabled = provider_ready(config, "otpindia")
    sell_enabled = provider_ready(config, "otpsell")

    if "all" in requested or "both" in requested:
        if tempora_enabled:
            clients.append(build_tempora_client(config))
        if otp_enabled:
            clients.append(build_otpdoctor_client(config))
        if cart_enabled:
            clients.append(build_otpcart_client(config))
        if vsi_enabled:
            clients.append(build_vsimpro_client(config))
        if india_enabled:
            clients.append(build_otpindia_client(config))
        if sell_enabled:
            clients.append(build_otpsell_client(config))
    else:
        for p in requested:
            canonical = canonical_provider(p)
            if canonical == "tempora" and tempora_enabled:
                clients.append(build_tempora_client(config))
            elif canonical == "otpdoctor" and otp_enabled:
                clients.append(build_otpdoctor_client(config))
            elif canonical == "otpcart" and cart_enabled:
                clients.append(build_otpcart_client(config))
            elif canonical == "vsimpro" and vsi_enabled:
                clients.append(build_vsimpro_client(config))
            elif canonical == "otpindia" and india_enabled:
                clients.append(build_otpindia_client(config))
            elif canonical == "otpsell" and sell_enabled:
                clients.append(build_otpsell_client(config))

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
        if india_enabled:
            clients.append(build_otpindia_client(config))
        if sell_enabled:
            clients.append(build_otpsell_client(config))

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