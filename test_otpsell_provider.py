"""
OTPSell (otpsell.com) provider + SQLite refund ledger.

Runs without network access by stubbing requests, then checks:

  * OtpSellClient speaks the documented handler_api protocol
    (getBalance -> ACCESS_BALANCE, getOperators/getCountries/getServices ->
    JSON maps, getNumber with service+country+operator -> ACCESS_NUMBER,
    getStatus, setStatus 3/8),
  * maxPrice is sent when configured and demanded for operators 6 & 9,
  * per-operator cancel windows (cancel_wait_seconds: scalar or
    {"default": ..., "<op>": ...}): a cancel inside the window is refused
    CLIENT-SIDE with the ACCESS_CANCEL_WAIT contract the coordinator already
    knows from OTPIndia (no provider call -> nothing goes stale), and passes
    through the moment the window has passed,
  * operator rotation pools spread getNumber across every configured
    operator (more numbers for fast iterations) and the activation window
    follows the operator that actually served the number,
  * create_otp_clients()/validate_provider_selection() know the provider
    (all / explicit / 'sell' alias, credentials required),
  * the coordinator: the OTP wait covers OTPSell's cancel window, a windowed
    cancel is deferred and retried (quietly - it is routine), refund tallied,
  * refund_ledger (SQLite): baseline + holds are noted atomically, expected
    balances stay exact under fast parallel buying (the OTPIndia refund-tally
    race - a settle while OTHER cancels still hold no longer double-counts
    their holds), the baseline survives a restart, and a refund that never
    lands is DETECTED instead of silently passing.

    python test_otpsell_provider.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
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
from base_otp import OTPError  # noqa: E402
from otp_client import (  # noqa: E402
    create_otp_clients,
    validate_provider_selection,
)
from otpsell_client import OtpSellClient, _parse_cancel_windows  # noqa: E402
from refund_ledger import RefundLedger  # noqa: E402
import main as m  # noqa: E402


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


# --- scripted HTTP, queued PER ACTION (deterministic under polling) -----------

class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


HTTP_CALLS = []
ACTION_QUEUES = {}   # action -> list of upcoming bodies
ACTION_TAILS = {}    # action -> body repeated once its queue is empty


def scripted_get(url, params=None, timeout=None, **kwargs):
    params = dict(params or {})
    HTTP_CALLS.append({"url": url, "params": params})
    action = str(params.get("action") or "")
    queue = ACTION_QUEUES.get(action) or []
    if queue:
        return FakeResponse(queue.pop(0))
    tail = ACTION_TAILS.get(action)
    if tail is None:
        raise RuntimeError(f"no scripted response for action {action!r}")
    return FakeResponse(tail)


base_otp.requests.get = scripted_get


def respond(action, *texts, tail=None):
    """Queue responses for one handler_api action.

    Once the queued bodies are used up, the tail (by default the last queued
    body) is repeated forever, so polling loops never run dry.
    """
    queue = ACTION_QUEUES.setdefault(action, [])
    queue.extend(texts)
    ACTION_TAILS[action] = tail if tail is not None else (texts[-1] if texts else None)


def reset_http():
    HTTP_CALLS.clear()
    ACTION_QUEUES.clear()
    ACTION_TAILS.clear()


def calls_for(action):
    return [c for c in HTTP_CALLS if c["params"].get("action") == action]


def last_call():
    return HTTP_CALLS[-1]


# --- client protocol checks ---------------------------------------------------

def test_balance():
    reset_http()
    client = OtpSellClient(api_key="KEY123", min_request_interval_seconds=0)
    respond("getBalance", "ACCESS_BALANCE:100.20")
    bal = client.get_balance()
    check("otpsell: balance parsed from ACCESS_BALANCE", bal == 100.20, bal)
    call = last_call()
    check("otpsell: getBalance action + api_key sent",
          call["params"].get("action") == "getBalance"
          and call["params"].get("api_key") == "KEY123", call["params"])
    check("otpsell: base URL",
          call["url"] == "https://otpsell.com/stubs/handler_api.php", call["url"])

    for body in ("BAD_KEY", "ERROR"):
        respond("getBalance", body, tail="ACCESS_BALANCE:1")
        try:
            client.get_balance()
            check(f"otpsell: {body} raises OTPError", False, "no exception")
        except OTPError as exc:
            check(f"otpsell: {body} raises OTPError", body in str(exc), exc)


def test_catalogs():
    reset_http()
    client = OtpSellClient(api_key="KEY123", min_request_interval_seconds=0)

    respond("getOperators", '{"Operator 1": "1", "Operator 2": "2", "Any": "any"}')
    ops = client.get_operators()
    check("otpsell: getOperators -> operator map",
          ops == {"Operator 1": "1", "Operator 2": "2", "Any": "any"}, ops)

    respond("getCountries", '{"1": "Ukraine", "91": "India", "21": "USA"}')
    countries = client.get_countries()
    check("otpsell: getCountries -> country map",
          countries == {"1": "Ukraine", "91": "India", "21": "USA"}, countries)

    respond("getServices", '{"wa": "WhatsApp", "tg": "Telegram", "ig": "Instagram"}')
    services = client.get_services()
    check("otpsell: getServices -> service map",
          services == {"wa": "WhatsApp", "tg": "Telegram", "ig": "Instagram"},
          services)

    respond("getOperators", "TOO_MANY_REQUESTS")
    try:
        client.get_operators()
        check("otpsell: TOO_MANY_REQUESTS on a catalog raises OTPError", False,
              "no exception")
    except OTPError as exc:
        check("otpsell: TOO_MANY_REQUESTS on a catalog raises OTPError",
              "TOO_MANY_REQUESTS" in str(exc), exc)

    respond("getCountries", "BAD_ACTION")
    try:
        client.get_countries()
        check("otpsell: BAD_ACTION on a catalog raises OTPError", False,
              "no exception")
    except OTPError as exc:
        check("otpsell: BAD_ACTION on a catalog raises OTPError",
              "BAD_ACTION" in str(exc), exc)


def test_get_number():
    reset_http()
    client = OtpSellClient(api_key="KEY123", default_service="wa",
                           default_country="91", default_operator="any",
                           min_request_interval_seconds=0)
    respond("getNumber", "ACCESS_NUMBER:3423423432:914738485900")
    res = client.get_number(service="wa", country="91")
    check("otpsell: ACCESS_NUMBER parsed",
          res.get("type") == "ACCESS_NUMBER"
          and res.get("activation_id") == "3423423432"
          and res.get("number") == "914738485900", res)
    params = last_call()["params"]
    check("otpsell: getNumber sends service + country + operator",
          params.get("action") == "getNumber"
          and params.get("service") == "wa"
          and params.get("country") == "91"
          and params.get("operator") == "any", params)
    check("otpsell: the serving operator rides along for the cancel window",
          res.get("operator") == "any", res)

    # Defaults from config are used when the caller passes nothing.
    respond("getNumber", "ACCESS_NUMBER:9:9111111111")
    client.get_number()
    params = last_call()["params"]
    check("otpsell: configured service/country defaults used",
          params.get("service") == "wa" and params.get("country") == "91", params)

    # maxPrice only when configured.
    check("otpsell: no maxPrice unless configured",
          all("maxPrice" not in c["params"] for c in calls_for("getNumber")),
          calls_for("getNumber"))
    with_price = OtpSellClient(api_key="k", max_price=8.5,
                               min_request_interval_seconds=0)
    respond("getNumber", "ACCESS_NUMBER:10:9222222222")
    with_price.get_number(operator="1")
    check("otpsell: configured maxPrice forwarded",
          last_call()["params"].get("maxPrice") == 8.5, last_call()["params"])

    # maxPrice is MANDATORY for operators 6 & 9: without one configured the
    # client refuses locally instead of burning a request.
    no_price = OtpSellClient(api_key="k", min_request_interval_seconds=0)
    try:
        no_price.get_number(operator="6")
        check("otpsell: operator 6 without maxPrice is refused locally", False,
              "no exception")
    except OTPError as exc:
        check("otpsell: operator 6 without maxPrice is refused locally",
              "maxPrice" in str(exc), exc)
    check("otpsell: the refused request never hit the wire",
          calls_for("getNumber")[-1]["params"].get("maxPrice") == 8.5,
          calls_for("getNumber"))
    no_price_9 = OtpSellClient(api_key="k", max_price=3, min_request_interval_seconds=0)
    respond("getNumber", "ACCESS_NUMBER:11:9333333333")
    no_price_9.get_number(operator="9")
    check("otpsell: operator 9 with maxPrice goes through",
          last_call()["params"].get("maxPrice") == 3
          and last_call()["params"].get("operator") == "9", last_call()["params"])

    for body in ("NO_NUMBERS", "NO_BALANCE", "BAD_SERVICE"):
        respond("getNumber", body, tail="ACCESS_NUMBER:1:910")
        res = client.get_number(service="wa", country="91")
        check(f"otpsell: {body} returned as a type", res == {"type": body}, res)


def test_status_and_setstatus():
    reset_http()
    client = OtpSellClient(api_key="KEY123", min_request_interval_seconds=0)

    respond("getStatus", "STATUS_WAIT_CODE")
    check("otpsell: STATUS_WAIT_CODE",
          client.get_status("3423423432") == {"type": "STATUS_WAIT_CODE"})

    respond("getStatus", "STATUS_OK:12343")
    res = client.get_status("3423423432")
    check("otpsell: STATUS_OK code extracted",
          res.get("type") == "STATUS_OK" and res.get("code") == "12343", res)
    check("otpsell: getStatus sends the activation id",
          last_call()["params"].get("id") == "3423423432", last_call()["params"])

    respond("getStatus", "NO_ACTIVATION")
    check("otpsell: NO_ACTIVATION for an unknown order id",
          client.get_status("nope") == {"type": "NO_ACTIVATION"})

    respond("getStatus", "STATUS_CANCEL")
    check("otpsell: STATUS_CANCEL (timed out / cancelled)",
          client.get_status("3423423432") == {"type": "STATUS_CANCEL"})

    # setStatus 3 = request another SMS.
    respond("setStatus", "ACCESS_RETRY_GET")
    check("otpsell: request another SMS (status 3)",
          client.request_next_sms("3423423432") == {"type": "ACCESS_RETRY_GET"})
    check("otpsell: setStatus sends status 3",
          calls_for("setStatus")[-1]["params"].get("status") == 3,
          calls_for("setStatus"))

    # finish (status 6) is undocumented: tolerated either way.
    respond("setStatus", "BAD_STATUS")
    res = client.finish("3423423432")
    check("otpsell: unsupported finish tolerated",
          res == {"type": "FINISH_UNSUPPORTED", "rejected_as": "BAD_STATUS"}, res)


# --- per-operator cancel windows ----------------------------------------------

def test_cancel_window_parsing():
    check("windows: scalar applies to every operator",
          _parse_cancel_windows(60) == {"default": 60.0}, _parse_cancel_windows(60))
    windows = _parse_cancel_windows({"default": 120, "1": 120, "2": 60, "any": 90})
    check("windows: mapping kept per operator",
          windows.get("1") == 120.0 and windows.get("2") == 60.0
          and windows.get("any") == 90.0, windows)
    check("windows: default falls back to 120",
          _parse_cancel_windows({})["default"] == 120.0, _parse_cancel_windows({}))
    check("windows: junk ignored",
          _parse_cancel_windows({"x": "nope", "default": "45"})["default"] == 45.0,
          _parse_cancel_windows({"x": "nope", "default": "45"}))


def test_operator_rotation_pool():
    reset_http()
    client = OtpSellClient(api_key="k", operators=["1", "2"],
                           cancel_wait_seconds={"default": 90, "1": 120, "2": 60},
                           min_request_interval_seconds=0)
    ops = []
    for i in range(4):
        respond("getNumber", f"ACCESS_NUMBER:{100 + i}:9190000000{i}")
        ops.append(client.get_number()["operator"])
    check("rotation: getNumber walks the pool round-robin",
          ops == ["1", "2", "1", "2"], ops)
    check("rotation: the rotating operator is what goes on the wire",
          [c["params"].get("operator") for c in calls_for("getNumber")] == ["1", "2", "1", "2"])

    check("rotation: window follows the serving operator (op 1 = 120s)",
          115.0 <= client.cancel_window_remaining("100") <= 120.0,
          client.cancel_window_remaining("100"))
    check("rotation: window follows the serving operator (op 2 = 60s)",
          55.0 <= client.cancel_window_remaining("101") <= 60.0,
          client.cancel_window_remaining("101"))

    # When the pool is empty the single configured operator is used.
    single = OtpSellClient(api_key="k", default_operator="2",
                           cancel_wait_seconds={"default": 120, "2": 60},
                           min_request_interval_seconds=0)
    respond("getNumber", "ACCESS_NUMBER:200:9191111111")
    single.get_number()
    check("rotation: without a pool the configured operator stands",
          last_call()["params"].get("operator") == "2"
          and 55.0 <= single.cancel_window_remaining("200") <= 60.0,
          (last_call()["params"], single.cancel_window_remaining("200")))


def test_cancel_blocked_inside_window_client_side():
    reset_http()
    client = OtpSellClient(api_key="k", default_operator="any",
                           cancel_wait_seconds={"default": 120, "any": 120},
                           min_request_interval_seconds=0)
    respond("getNumber", "ACCESS_NUMBER:777:914738485900")
    client.get_number(service="wa", country="91")

    res = client.cancel("777")
    check("window: cancel inside the window -> ACCESS_CANCEL_WAIT with the wait",
          res.get("type") == "ACCESS_CANCEL_WAIT"
          and 115 <= int(res.get("seconds", 0)) <= 120, res)
    check("window: the refusal is CLIENT-SIDE - no provider call was made",
          calls_for("setStatus") == [], calls_for("setStatus"))

    client._acquired_at["777"] = time.time() - 60
    res = client.cancel("777")
    check("window: the wait counts down from number issue",
          res.get("type") == "ACCESS_CANCEL_WAIT"
          and 55 <= int(res.get("seconds", 0)) <= 62, res)
    check("window: still no provider call", calls_for("setStatus") == [])

    client._acquired_at["777"] = time.time() - 300
    respond("setStatus", "ACCESS_CANCEL")
    res = client.cancel("777")
    check("window: the cancel goes to the provider once the window passed",
          res == {"type": "ACCESS_CANCEL"} and len(calls_for("setStatus")) == 1
          and calls_for("setStatus")[0]["params"].get("status") == 8,
          (res, calls_for("setStatus")))
    check("window: issue time dropped once closed",
          "777" not in client._acquired_at, client._acquired_at)
    check("window: unknown activation -> no local block (provider decides)",
          OtpSellClient(api_key="k", min_request_interval_seconds=0)
          .cancel_window_remaining("nope") == 0.0)

    # Even when our window bookkeeping says go, the provider may still say
    # wait: its own ACCESS_CANCEL_WAIT is honoured too.
    client2 = OtpSellClient(api_key="k", cancel_wait_seconds=0,
                            min_request_interval_seconds=0)
    respond("getNumber", "ACCESS_NUMBER:778:914700000000")
    client2.get_number()
    respond("setStatus", "ACCESS_CANCEL_WAIT:45")
    res = client2.cancel("778")
    check("window: provider-side ACCESS_CANCEL_WAIT:<s> wins",
          res.get("type") == "ACCESS_CANCEL_WAIT" and res.get("seconds") == 45, res)


# --- factory + selection validation -------------------------------------------

def sell_config(api_key="sell-key"):
    return {
        "active_otp_provider": "all",
        "tempora": {"enabled": True, "api_key": "t-key"},
        "otp": {"enabled": True, "api_key": "o-key"},
        "otpcart": {"enabled": True, "token": "c-token"},
        "vsimpro": {"enabled": True, "api_key": "v-key"},
        "otpindia": {"enabled": True, "api_key": "i-key"},
        "otpsell": {"enabled": True, "api_key": api_key,
                    "service": "wa", "country": "91", "operator": "any"},
    }


def test_create_clients():
    cfg = sell_config()

    names = [c.name for c in create_otp_clients(cfg)]
    check("factory: 'all' includes otpsell", "otpsell" in names, names)
    check("factory: all providers present",
          set(names) == {"tempora", "otpdoctor", "otpcart", "vsimpro",
                         "otpindia", "otpsell"}, names)

    clients = create_otp_clients(cfg, provider_override="otpsell")
    check("factory: explicit otpsell selection",
          len(clients) == 1 and clients[0].name == "otpsell",
          [c.name for c in clients])
    c = clients[0]
    check("factory: otpsell client configured from config.json",
          c.api_key == "sell-key" and c.default_service == "wa"
          and c.default_country == "91" and c.default_operator == "any"
          and c.base_url == "https://otpsell.com/stubs/handler_api.php",
          (c.api_key, c.default_service, c.default_country,
           c.default_operator, c.base_url))
    check("factory: otpsell cancel window defaults to 2 minutes",
          c.cancel_wait_seconds == 120.0, c.cancel_wait_seconds)
    check("factory: otpsell advertises its cancel window to the coordinator",
          getattr(c, "has_cancel_window", False) is True)

    tuned = sell_config()
    tuned["otpsell"]["cancel_wait_seconds"] = {"default": 120, "2": 60}
    tuned["otpsell"]["operators"] = ["1", "2"]
    c2 = create_otp_clients(tuned, provider_override="otpsell")[0]
    check("factory: per-operator windows + rotation from config.json",
          c2.operator_pool == ["1", "2"]
          and c2._window_for_operator("2") == 60.0
          and c2._window_for_operator("1") == 120.0
          and c2._window_for_operator("other") == 120.0,
          (c2.operator_pool, c2._cancel_windows))

    clients = create_otp_clients(cfg, provider_override="sell")
    check("factory: 'sell' alias maps to otpsell",
          len(clients) == 1 and clients[0].name == "otpsell",
          [c.name for c in clients])

    clients = create_otp_clients(cfg, provider_override="vsi,sell")
    check("factory: comma selection with alias",
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
    check("validate: alias ok", ok and err is None, (ok, err))

    ok, err = validate_provider_selection(cfg, "bogus")
    check("validate: unknown provider rejected (message lists otpsell)",
          not ok and "Unknown provider 'bogus'" in err and "otpsell" in err,
          (ok, err))

    ok, err = validate_provider_selection(no_key, "otpsell")
    check("validate: missing api_key rejected",
          not ok and "otpsell" in err and "api_key" in err, (ok, err))


# --- coordinator integration ----------------------------------------------------

SCENARIO_SEQ = [0]


def build_coordinator(**automation):
    SCENARIO_SEQ[0] += 1
    case_dir = os.path.join(SCRATCH_DIR, f"case_{SCENARIO_SEQ[0]}")
    os.makedirs(case_dir, exist_ok=True)
    os.chdir(case_dir)
    config = {
        "active_otp_provider": "tempora",
        "tempora": {"enabled": True, "api_key": "k", "max_attempts": 3},
        "checker": {"mode": "api", "api_keys": ["k"], "service": "meesho"},
        "automation": {
            "max_attempts": 3,
            "refund_check_delay_seconds": 0,
            "cancel_salvage_delay": 0,
            "cancel_salvage_probes": 0,
        },
        "balance_guard": {
            "enabled": True,
            "tolerance": 0.5,
            "refund_wait_seconds": 1,
            "poll_interval_seconds": 0.1,
        },
        "telegram": {"enabled": False},
        "termux": {"enabled": False},
    }
    config["automation"].update(automation)
    coordinator = m.ParallelAutomationCoordinator(config, provider_override="tempora")
    coordinator.alerts = []
    coordinator.messages = []
    coordinator.notify.send = lambda title, message, **kw: coordinator.messages.append((title, message))
    coordinator.notify.alert = lambda title, message, **kw: coordinator.alerts.append((title, message))
    coordinator.notify.telegram.send = lambda *a, **k: False
    coordinator.notify.termux.send = lambda *a, **k: False
    coordinator.stopped = []
    coordinator._critical_stop = lambda title, message: (
        coordinator.stopped.append((title, message)),
        coordinator.stop_requested.set(),
    )
    return coordinator


def wait_until(condition, timeout=25.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


def test_worker_get_number_branch():
    """The worker's otpsell branch forwards config service/country/operator/max_price."""
    reset_http()
    coordinator = build_coordinator()
    coordinator.config["otpsell"] = {
        "enabled": True, "api_key": "sell-key",
        "service": "wa", "country": "91", "operator": "1", "max_price": 7,
    }
    config = coordinator.config
    client = create_otp_clients(config, provider_override="otpsell")[0]

    sell_conf = config.get("otpsell", {})
    respond("getNumber", "ACCESS_NUMBER:77:914700000001")
    res = client.get_number(service=sell_conf.get("service"),
                            country=sell_conf.get("country"),
                            operator=sell_conf.get("operator"),
                            max_price=sell_conf.get("max_price"))
    params = last_call()["params"]
    check("worker: otpsell getNumber uses the config block",
          res["type"] == "ACCESS_NUMBER"
          and params.get("service") == "wa" and params.get("country") == "91"
          and params.get("operator") == "1" and params.get("maxPrice") == 7,
          (res, params))


