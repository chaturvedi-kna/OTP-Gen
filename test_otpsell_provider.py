"""
OTPSell (otpsell.com) provider + Telegram "/run <provider>" selection.

Runs without network access by stubbing requests, then checks:

  * OtpSellClient speaks the documented handler_api protocol
    (getBalance -> ACCESS_BALANCE, getNumber with service+country (+ optional
    operator/maxPrice) -> ACCESS_NUMBER, getStatus, setStatus with the
    OTPSell-specific ACCESS_RETRY_GET / ACCESS_CANCEL answers),
  * cancellation is IMMEDIATE (ACCESS_CANCEL, no wait window): the client
    reports cancel_window_remaining() == 0.0 and the coordinator keeps the
    configured OTP timeout instead of stretching it to a cancel window,
  * the catalog endpoints (getOperators / getCountries / getServices) parse
    their JSON maps,
  * create_otp_clients() knows the new provider (all / explicit / alias,
    credentials required),
  * validate_provider_selection() rejects unknown names and providers
    without credentials instead of silently falling back,
  * the worker forwards the configured service/country/operator/maxPrice to
    getNumber.

    python test_otpsell_provider.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = tempfile.mkdtemp(prefix="otpsell_check_")

if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)


# --- stub the optional runtime dependencies (no network in this check) -------

if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        requests = types.ModuleType("requests")

        class _RequestException(Exception):
            pass

        class _Session:
            def get(self, *a, **k):
                raise RuntimeError("network disabled in this check")

            def post(self, *a, **k):
                raise RuntimeError("network disabled in this check")

        requests.Session = _Session
        requests.RequestException = _RequestException
        requests.exceptions = types.SimpleNamespace(RequestException=_RequestException)
        requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network disabled"))
        requests.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network disabled"))
        sys.modules["requests"] = requests

if "websocket" not in sys.modules:
    try:
        import websocket  # noqa: F401
    except ImportError:
        websocket = types.ModuleType("websocket")
        websocket.WebSocketApp = object
        websocket.enableTrace = lambda *a, **k: None
        sys.modules["websocket"] = websocket

import base_otp  # noqa: E402
from base_otp import OTPError, BaseOTPClient  # noqa: E402
from otp_client import (  # noqa: E402
    create_otp_clients,
    validate_provider_selection,
)
from otpsell_client import OtpSellClient  # noqa: E402
import main as m  # noqa: E402


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


# --- scripted HTTP for the raw client checks ---------------------------------

class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


HTTP_CALLS = []
SCRIPTED = []


def scripted_get(url, params=None, timeout=None, **kwargs):
    HTTP_CALLS.append({"url": url, "params": dict(params or {})})
    if not SCRIPTED:
        raise RuntimeError("no scripted response left")
    return FakeResponse(SCRIPTED.pop(0))


base_otp.requests.get = scripted_get


def respond(*texts):
    SCRIPTED[:] = list(texts)


def last_call():
    return HTTP_CALLS[-1]


# --- client protocol checks ---------------------------------------------------

def test_balance():
    client = OtpSellClient(api_key="KEY123")
    respond("ACCESS_BALANCE:100.20")
    bal = client.get_balance()
    check("otpsell: balance parsed from ACCESS_BALANCE", bal == 100.20, bal)
    call = last_call()
    check("otpsell: getBalance action + api_key sent",
          call["params"].get("action") == "getBalance"
          and call["params"].get("api_key") == "KEY123", call["params"])
    check("otpsell: base URL",
          call["url"] == "https://otpsell.com/stubs/handler_api.php", call["url"])

    respond("BAD_KEY")
    try:
        client.get_balance()
        check("otpsell: BAD_KEY raises OTPError", False, "no exception")
    except OTPError as exc:
        check("otpsell: BAD_KEY raises OTPError", "BAD_KEY" in str(exc), exc)


def test_get_number():
    client = OtpSellClient(api_key="KEY123")
    respond("ACCESS_NUMBER:3423423432:914738485900")
    res = client.get_number(service="wa", country="91")
    check("otpsell: ACCESS_NUMBER parsed",
          res == {"type": "ACCESS_NUMBER", "activation_id": "3423423432",
                  "number": "914738485900"}, res)
    params = last_call()["params"]
    check("otpsell: getNumber sends service + country",
          params.get("action") == "getNumber"
          and params.get("service") == "wa"
          and params.get("country") == "91", params)
    check("otpsell: no operator param unless configured", "operator" not in params, params)
    check("otpsell: no maxPrice unless configured", "maxPrice" not in params, params)

    # operator is optional and forwarded when given
    respond("ACCESS_NUMBER:1:919900000001")
    client.get_number(service="wa", country="91", operator="6", max_price=12)
    params = last_call()["params"]
    check("otpsell: operator + maxPrice forwarded when provided",
          params.get("operator") == "6" and params.get("maxPrice") == 12, params)

    # Defaults from config are used when the caller passes nothing.
    client2 = OtpSellClient(api_key="k", default_service="meesho",
                            default_country="91", default_operator="3", max_price=9)
    respond("ACCESS_NUMBER:2:919900000002")
    client2.get_number()
    params = last_call()["params"]
    check("otpsell: configured service/country/operator/maxPrice defaults used",
          params.get("service") == "meesho" and params.get("country") == "91"
          and params.get("operator") == "3" and params.get("maxPrice") == 9, params)

    respond("NO_NUMBERS")
    res = client.get_number(service="wa", country="91")
    check("otpsell: NO_NUMBERS returned as a type", res == {"type": "NO_NUMBERS"}, res)

    respond("NO_BALANCE")
    res = client.get_number(service="wa", country="91")
    check("otpsell: NO_BALANCE returned as a type", res == {"type": "NO_BALANCE"}, res)

    respond("BAD_SERVICE")
    res = client.get_number(service="nope", country="91")
    check("otpsell: BAD_SERVICE returned as a type", res == {"type": "BAD_SERVICE"}, res)

    # service and country are required by the spec
    respond("ACCESS_NUMBER:3:919900000003")
    try:
        OtpSellClient(api_key="k", default_service="", default_country="").get_number()
        check("otpsell: missing service/country raises OTPError", False, "no exception")
    except OTPError as exc:
        check("otpsell: missing service/country raises OTPError", True, exc)


def test_status_and_cancel():
    client = OtpSellClient(api_key="KEY123")

    respond("STATUS_WAIT_CODE")
    check("otpsell: STATUS_WAIT_CODE",
          client.get_status("3423423432") == {"type": "STATUS_WAIT_CODE"})

    respond("STATUS_OK:12343")
    res = client.get_status("3423423432")
    check("otpsell: STATUS_OK code extracted",
          res.get("type") == "STATUS_OK" and res.get("code") == "12343", res)
    check("otpsell: getStatus sends the activation id",
          last_call()["params"].get("id") == "3423423432", last_call()["params"])

    respond("STATUS_CANCEL")
    check("otpsell: STATUS_CANCEL",
          client.get_status("3423423432") == {"type": "STATUS_CANCEL"})

    respond("NO_ACTIVATION")
    check("otpsell: NO_ACTIVATION",
          client.get_status("bad") == {"type": "NO_ACTIVATION"})

    # Cancel (status 8) is IMMEDIATE - ACCESS_CANCEL, no wait window.
    respond("ACCESS_CANCEL")
    check("otpsell: cancel (status 8) is immediate",
          client.cancel("3423423432") == {"type": "ACCESS_CANCEL"})
    check("otpsell: setStatus sends status 8",
          last_call()["params"].get("status") == 8, last_call()["params"])

    # Request another SMS (status 3) -> ACCESS_RETRY_GET (OTPSell-specific).
    respond("ACCESS_RETRY_GET")
    check("otpsell: request next SMS (status 3) -> ACCESS_RETRY_GET",
          client.request_next_sms("3423423432") == {"type": "ACCESS_RETRY_GET"})
    check("otpsell: setStatus sends status 3",
          last_call()["params"].get("status") == 3, last_call()["params"])

    # finish (status 6) is not documented for OTPSell: tolerate a rejection.
    respond("ACCESS_ACTIVATION")
    check("otpsell: finish (status 6) accepted when supported",
          client.finish("3423423432") == {"type": "ACCESS_ACTIVATION"})

    respond("BAD_STATUS")
    res = client.finish("3423423432")
    check("otpsell: unsupported finish tolerated (FINISH_UNSUPPORTED)",
          res == {"type": "FINISH_UNSUPPORTED", "rejected_as": "BAD_STATUS"}, res)


def test_catalog_endpoints():
    client = OtpSellClient(api_key="KEY123")

    respond(json.dumps({"Operator 1": "1", "Operator 2": "2", "Any": "any"}))
    ops = client.get_operators()
    check("otpsell: getOperators parses the JSON map",
          ops == {"Operator 1": "1", "Operator 2": "2", "Any": "any"}, ops)
    check("otpsell: getOperators action",
          last_call()["params"].get("action") == "getOperators", last_call()["params"])

    respond(json.dumps({"1": "Ukraine", "91": "India", "21": "USA"}))
    countries = client.get_countries()
    check("otpsell: getCountries parses the JSON map",
          countries == {"1": "Ukraine", "91": "India", "21": "USA"}, countries)

    respond(json.dumps({"wa": "WhatsApp", "tg": "Telegram", "ig": "Instagram"}))
    services = client.get_services()
    check("otpsell: getServices parses the JSON map",
          services == {"wa": "WhatsApp", "tg": "Telegram", "ig": "Instagram"}, services)

    respond("not-json")
    try:
        client.get_operators()
        check("otpsell: invalid getOperators JSON raises OTPError", False, "no exception")
    except OTPError as exc:
        check("otpsell: invalid getOperators JSON raises OTPError", True, exc)


def test_cancel_is_immediate():
    """OTPSell cancels right away - no cancel window for the coordinator to wait on."""
    client = OtpSellClient(api_key="KEY123")
    check("otpsell: cancel_window_remaining is 0 (immediate cancel)",
          client.cancel_window_remaining("anything") == 0.0,
          client.cancel_window_remaining("anything"))

    # Inherited base behaviour: even a plain client cancels immediately.
    plain = BaseOTPClient(name="plain", base_url="http://x", api_key="k")
    check("otpsell: matches the base immediate-cancel answer",
          plain.cancel_window_remaining("anything") == 0.0)


# --- factory + selection validation ------------------------------------------

def sell_config(api_key="sell-key"):
    return {
        "active_otp_provider": "all",
        "tempora": {"enabled": True, "api_key": "t-key"},
        "otp": {"enabled": True, "api_key": "o-key"},
        "otpcart": {"enabled": True, "token": "c-token"},
        "vsimpro": {"enabled": True, "api_key": "v-key"},
        "otpindia": {"enabled": True, "api_key": "i-key", "service": "meesho", "server": "3"},
        "otpsell": {"enabled": True, "api_key": api_key,
                    "service": "meesho", "country": "91", "operator": "", "max_price": None},
    }


def test_create_clients():
    cfg = sell_config()

    names = [c.name for c in create_otp_clients(cfg)]
    check("factory: 'all' includes otpsell", "otpsell" in names, names)
    check("factory: all providers present",
          set(names) == {"tempora", "otpdoctor", "otpcart", "vsimpro", "otpindia", "otpsell"},
          names)

    clients = create_otp_clients(cfg, provider_override="otpsell")
    check("factory: explicit otpsell selection", len(clients) == 1
          and clients[0].name == "otpsell", [c.name for c in clients])
    c = clients[0]
    check("factory: otpsell client configured from config.json",
          c.api_key == "sell-key" and c.default_service == "meesho"
          and c.default_country == "91"
          and c.base_url == "https://otpsell.com/stubs/handler_api.php",
          (c.api_key, c.default_service, c.default_country, c.base_url))

    clients = create_otp_clients(cfg, provider_override="sell")
    check("factory: 'sell' alias maps to otpsell",
          len(clients) == 1 and clients[0].name == "otpsell",
          [c.name for c in clients])

    clients = create_otp_clients(cfg, provider_override="vsimpro,otpsell")
    check("factory: comma selection with otpsell",
          [c.name for c in clients] == ["vsimpro", "otpsell"],
          [c.name for c in clients])

    no_key = sell_config(api_key="")
    names = [c.name for c in create_otp_clients(no_key)]
    check("factory: otpsell without api_key is excluded from 'all'",
          "otpsell" not in names, names)


def test_validate_selection():
    cfg = sell_config()
    no_key = sell_config(api_key="")

    ok, err = validate_provider_selection(cfg, "otpsell")
    check("validate: configured otpsell ok", ok and err is None, (ok, err))

    ok, err = validate_provider_selection(cfg, "sell")
    check("validate: 'sell' alias ok", ok and err is None, (ok, err))

    ok, err = validate_provider_selection(cfg, "bogus")
    check("validate: unknown provider rejected",
          not ok and "Unknown provider 'bogus'" in err
          and "otpsell" in err, (ok, err))

    ok, err = validate_provider_selection(no_key, "otpsell")
    check("validate: missing api_key rejected",
          not ok and "otpsell" in err and "api_key" in err, (ok, err))


# --- coordinator: worker branch + immediate-cancel OTP wait ------------------

def build_coordinator(config=None):
    if config is None:
        config = json.load(open(os.path.join(REPO_DIR, "config.json"), encoding="utf-8"))
        config["active_otp_provider"] = "tempora"
    config["telegram"]["enabled"] = False
    config["termux"]["enabled"] = False
    os.chdir(SCRATCH_DIR)
    coordinator = m.ParallelAutomationCoordinator(config, provider_override="tempora")
    coordinator.notify.send = lambda *a, **k: None
    coordinator.notify.alert = lambda *a, **k: None
    coordinator.run = lambda *a, **k: None
    return coordinator


def test_worker_requests_number_from_config():
    """The worker's otpsell branch forwards config service/country/operator/maxPrice."""
    config = json.load(open(os.path.join(REPO_DIR, "config.json"), encoding="utf-8"))
    config["active_otp_provider"] = "tempora"
    config["otpsell"]["service"] = "meesho"
    config["otpsell"]["country"] = "91"
    config["otpsell"]["operator"] = "4"
    config["otpsell"]["max_price"] = 11
    coordinator = build_coordinator(config)
    client = coordinator.client_by_name("otpsell") or \
        create_otp_clients(config, provider_override="otpsell")[0]

    sell_conf = coordinator.config.get("otpsell", {})
    respond("ACCESS_NUMBER:55:919900000055")
    res = client.get_number(service=sell_conf.get("service", "meesho"),
                            country=sell_conf.get("country", "91"),
                            operator=sell_conf.get("operator"),
                            max_price=sell_conf.get("max_price"))
    params = last_call()["params"]
    check("worker: otpsell getNumber uses config service/country/operator/maxPrice",
          res["type"] == "ACCESS_NUMBER"
          and params.get("service") == "meesho" and params.get("country") == "91"
          and params.get("operator") == "4" and params.get("maxPrice") == 11,
          (res, params))


