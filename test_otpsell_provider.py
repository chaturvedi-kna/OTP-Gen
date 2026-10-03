"""
OTPSell (otpsell.com) provider + Telegram "/run <provider>" selection.

Runs without network access by stubbing requests, then checks:

  * OtpSellClient speaks the documented handler_api protocol
    (getBalance -> ACCESS_BALANCE, getNumber with service+country (+ optional
    operator/maxPrice) -> ACCESS_NUMBER, getStatus, setStatus),
  * setStatus REPLAYS the order's service / country / operator (recorded at
    getNumber) - otpsell answers BAD_STATUS otherwise,
  * the ~2 minute cancel window: a cancel before it elapses is refused with
    BAD_STATUS and surfaced as ACCESS_CANCEL_WAIT (with the seconds left) so the
    coordinator defers and retries; cancel_window_remaining() drives the OTP-wait
    stretch so a number is only abandoned once it can be refunded,
  * the catalog endpoints (getOperators / getCountries / getServices) parse
    their JSON maps,
  * create_otp_clients() knows the new provider (all / explicit / alias,
    credentials required) and passes cancel_wait_seconds,
  * validate_provider_selection() rejects unknown names and providers without
    credentials,
  * the worker forwards the configured service/country/operator/maxPrice.

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
    client = OtpSellClient(api_key="KEY123", default_service="meesho",
                           default_country="91", default_operator="server-62")
    respond("ACCESS_NUMBER:ORD-ABC:918627031056")
    res = client.get_number()
    check("otpsell: ACCESS_NUMBER parsed",
          res == {"type": "ACCESS_NUMBER", "activation_id": "ORD-ABC",
                  "number": "918627031056"}, res)
    params = last_call()["params"]
    check("otpsell: getNumber sends service + country + operator",
          params.get("action") == "getNumber"
          and params.get("service") == "meesho"
          and params.get("country") == "91"
          and params.get("operator") == "server-62", params)
    check("otpsell: no maxPrice unless configured", "maxPrice" not in params, params)
    check("otpsell: order context recorded for setStatus replay",
          client._orders.get("ORD-ABC") == {"service": "meesho", "country": "91",
                                            "operator": "server-62"},
          client._orders.get("ORD-ABC"))
    check("otpsell: issue time recorded (cancel window)",
          "ORD-ABC" in client._acquired_at, client._acquired_at)

    # maxPrice forwarded when configured
    respond("ACCESS_NUMBER:ORD-DEF:919900000001")
    client.get_number(max_price=9)
    params = last_call()["params"]
    check("otpsell: configured maxPrice forwarded",
          params.get("maxPrice") == 9, params)

    # operator omitted when not configured -> param absent (but then the order
    # cannot be cancelled, so a specific operator is the intended setup)
    client2 = OtpSellClient(api_key="k", default_service="meesho", default_country="91")
    respond("ACCESS_NUMBER:ORD-GHI:919900000002")
    client2.get_number()
    check("otpsell: operator omitted when not configured",
          "operator" not in last_call()["params"], last_call()["params"])

    respond("NO_NUMBERS")
    check("otpsell: NO_NUMBERS returned as a type",
          client.get_number() == {"type": "NO_NUMBERS"})

    respond("BAD_SERVICE")
    check("otpsell: BAD_SERVICE returned as a type",
          client.get_number() == {"type": "BAD_SERVICE"})

    # service and country are required by the spec
    try:
        OtpSellClient(api_key="k", default_service="", default_country="").get_number()
        check("otpsell: missing service/country raises OTPError", False, "no exception")
    except OTPError as exc:
        check("otpsell: missing service/country raises OTPError", True, exc)


def test_status_and_cancel():
    client = OtpSellClient(api_key="KEY123", default_service="meesho",
                           default_country="91", default_operator="server-62")

    respond("STATUS_WAIT_CODE")
    check("otpsell: STATUS_WAIT_CODE",
          client.get_status("ORD-ABC") == {"type": "STATUS_WAIT_CODE"})

    respond("STATUS_OK:12343")
    res = client.get_status("ORD-ABC")
    check("otpsell: STATUS_OK code extracted",
          res.get("type") == "STATUS_OK" and res.get("code") == "12343", res)
    check("otpsell: getStatus sends the activation id",
          last_call()["params"].get("id") == "ORD-ABC", last_call()["params"])

    respond("STATUS_CANCEL")
    check("otpsell: STATUS_CANCEL",
          client.get_status("ORD-ABC") == {"type": "STATUS_CANCEL"})

    respond("NO_ACTIVATION")
    check("otpsell: NO_ACTIVATION",
          client.get_status("bad") == {"type": "NO_ACTIVATION"})

    # Fresh order so the cancel-window bookkeeping is populated.
    respond("ACCESS_NUMBER:ORD-WIN:919900000077")
    client.get_number()
    act = "ORD-WIN"

    # setStatus REPLAYS the order's service / country / operator - otpsell
    # rejects anything else with BAD_STATUS.
    respond("ACCESS_CANCEL")
    res = client.cancel(act)
    check("otpsell: cancel (status 8) accepted after the window",
          res == {"type": "ACCESS_CANCEL"}, res)
    params = last_call()["params"]
    check("otpsell: setStatus replays service/country/operator + status 8",
          params.get("id") == act and params.get("status") == 8
          and params.get("service") == "meesho" and params.get("country") == "91"
          and params.get("operator") == "server-62", params)
    check("otpsell: order bookkeeping dropped after a successful cancel",
          act not in client._orders and act not in client._acquired_at,
          (client._orders, client._acquired_at))

    # A cancel BEFORE the window has passed is refused with BAD_STATUS and must
    # be surfaced as ACCESS_CANCEL_WAIT (carrying the seconds left) so the
    # coordinator defers instead of critical-stopping.
    respond("ACCESS_NUMBER:ORD-EARLY:919900000088")
    client.get_number()
    early = "ORD-EARLY"
    respond("BAD_STATUS")
    res = client.cancel(early)
    check("otpsell: early cancel (BAD_STATUS) -> ACCESS_CANCEL_WAIT",
          res.get("type") == "ACCESS_CANCEL_WAIT"
          and 115 <= int(res.get("seconds", 0)) <= 120, res)

    # request another SMS (status 3) -> ACCESS_RETRY_GET
    respond("ACCESS_RETRY_GET")
    check("otpsell: request next SMS (status 3) -> ACCESS_RETRY_GET",
          client.request_next_sms(early) == {"type": "ACCESS_RETRY_GET"})
    check("otpsell: setStatus sends status 3",
          last_call()["params"].get("status") == 3, last_call()["params"])

    # finish (status 6) is not documented for OTPSell: tolerate a rejection.
    respond("ACCESS_ACTIVATION")
    check("otpsell: finish (status 6) accepted when supported",
          client.finish(early) == {"type": "ACCESS_ACTIVATION"})

    respond("BAD_STATUS")
    res = client.finish(early)
    check("otpsell: unsupported finish tolerated (FINISH_UNSUPPORTED)",
          res == {"type": "FINISH_UNSUPPORTED", "rejected_as": "BAD_STATUS"}, res)


def test_catalog_endpoints():
    client = OtpSellClient(api_key="KEY123")

    respond(json.dumps({"SERVER-62": "server-62", "Any": "any"}))
    ops = client.get_operators()
    check("otpsell: getOperators parses the JSON map",
          ops == {"SERVER-62": "server-62", "Any": "any"}, ops)
    check("otpsell: getOperators action",
          last_call()["params"].get("action") == "getOperators", last_call()["params"])

    respond(json.dumps({"1": "USA", "91": "india"}))
    check("otpsell: getCountries parses the JSON map",
          client.get_countries() == {"1": "USA", "91": "india"})

    respond(json.dumps({"meesho": "Meesho", "hp": "Meesho"}))
    check("otpsell: getServices parses the JSON map",
          client.get_services() == {"meesho": "Meesho", "hp": "Meesho"})

    respond("not-json")
    try:
        client.get_operators()
        check("otpsell: invalid getOperators JSON raises OTPError", False, "no exception")
    except OTPError as exc:
        check("otpsell: invalid getOperators JSON raises OTPError", True, exc)


def test_cancel_window_remaining():
    """The client tells the coordinator how long until a cancel is accepted."""
    client = OtpSellClient(api_key="KEY123", cancel_wait_seconds=120)
    respond("ACCESS_NUMBER:ORD-W1:919811111111")
    client.get_number()

    left = client.cancel_window_remaining("ORD-W1")
    check("window: full window right after getNumber",
          115.0 <= left <= 120.0, left)

    client._acquired_at["ORD-W1"] = time.time() - 80
    left = client.cancel_window_remaining("ORD-W1")
    check("window: ~40s left 80s after a number was issued",
          38.0 <= left <= 41.0, left)

    client._acquired_at["ORD-W1"] = time.time() - 300
    check("window: 0 once the window has passed",
          client.cancel_window_remaining("ORD-W1") == 0.0,
          client.cancel_window_remaining("ORD-W1"))

    check("window: 0 for an activation this client never issued",
          client.cancel_window_remaining("nope") == 0.0)

    short = OtpSellClient(api_key="k", cancel_wait_seconds=30)
    respond("ACCESS_NUMBER:ORD-W2:919822222222")
    short.get_number()
    check("window: follows the configured cancel_wait_seconds",
          25.0 <= short.cancel_window_remaining("ORD-W2") <= 30.0,
          short.cancel_window_remaining("ORD-W2"))

    # cancel_wait_seconds=0 -> cancels immediately (base answer 0)
    instant = OtpSellClient(api_key="k", cancel_wait_seconds=0)
    check("window: 0 when cancel_wait_seconds is 0 (immediate)",
          instant.cancel_window_remaining("anything") == 0.0)


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
                    "service": "meesho", "country": "91",
                    "operator": "server-62", "cancel_wait_seconds": 120},
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
          and c.default_country == "91" and c.default_operator == "server-62"
          and c.base_url == "https://otpsell.com/stubs/handler_api.php",
          (c.api_key, c.default_service, c.default_country, c.default_operator, c.base_url))
    check("factory: otpsell cancel window defaults to 2 minutes",
          c.cancel_wait_seconds == 120.0, c.cancel_wait_seconds)

    tuned = sell_config()
    tuned["otpsell"]["cancel_wait_seconds"] = 45
    c2 = create_otp_clients(tuned, provider_override="otpsell")[0]
    check("factory: otpsell cancel window configurable via config.json",
          c2.cancel_wait_seconds == 45.0, c2.cancel_wait_seconds)

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


# --- coordinator: worker branch + cancel-window OTP wait ---------------------

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
    config["otpsell"]["operator"] = "server-62"
    config["otpsell"]["max_price"] = 9
    coordinator = build_coordinator(config)
    client = coordinator.client_by_name("otpsell") or \
        create_otp_clients(config, provider_override="otpsell")[0]

    sell_conf = coordinator.config.get("otpsell", {})
    respond("ACCESS_NUMBER:ORD-WK:919900000055")
    res = client.get_number(service=sell_conf.get("service", "meesho"),
                            country=sell_conf.get("country", "91"),
                            operator=sell_conf.get("operator"),
                            max_price=sell_conf.get("max_price"))
    params = last_call()["params"]
    check("worker: otpsell getNumber uses config service/country/operator/maxPrice",
          res["type"] == "ACCESS_NUMBER"
          and params.get("service") == "meesho" and params.get("country") == "91"
          and params.get("operator") == "server-62" and params.get("maxPrice") == 9,
          (res, params))


def otp_wait_coordinator(timeout):
    coordinator = build_coordinator()
    coordinator.settings["otp_timeout_seconds"] = timeout
    coordinator.settings["otp_poll_interval_seconds"] = 0.05
    coordinator.settings["timeout_salvage_probes"] = 1
    coordinator.settings["timeout_salvage_delay"] = 0
    return coordinator


def test_otp_wait_covers_cancel_window():
    """
    OTPSell has a cancel window, so (like OTPIndia) the coordinator must not
    abandon a number before it can actually be refunded: the OTP wait is
    stretched to the end of the cancel window. Scaled down: 0.2s timeout /
    0.7s window.
    """
    coordinator = otp_wait_coordinator(timeout=0.2)
    client = OtpSellClient(api_key="k", cancel_wait_seconds=0.7)
    respond("ACCESS_NUMBER:ORD-OTP:919800000006")
    client.get_number()
    respond(*(["STATUS_WAIT_CODE"] * 60))
    ctx = m.NumberContext(client, "ORD-OTP", "919800000006", "9800000006")
    started = time.time()
    kind, _ = coordinator.wait_for_otp(ctx)
    elapsed = time.time() - started
    check("otp wait: window longer than the timeout -> waits for the window",
          kind == "timeout" and 0.6 <= elapsed < 3.0, (kind, round(elapsed, 2)))
    SCRIPTED[:] = []


def main():
    print("=" * 60)
    print("OTPSELL PROVIDER + /run <provider> CHECK")
    print("=" * 60)

    test_balance()
    test_get_number()
    test_status_and_cancel()
    test_catalog_endpoints()
    test_cancel_window_remaining()
    test_create_clients()
    test_validate_selection()
    test_worker_requests_number_from_config()
    test_otp_wait_covers_cancel_window()

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