def test_otp_wait_covers_otpsell_window():
    """The OTP wait on OTPSell covers the serving operator's cancel window."""
    reset_http()
    coordinator = build_coordinator(
        otp_timeout_seconds=0.2, otp_poll_interval_seconds=0.05,
        timeout_salvage_probes=0, otp_wait_covers_cancel_window=True,
    )
    real = OtpSellClient(api_key="k", cancel_wait_seconds=0.7,
                         min_request_interval_seconds=0)
    respond("getNumber", "ACCESS_NUMBER:601:919800000006")
    real.get_number(service="wa", country="91", operator="1")
    respond("getStatus", *([ "STATUS_WAIT_CODE" ] * 3))
    ctx = m.NumberContext(real, "601", "919800000006", "9800000006")
    started = time.time()
    kind, _ = coordinator.wait_for_otp(ctx)
    elapsed = time.time() - started
    check("otp wait: stretched to OTPSell's cancel window from getNumber",
          kind == "timeout" and 0.6 <= elapsed < 3.0, (kind, round(elapsed, 2)))
    coordinator.stop_requested.set()


def test_deferred_cancel_after_window_resolves_quietly():
    """
    A cancel inside an OTPSell operator window is refused client-side (no
    provider call), deferred with the window as the retry horizon, retried by
    the watcher once the window passes, and the refund is tallied - quietly
    (routine window provider, no per-number notifications).
    """
    reset_http()
    coordinator = build_coordinator(
        cancel_error_expiry_seconds=600,     # high on purpose: the WINDOW must win
        cancel_error_grace_seconds=0,
        cancel_error_poll_interval_seconds=0.05,
        cancel_error_retry_attempts=3,
        cancel_error_retry_delay_seconds=0.1,
        refund_check_delay_seconds=0,
    )
    client = OtpSellClient(api_key="k", default_operator="1",
                           cancel_wait_seconds={"default": 120, "1": 1.0},
                           min_request_interval_seconds=0)
    coordinator.clients = [client]
    coordinator.client_registry["otpsell"] = client
    coordinator._note_balance("otpsell", 100.0)

    respond("getNumber", "ACCESS_NUMBER:900:914700000900")
    respond("getStatus", "STATUS_WAIT_CODE")           # polls during the wait
    respond("setStatus", "ACCESS_CANCEL")              # the retry after 1s
    respond("getBalance", "ACCESS_BALANCE:100")        # refund landed

    res = client.get_number(service="wa", country="91")
    act = res["activation_id"]
    coordinator._note_activation("otpsell", act, "94700000900", operator=res.get("operator"))

    started = time.time()
    result = coordinator.handle_cancellation(client, act, "94700000900",
                                             "Already registered on Meesho")
    elapsed = time.time() - started

    check("sell window: deferred immediately (the worker is not blocked)",
          elapsed < 5.0 and result.get("deferred") is True
          and result.get("tally_ok") is True, result)
    check("sell window: no setStatus call inside the window (client-side refusal)",
          calls_for("setStatus") == [], calls_for("setStatus"))
    check("sell window: no CRITICAL STOP while the money is held",
          not coordinator.stopped, coordinator.stopped)

    record = coordinator.pending_cancels.store.get(act)
    horizon = (float(record.get("expiry_at", 0))
               - float(record.get("deferred_at_epoch", 0))) if record else -1
    check("sell window: retry horizon is the operator window (~1s), not the expiry",
          record is not None and not record.get("expiry_assumed")
          and 0.5 <= horizon <= 5, horizon)
    check("sell window: routine deferral sends no notification",
          not any("Cancel refused - deferred" in title
                  for title, _ in coordinator.messages), coordinator.messages)

    resolved = wait_until(
        lambda: coordinator.pending_cancels.store.get(act) is None, timeout=30)
    check("sell window: the watcher finished the cancellation",
          resolved and coordinator.pending_cancels.store.get(act) is None,
          coordinator.pending_cancels.pending())
    check("sell window: exactly ONE setStatus - the retry after the window",
          len(calls_for("setStatus")) == 1
          and calls_for("setStatus")[0]["params"].get("status") == 8,
          calls_for("setStatus"))
    snapshot = coordinator.stats.snapshot()
    check("sell window: the refund tallied and was counted",
          snapshot["cancel_deferred_refunded"] == 1
          and snapshot["refunds_missing"] == 0
          and not coordinator.stopped, snapshot)
    check("sell window: completion is routine too (no per-number notify)",
          not any("Deferred cancel completed" in title
                  for title, _ in coordinator.messages), coordinator.messages)
    check("sell window: audit trail closed (nothing left open)",
          coordinator.refund_ledger.open_activations("otpsell") == [],
          coordinator.refund_ledger.open_activations("otpsell"))

    coordinator.stop_requested.set()