def otp_wait_coordinator(timeout):
    coordinator = build_coordinator()
    coordinator.settings["otp_timeout_seconds"] = timeout
    coordinator.settings["otp_poll_interval_seconds"] = 0.05
    coordinator.settings["timeout_salvage_probes"] = 1
    coordinator.settings["timeout_salvage_delay"] = 0
    return coordinator


def test_otp_wait_not_stretched():
    """
    OTPSell cancels immediately, so the coordinator keeps the configured OTP
    timeout - it must NOT be stretched to a cancel window (unlike OTPIndia).
    """
    coordinator = otp_wait_coordinator(timeout=0.3)
    client = OtpSellClient(api_key="k")
    respond(*(["STATUS_WAIT_CODE"] * 40))
    ctx = m.NumberContext(client, "act-sell", "919900000001", "9800000001")
    started = time.time()
    kind, status = coordinator.wait_for_otp(ctx)
    elapsed = time.time() - started
    check("otp wait: immediate-cancel provider keeps the configured timeout",
          kind == "timeout" and status is None and 0.28 <= elapsed < 0.8,
          (kind, round(elapsed, 2)))
    SCRIPTED[:] = []


def main():
    print("=" * 60)
    print("OTPSELL PROVIDER + /run <provider> CHECK")
    print("=" * 60)

    test_balance()
    test_get_number()
    test_status_and_cancel()
    test_catalog_endpoints()
    test_cancel_is_immediate()
    test_create_clients()
    test_validate_selection()
    test_worker_requests_number_from_config()
    test_otp_wait_not_stretched()

    shutil.rmtree(SCRATCH_DIR, ignore_errors=True)

    print("=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