def test_no_balance_wait_gate():
    reset_http()
    coordinator = build_coordinator()
    sell_client = OtpSellClient(api_key="k", min_request_interval_seconds=0)
    check("no balance: OTPSell with nothing pending -> ordinary handling",
          coordinator._wait_for_pending_otpindia_refunds(sell_client) is False)

    class Plain(object):
        name = "tempora"

    check("no balance: providers without a cancel window never take this path",
          coordinator._wait_for_pending_otpindia_refunds(Plain()) is False)
    coordinator.stop_requested.set()


# --- SQLite refund ledger -------------------------------------------------------

def test_ledger_expected_math_and_persistence():
    case_dir = os.path.join(SCRATCH_DIR, "ledger_direct")
    os.makedirs(case_dir, exist_ok=True)
    path = os.path.join(case_dir, "ledger.db")

    ledger = RefundLedger(filename=path)
    at_rest = ledger.note_balance("otpsell", 100.0, source="seed", holds=0.0)
    check("ledger: at-rest baseline returned and stored",
          at_rest == 100.0 and ledger.baseline("otpsell")["baseline"] == 100.0,
          (at_rest, ledger.baseline("otpsell")))

    # The crux: a balance noted while holds are open means at-rest = B + H.
    at_rest = ledger.note_balance("otpsell", 90.0, source="settle", holds=10.0)
    check("ledger: settle with open holds keeps the at-rest balance",
          at_rest == 100.0, at_rest)
    check("ledger: expected() deducts only the OTHER still-open holds",
          ledger.expected("otpsell", holds_excluded=10.0) == 100.0 - 10.0 + 10.0 - 10.0
          and ledger.expected("otpsell", holds_excluded=0.0) == 100.0,
          (ledger.expected("otpsell", holds_excluded=10.0),
           ledger.expected("otpsell", holds_excluded=0.0)))

    # Activation audit trail.
    ledger.record_purchase("otpsell", "act-1", number="123", operator="1")
    ledger.mark_deferred("otpsell", "act-1", hold=5.0, reason="window")
    opens = ledger.open_activations("otpsell")
    check("ledger: purchases tracked as open until resolved",
          len(opens) == 1 and opens[0]["state"] == "DEFERRED"
          and opens[0]["hold"] == 5.0 and opens[0]["operator"] == "1", opens)
    ledger.mark_resolved("otpsell", "act-1", outcome="REFUNDED")
    check("ledger: resolved activations leave the open set",
          ledger.open_activations("otpsell") == [],
          ledger.open_activations("otpsell"))

    # Restart persistence: the baseline (and its holds) survive a reopen.
    ledger.close()
    ledger2 = RefundLedger(filename=path)
    row = ledger2.baseline("otpsell")
    check("ledger: baseline + holds survive a restart",
          row is not None and row["baseline"] == 90.0 and row["holds"] == 10.0,
          row)
    check("ledger: observation audit trail kept",
          len(ledger2.recent_observations("otpsell")) == 2,
          ledger2.recent_observations("otpsell"))
    ledger2.close()


def test_ledger_thread_safety_smoke():
    case_dir = os.path.join(SCRATCH_DIR, "ledger_threads")
    os.makedirs(case_dir, exist_ok=True)
    path = os.path.join(case_dir, "ledger.db")
    ledger = RefundLedger(filename=path)

    threads = []
    notes_per_thread = 25
    for t in range(4):
        def work(idx=t):
            for i in range(notes_per_thread):
                ledger.note_balance("otpsell", 100.0 - i, source=f"t{idx}",
                                    holds=float(i % 3))
        th = threading.Thread(target=work)
        threads.append(th)
        th.start()
    for th in threads:
        th.join()

    row = ledger.baseline("otpsell")
    check("ledger: concurrent notes never lose a write (seq counts them all)",
          row is not None and row["seq"] == 4 * notes_per_thread, row)
    ledger.close()


def test_refund_race_regression():
    """
    The OTPIndia parallel refund race, fixed.

    Fast buying while cancels mature: settle #1 lands while cancel B is still
    pending. The OLD ledger stored the raw balance (95) as the new baseline
    and deducted B's hold again at every later tally - understating each
    expectation by the held 5. If B's refund then NEVER arrived, its tally
    still 'passed': the money was silently gone.

    The SQLite ledger stores (baseline=95, baseline_holds=5) so the at-rest
    balance stays 100 and B's missing refund is DETECTED.
    """
    reset_http()
    coordinator = build_coordinator()
    pname = "tempora"

    class Provider(object):
        def __init__(self):
            self.name = pname
            self.balance = 100.0
        def get_balance(self):
            return self.balance

    provider = Provider()

    # Start clean: nothing held.
    provider.balance = 100.0
    coordinator._note_balance(pname, provider.get_balance(), source="seed")

    # Two buys race into deferred cancels; each holds 5.
    store = coordinator.pending_cancels.store
    now_ts = time.time()
    for act in ("race-a", "race-b"):
        provider.balance -= 5
        store.add({"provider": pname, "activation_id": act, "number": "9x" + act,
                   "hold": 5.0, "expiry_at": now_ts + 60,
                   "deferred_at_epoch": now_ts})
        coordinator.refund_ledger.mark_deferred(pname, act, hold=5.0)

    # A refunds (balance 95) and its watcher settles AFTER the record closed.
    store.remove("race-a")
    provider.balance = 95.0
    coordinator._note_balance(pname, provider.get_balance(), source="settle")

    check("race: the at-rest expectation never drifted (100, not 90)",
          coordinator._expected_balance(pname, "race-b") == 100.0,
          coordinator._expected_balance(pname, "race-b"))
    check("race: a NEW number is tallied against B's still-open hold",
          coordinator._expected_balance(pname, "new-number") == 95.0,
          coordinator._expected_balance(pname, "new-number"))
    check("race: raw baseline still visible for /status",
          coordinator._ledger(pname)["expected_balance"] == 95.0,
          coordinator._ledger(pname))

    # B's refund NEVER arrives: the tally must FAIL (old code: silent pass).
    coordinator.guard.refund_wait = 0.3
    coordinator.guard.poll_interval_seconds = 0.05
    expected = coordinator._expected_balance(pname, "race-b")
    ok, actual = coordinator.guard.verify_refund(
        provider, expected, activation_id="race-b", prefix=pname.upper())
    check("race: a refund that never lands is DETECTED (no silent loss)",
          ok is False and actual == 95.0, (ok, actual, expected))

    # ...and once it does land, the same expectation passes again.
    provider.balance = 100.0
    ok, actual = coordinator.guard.verify_refund(
        provider, expected, activation_id="race-b", prefix=pname.upper())
    check("race: the same expectation tallies once the refund lands",
          ok is True and actual == 100.0, (ok, actual))

    coordinator.stop_requested.set()


def test_cancel_expectation_with_parallel_buys():
    """
    The exact scenario: every number costs 10, balance starts at 100.

    Number A is stuck in its cancel window while THREE more numbers (B, C, D)
    are bought. When A's cancel finally lands, the tally must expect 70 -
    its 10 refund on top of the live 60 (= 100 at-rest minus the 30 still
    held by B, C, D). NOT 90 (the stale balance from when A was bought) and
    NOT 100 (30 is still out). Then the remaining cancels cascade exactly:
    B -> 80, C -> 90, D -> 100.
    """
    reset_http()
    coordinator = build_coordinator()
    pname = "tempora"
    PRICE = 10.0

    class Wallet(object):
        """Simulated provider account: balance moves on buys and refunds."""
        def __init__(self, balance):
            self.balance = balance
        def get_balance(self):
            return self.balance

    wallet = Wallet(100.0)
    store = coordinator.pending_cancels.store

    def defer_buy(act):
        """What handle_cancellation does when the window refuses the cancel."""
        wallet.balance -= PRICE                                   # getNumber
        expected = coordinator._expected_balance(pname, act)      # tally target
        balance_after_buy = wallet.get_balance()                  # defer reads it
        hold = max(0.0, round(expected - balance_after_buy, 6))
        store.add({"provider": pname, "activation_id": act, "number": "9x",
                   "hold": hold, "expiry_at": time.time() + 600,
                   "deferred_at_epoch": time.time()})
        coordinator.refund_ledger.mark_deferred(pname, act, hold=hold)
        return expected, hold

    def mature_cancel(act):
        """What the watcher does once the window passes."""
        target = coordinator._expected_balance(pname, act)
        wallet.balance += PRICE                                   # ACCESS_CANCEL -> refund lands
        tallied = wallet.get_balance()
        # Close first, settle second (the watcher's order).
        store.remove(act)
        coordinator._note_balance(pname, tallied, source="settle")
        return target, tallied

    # Seed: 100, nothing held.
    coordinator._note_balance(pname, wallet.get_balance(), source="seed")

    # A is bought (10 -> balance 90) and parked behind its cancel window.
    exp_a, hold_a = defer_buy("A")
    check("parallel buys: A's hold is its price (10)",
          exp_a == 100.0 and hold_a == 10.0, (exp_a, hold_a))

    # While A waits, three more numbers are bought (and parked the same way):
    defer_buy("B")          # balance 80
    defer_buy("C")          # balance 70
    defer_buy("D")          # balance 60
    check("parallel buys: seed baseline kept, 40 correctly still held",
          coordinator._expected_balance(pname, "anything") == 60.0
          and coordinator._ledger(pname)["expected_balance"] == 100.0,
          coordinator._ledger(pname))

    # A's cancel finally lands: expect 70 - not 90, not 100.
    target_a, tallied_a = mature_cancel("A")
    check("parallel buys: A's late cancel is tallied against 70 (not 90/100)",
          target_a == 70.0 and tallied_a == 70.0,
          (target_a, tallied_a))
    check("parallel buys: A's refund tallies exactly (guard would pass)",
          tallied_a >= target_a - coordinator.guard.tolerance)

    # The remaining three cascade exactly.
    target_b, tallied_b = mature_cancel("B")
    check("parallel buys: B -> 80", target_b == 80.0 and tallied_b == 80.0,
          (target_b, tallied_b))
    target_c, tallied_c = mature_cancel("C")
    check("parallel buys: C -> 90", target_c == 90.0 and tallied_c == 90.0,
          (target_c, tallied_c))
    target_d, tallied_d = mature_cancel("D")
    check("parallel buys: D -> 100", target_d == 100.0 and tallied_d == 100.0,
          (target_d, tallied_d))
    check("parallel buys: fully refunded - at-rest is the seed again",
          coordinator._expected_balance(pname, None) == 100.0,
          coordinator._expected_balance(pname, None))

    # Variant: the three others are consumed/refunded BEFORE A's window ends -
    # then A's own late cancel must expect the full 100 again.
    coordinator2 = build_coordinator()
    store2 = coordinator2.pending_cancels.store
    wallet2 = Wallet(100.0)
    coordinator2._note_balance(pname, wallet2.get_balance(), source="seed")
    wallet2.balance -= PRICE                      # A bought -> 90
    store2.add({"provider": pname, "activation_id": "A", "number": "9x",
                "hold": 10.0, "expiry_at": time.time() + 600,
                "deferred_at_epoch": time.time()})
    coordinator2.refund_ledger.mark_deferred(pname, "A", hold=10.0)
    for act in ("B", "C", "D"):
        # bought (down 10) and refunded right back (up 10): A keeps holding 10
        wallet2.balance -= PRICE
        wallet2.balance += PRICE                  # immediate refund (+ note)
        coordinator2._note_balance(pname, wallet2.get_balance(), source=f"{act}-tally")
    check("parallel buys: balance is 90 with A still holding 10",
          wallet2.get_balance() == 90.0, wallet2.get_balance())
    target_a2 = coordinator2._expected_balance(pname, "A")
    wallet2.balance += PRICE
    check("variant: A cancelled last expects the full 100",
          target_a2 == 100.0 and wallet2.get_balance() == 100.0,
          (target_a2, wallet2.get_balance()))

    coordinator.stop_requested.set()
    coordinator2.stop_requested.set()


# --- in-flight holds: the live-run failure mode ---------------------------------

class _WalletClient:
    """A minimal provider client backed by a mutable balance."""
    def __init__(self, name, balance):
        self.name = name
        self.balance = balance
        self.fail_balance = False
    def get_balance(self):
        if self.fail_balance:
            raise RuntimeError("balance unreadable")
        return self.balance


def test_inflight_holds_and_buy_pricing():
    """
    In-flight holds: numbers bought but not yet deferred/consumed.

    The OTPSell live run false-stopped because an expected balance was frozen
    while ~6 more buys happened during the tally's poll. The fix prices every
    purchase at buy-time (predicted live balance - balance right after
    getNumber) and counts that price as an IN-FLIGHT hold in every expected
    calculation. This checks the math end to end.
    """
    reset_http()
    coordinator = build_coordinator()
    pname = "tempora"
    wallet = _WalletClient(pname, 100.0)

    coordinator._note_balance(pname, wallet.get_balance(), source="seed")

    def buy(act):
        """What the worker does: price = balance before - after getNumber."""
        before = wallet.get_balance()
        wallet.balance -= 10.0                      # provider debits the number
        price = round(before - wallet.get_balance(), 6)
        coordinator._note_activation(pname, act, "9x" + act, hold=price)
        return before, price

    before_a, price_a = buy("A")
    check("in-flight: A priced at 10 across the buy",
          before_a == 100.0 and price_a == 10.0, (before_a, price_a))

    before_b, price_b = buy("B")
    check("in-flight: B priced at 10 while A is still in play",
          before_b == 90.0 and price_b == 10.0, (before_b, price_b))

    total, unknown = coordinator._hold_totals(pname)
    check("in-flight: two OPEN purchases hold 20, all known",
          total == 20.0 and unknown is False, (total, unknown))
    check("in-flight: an activation never pays its own hold twice",
          coordinator._hold_totals(pname, exclude="A") == (10.0, False),
          coordinator._hold_totals(pname, exclude="A"))

    before_c, price_c = buy("C")
    check("in-flight: C priced at 10 while A and B are still in play",
          before_c == 80.0 and price_c == 10.0, (before_c, price_c))
    check("in-flight: expected(A) = at-rest 100 - 20 held by others",
          coordinator._expected_balance(pname, "A") == 80.0,
          coordinator._expected_balance(pname, "A"))

    # B is consumed by the bot (charge stands): its hold must STOP counting.
    coordinator._mark_activation_closed(pname, "B", "CONSUMED")
    coordinator._note_balance(pname, wallet.get_balance(), source="consume")
    check("in-flight: B's charge booked against at-rest (100-10=90)",
          coordinator._expected_balance(pname, "X") == 70.0
          and coordinator._ledger(pname)["expected_balance"] == 70.0,
          coordinator._ledger(pname))

    # A's window refuses its cancel; the balance is unreadable. The recorded
    # purchase price becomes the deferred hold - never 'unknown', bookkeeping
    # stays exact.
    wallet.fail_balance = True
    res = coordinator._defer_cancellation(
        wallet, "A", "9xA", "Already registered on Meesho",
        coordinator._expected_balance(pname, "A"),
        cancel_res={"type": "ACCESS_CANCEL_WAIT", "seconds": 60},
    )
    record = coordinator.pending_cancels.store.get("A")
    check("in-flight: unreadable balance falls back to the measured price",
          res.get("deferred") is True and record is not None
          and record.get("hold") == 10.0, (res.get("deferred"), record))
    ledger_row = coordinator.refund_ledger.activation(pname, "A")
    check("in-flight: the deferred row left the OPEN set (no double count)",
          ledger_row is not None and ledger_row.get("state") == "DEFERRED",
          ledger_row)
    total_after, unknown_after = coordinator._hold_totals(pname)
    check("in-flight: A moved from in-flight to queued - total unchanged (20)",
          total_after == 20.0 and unknown_after is False,
          (total_after, unknown_after))
    check("in-flight: hold contents diagnosable for alerts",
          "in-flight holds 10.0000" in coordinator.hold_breakdown(pname)
          and "pending-cancel holds 10.0000" in coordinator.hold_breakdown(pname),
          coordinator.hold_breakdown(pname))

    coordinator.stop_requested.set()


def test_watcher_expected_rederived_while_buys_continue():
    """
    The exact live-run false stop, reproduced deterministically: A's deferred
    refund tally runs its poll WHILE another number is being bought. A FROZEN
    expectation compares the world-that-was against the balance-that-is and
    cries mismatch; the re-derived expectation drops with the new debit and
    the (correct) refund tallies cleanly - no critical stop.
    """
    reset_http()
    coordinator = build_coordinator()
    pname = "tempora"
    wallet = _WalletClient(pname, 100.0)
    coordinator._note_balance(pname, wallet.get_balance(), source="seed")

    # A is bought and parked as a deferred cancel (10 held).
    wallet.balance -= 10.0
    coordinator._note_activation(pname, "A", "9xA", hold=10.0)
    coordinator.pending_cancels.store.add({
        "provider": pname, "activation_id": "A", "number": "9xA",
        "hold": 10.0, "expiry_at": time.time() + 600,
        "deferred_at_epoch": time.time(),
    })
    coordinator.refund_ledger.mark_deferred(pname, "A", hold=10.0)

    # The watcher retries A's cancel: the refund LANDS (+10 -> 100), and the
    # expectation is frozen the way the old code froze it.
    wallet.balance += 10.0
    frozen = coordinator._expected_balance(pname, "A")
    check("watcher: frozen expectation is 100 while nothing else is in play",
          frozen == 100.0, frozen)

    bought = {"b": False}

    def expected_for_a():
        # What the live run did: another number is bought DURING the poll.
        # (Priced exactly like the worker: balance before - after getNumber.)
        if not bought["b"]:
            bought["b"] = True
            before = wallet.get_balance()
            wallet.balance -= 10.0
            coordinator._note_activation(
                pname, "B", "9xB",
                hold=round(before - wallet.get_balance(), 6))
        return coordinator._expected_balance(pname, "A")

    coordinator.guard.poll_interval_seconds = 0.05
    ok, actual = coordinator.guard.verify_refund(
        wallet, expected_for_a, activation_id="A", prefix=pname.upper(),
        refund_wait=2.0)
    check("watcher: refund tallies although a buy landed mid-poll",
          ok is True and actual == 90.0, (ok, actual))
    check("watcher: the buy was booked (B in-flight, expected(A) == 90)",
          coordinator._expected_balance(pname, "A") == 90.0,
          coordinator._expected_balance(pname, "A"))

    # A genuinely MISSING refund still fails - the re-derivation is not a
    # rubber stamp. A's 10 was never refunded and B now holds 10, so the
    # balance (80 after a phantom extra debit) can never reach 90.
    wallet.balance = 80.0
    ok2, actual2 = coordinator.guard.verify_refund(
        wallet, expected_for_a, activation_id="A", prefix=pname.upper())
    check("watcher: a real shortfall is still detected",
          ok2 is False and actual2 == 80.0, (ok2, actual2))

    coordinator.stop_requested.set()


def test_otp_wait_survives_critical_stop():
    """
    A paid in-flight OTP wait must never be sacrificed to a critical stop.
    The live incident: a false refund mismatch stopped the run, the 80s OTP
    wait aborted at second ~20, and the code that landed afterwards was
    charged but never seen. Now a critical stop only halts the BUYING - the
    active target's wait runs to its natural end, while /stop still aborts
    right away.
    """
    reset_http()

    class ScriptedClient:
        def __init__(self):
            self.name = "tempora"
            self.polls = 0
            self.stop_after = 3
            self.code_after = 5
            self.coordinator = None
        def get_status(self, activation_id):
            self.polls += 1
            if self.polls == self.stop_after and self.coordinator is not None:
                self.coordinator._stop_source = "critical"
                self.coordinator.stop_requested.set()
            if self.polls >= self.code_after:
                return {"type": "STATUS_OK", "code": "447191",
                        "sms": "Your Meesho OTP is 447191"}
            return {"type": "STATUS_WAIT_CODE"}

    coordinator = build_coordinator(
        otp_timeout_seconds=5.0, otp_poll_interval_seconds=0.05,
        timeout_salvage_probes=0, keep_otp_wait_on_critical_stop=True,
    )
    client = ScriptedClient()
    client.coordinator = coordinator
    ctx = m.NumberContext(client, "act-otp", "919123456789", "9123456789")
    with coordinator.active_target_lock:
        coordinator.active_target = ctx

    started = time.time()
    kind, status = coordinator.wait_for_otp(ctx)
    elapsed = time.time() - started
    check("otp wait: code arriving AFTER a critical stop is still delivered",
          kind == "ok" and (status or {}).get("code") == "447191"
          and client.polls >= 5, (kind, status, client.polls))
    check("otp wait: the wait outlived the stop instead of aborting",
          elapsed > 0.15, round(elapsed, 3))
    check("otp wait: no early 'timeout' misread after a stop",
          kind != "timeout", kind)

    # The toggle restores the old abort-on-any-stop behaviour.
    coordinator2 = build_coordinator(
        otp_timeout_seconds=5.0, otp_poll_interval_seconds=0.05,
        timeout_salvage_probes=0, keep_otp_wait_on_critical_stop=False,
    )
    client2 = ScriptedClient()
    client2.coordinator = coordinator2
    ctx2 = m.NumberContext(client2, "act-otp2", "919123456789", "9123456789")
    with coordinator2.active_target_lock:
        coordinator2.active_target = ctx2
    started2 = time.time()
    kind2, _ = coordinator2.wait_for_otp(ctx2)
    elapsed2 = time.time() - started2
    check("otp wait: toggle off -> the old immediate abort",
          kind2 == "timeout" and elapsed2 < 1.5 and client2.polls <= 4,
          (kind2, round(elapsed2, 3), client2.polls))

    # A USER /stop always aborts, protected or not.
    coordinator3 = build_coordinator(
        otp_timeout_seconds=5.0, otp_poll_interval_seconds=0.05,
        timeout_salvage_probes=0, keep_otp_wait_on_critical_stop=True,
    )
    client3 = ScriptedClient()
    client3.code_after = 50
    ctx3 = m.NumberContext(client3, "act-otp3", "919123456789", "9123456789")
    with coordinator3.active_target_lock:
        coordinator3.active_target = ctx3

    def user_stop():
        time.sleep(0.15)
        coordinator3._stop_source = "user"
        coordinator3.stop_requested.set()

    threading.Thread(target=user_stop, daemon=True).start()
    started3 = time.time()
    kind3, _ = coordinator3.wait_for_otp(ctx3)
    elapsed3 = time.time() - started3
    check("otp wait: /stop aborts immediately even for the active target",
          kind3 == "timeout" and elapsed3 < 2.0, (kind3, round(elapsed3, 3)))
    check("otp wait: /stop really came from the user path",
          coordinator3._stop_source == "user", coordinator3._stop_source)

    coordinator.stop_requested.set()
    coordinator2.stop_requested.set()
    coordinator3.stop_requested.set()


def test_ledger_open_hold_rows():
    """OPEN purchase rows carry the measured price and leave the open set."""
    case_dir = os.path.join(SCRATCH_DIR, "ledger_open_holds")
    os.makedirs(case_dir, exist_ok=True)
    ledger = RefundLedger(filename=os.path.join(case_dir, "ledger.db"))

    ledger.note_balance("otpsell", 100.0, source="seed", holds=0.0)
    ledger.record_purchase("otpsell", "a1", number="91x1", hold=10.0)
    ledger.record_purchase("otpsell", "a2", number="91x2", hold=None)
    total, unknown = ledger.open_hold_total("otpsell")
    check("ledger holds: one priced + one unpriced OPEN row",
          total == 10.0 and unknown is True, (total, unknown))

    row = ledger.activation("otpsell", "a1")
    check("ledger holds: activation() returns the priced OPEN row",
          row is not None and row.get("state") == "OPEN"
          and row.get("hold") == 10.0, row)
    check("ledger holds: unknown activation -> None",
          ledger.activation("otpsell", "nope") is None,
          ledger.activation("otpsell", "nope"))

    ledger.mark_deferred("otpsell", "a1", hold=10.0, reason="window")
    total2, unknown2 = ledger.open_hold_total("otpsell")
    check("ledger holds: DEFERRED rows do not count as in-flight",
          total2 == 0.0 and unknown2 is True, (total2, unknown2))

    ledger.mark_resolved("otpsell", "a2", outcome="CONSUMED")
    total3, unknown3 = ledger.open_hold_total("otpsell")
    check("ledger holds: resolved rows leave every tally",
          total3 == 0.0 and unknown3 is False, (total3, unknown3))
    ledger.close()


def test_dispute_evidence_artifact():
    """
    A refund mismatch leaves SELF-CONTAINED dispute evidence on disk (JSONL)
    - the mismatch plus the ledger picture at that moment - and /disputes
    surfaces the newest records for a quick look on the phone.
    """
    reset_http()
    coordinator = build_coordinator()
    pname = "tempora"
    coordinator._note_balance(pname, 100.0, source="seed")
    coordinator._note_activation(pname, "disp-1", "9xdisp", hold=10.0)

    entry = coordinator._append_dispute({
        "type": "refund_mismatch",
        "provider": pname,
        "activation_id": "disp-1",
        "number": "9xdisp",
        "reason": "Already registered on Meesho",
        "expected_balance": 100.0,
        "actual_balance": 90.0,
        "source": "immediate_cancel",
    })
    check("dispute: enriched with the ledger row, hold breakdown, observations",
          (entry.get("ledger_activation") or {}).get("state") == "OPEN"
          and (entry.get("ledger_activation") or {}).get("hold") == 10.0
          and "in-flight holds 10.0000" in (entry.get("hold_breakdown") or "")
          and len(entry.get("recent_balance_observations") or []) >= 1,
          (entry.get("ledger_activation"), entry.get("hold_breakdown")))
    path = coordinator.pending_cancels.disputes.path
    check("dispute: the record is stamped and refers to the instance file",
          entry.get("recorded_at") and entry.get("type") == "refund_mismatch",
          entry)
    check("dispute: evidence persisted on disk as JSONL",
          os.path.exists(path) and "refund_mismatch" in open(path).read(),
          path)

    reply = coordinator.command_disputes("")
    check("dispute: /disputes surfaces the newest record with its ledger line",
          "refund_mismatch" in reply and "disp-1" in reply
          and "in-flight holds" in reply, reply)
    check("dispute: /disputes honours the count and caps junk input",
          "refund_mismatch" in coordinator.command_disputes("1")
          and "refund_mismatch" in coordinator.command_disputes("nope"),
          "arg parsing")

    coordinator2 = build_coordinator()
    check("dispute: an empty log still answers cleanly",
          "No dispute records yet" in coordinator2.command_disputes(""),
          coordinator2.command_disputes(""))

    coordinator.stop_requested.set()
    coordinator2.stop_requested.set()


def test_stale_inflight_reconciliation():
    """
    Ghost in-flight holds get swept against the provider: rows left OPEN by
    an aborted run (or an older build that never closed them) keep their
    price in every tally forever unless they are closed against the truth.
    NO_ACTIVATION -> EXPIRED (money back), STATUS_CANCEL -> REFUNDED,
    STATUS_OK -> CONSUMED (charge stands), anything else -> left alone.
    """
    reset_http()
    coordinator = build_coordinator()
    pname = "tempora"
    coordinator._note_balance(pname, 100.0, source="seed")
    for act in ("stale-exp", "stale-use", "stale-live", "fresh"):
        coordinator._note_activation(pname, act, "9x" + act, hold=10.0)
    check("reconcile: four ghost/live buys hold 40",
          coordinator._hold_totals(pname) == (40.0, False),
          coordinator._hold_totals(pname))

    class StatusClient:
        name = pname
        def get_status(self, activation_id):
            return {
                "stale-exp": {"type": "NO_ACTIVATION"},
                "stale-use": {"type": "STATUS_OK", "code": "111223"},
                "stale-live": {"type": "STATUS_WAIT_CODE"},
                "fresh": {"type": "STATUS_CANCEL"},
            }.get(activation_id)

    client = StatusClient()
    coordinator._reconcile_inflight(client, stale_seconds=0.0, force=True)
    ledger = coordinator.refund_ledger
    check("reconcile: NO_ACTIVATION row closed as EXPIRED (money back)",
          (ledger.activation(pname, "stale-exp") or {}).get("state") == "EXPIRED",
          ledger.activation(pname, "stale-exp"))
    check("reconcile: STATUS_OK row closed as CONSUMED (charge stands)",
          (ledger.activation(pname, "stale-use") or {}).get("state") == "CONSUMED",
          ledger.activation(pname, "stale-use"))
    check("reconcile: WAIT_CODE row left alone (no verdict yet)",
          (ledger.activation(pname, "stale-live") or {}).get("state") == "OPEN",
          ledger.activation(pname, "stale-live"))
    check("reconcile: STATUS_CANCEL row closed as REFUNDED (money back)",
          (ledger.activation(pname, "fresh") or {}).get("state") == "REFUNDED",
          ledger.activation(pname, "fresh"))
    check("reconcile: only the undecided row still holds",
          coordinator._hold_totals(pname) == (10.0, False),
          coordinator._hold_totals(pname))

    # The next sweep closes what the provider has settled by then.
    client.get_status = lambda aid: {"type": "STATUS_CANCEL"}
    coordinator._reconcile_inflight(client, stale_seconds=0.0, force=True)
    check("reconcile: a later sweep closes the settled remainder as REFUNDED",
          (ledger.activation(pname, "stale-live") or {}).get("state") == "REFUNDED"
          and (ledger.activation(pname, "fresh") or {}).get("state") == "REFUNDED",
          (ledger.activation(pname, "stale-live"), ledger.activation(pname, "fresh")))
    check("reconcile: holds fully swept clean",
          coordinator._hold_totals(pname) == (0.0, False),
          coordinator._hold_totals(pname))

    coordinator.stop_requested.set()


def test_refund_last_chance_confirmation():
    """
    The last-chance re-check: a refund that lands right as the wait expires
    (or a derived expectation that was inflated by ghost holds) is confirmed
    against a FRESH expectation instead of critical-stopping the run - and a
    genuinely missing refund still fails.
    """
    reset_http()
    coordinator = build_coordinator()
    pname = "tempora"
    coordinator._note_balance(pname, 100.0, source="seed")
    coordinator._note_activation(pname, "X", "9xX", hold=10.0)
    coordinator.pending_cancels.store.add({
        "provider": pname, "activation_id": "X", "number": "9xX",
        "hold": 10.0, "expiry_at": time.time() + 600,
        "deferred_at_epoch": time.time(),
    })
    coordinator.refund_ledger.mark_deferred(pname, "X", hold=10.0)

    class RisingWallet:
        name = pname
        def __init__(self):
            self.calls = 0
        def get_balance(self):
            self.calls += 1
            return 100.0 if self.calls >= 2 else 90.0
        def get_status(self, activation_id):
            return {"type": "STATUS_WAIT_CODE"}

    wallet = RisingWallet()
    ok, bal = coordinator._confirm_refund_once(wallet, "X", probes=3, delay=0.01)
    check("last-chance: a refund landing mid-confirm is accepted (no stop)",
          ok is True and bal == 100.0 and wallet.calls == 2, (ok, bal, wallet.calls))

    class FlatWallet:
        name = pname
        def __init__(self):
            self.calls = 0
        def get_balance(self):
            self.calls += 1
            return 90.0
        def get_status(self, activation_id):
            return {"type": "STATUS_WAIT_CODE"}

    flat = FlatWallet()
    ok2, bal2 = coordinator._confirm_refund_once(flat, "X", probes=3, delay=0.01)
    check("last-chance: a genuinely missing refund still fails",
          ok2 is False and bal2 == 90.0 and flat.calls == 3, (ok2, bal2, flat.calls))

    coordinator.stop_requested.set()


def main():
    print("=" * 60)
    print("OTPSELL PROVIDER + SQLITE REFUND LEDGER CHECK")
    print("=" * 60)

    test_balance()
    test_catalogs()
    test_get_number()
    test_status_and_setstatus()
    test_cancel_window_parsing()
    test_operator_rotation_pool()
    test_cancel_blocked_inside_window_client_side()
    test_create_clients()
    test_validate_selection()
    test_worker_get_number_branch()
    test_otp_wait_covers_otpsell_window()
    test_deferred_cancel_after_window_resolves_quietly()
    test_no_balance_wait_gate()
    test_ledger_expected_math_and_persistence()
    test_ledger_thread_safety_smoke()
    test_refund_race_regression()
    test_cancel_expectation_with_parallel_buys()
    test_inflight_holds_and_buy_pricing()
    test_watcher_expected_rederived_while_buys_continue()
    test_otp_wait_survives_critical_stop()
    test_ledger_open_hold_rows()
    test_dispute_evidence_artifact()
    test_stale_inflight_reconciliation()
    test_refund_last_chance_confirmation()

    os.chdir(REPO_DIR)
    print("=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
        return 1
    print("ALL CHECKS PASSED")
    shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
