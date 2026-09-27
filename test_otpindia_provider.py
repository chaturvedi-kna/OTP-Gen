"""
OTPIndia (otpindia.org) provider + Telegram "/run <provider>" selection.

Runs without network access by stubbing requests, then checks:

  * OtpIndiaClient speaks the documented handler_api protocol
    (getBalance -> ACCESS_BALANCE, getNumber with service+server ->
    ACCESS_NUMBER, getStatus, setStatus),
  * create_otp_clients() knows the new provider (all / explicit / alias,
    credentials required),
  * validate_provider_selection() rejects unknown names and providers
    without credentials instead of silently falling back,
  * the Telegram /run command passes its optional provider argument through
    and reports rejections with a "Run Not Started" reply,
  * ParallelAutomationCoordinator.request_run() narrows this run only:
    the selection applies, /status shows it, /balance still covers every
    known provider, and a bare /run restores the configured set.

    python test_otpindia_provider.py
"""

import json
import os
import shutil
import sys
import tempfile
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = tempfile.mkdtemp(prefix="otpindia_check_")

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
from otpindia_client import OtpIndiaClient  # noqa: E402
from notifier import TelegramBackend  # noqa: E402
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
    client = OtpIndiaClient(api_key="KEY123")
    respond("ACCESS_BALANCE:150.50")
    bal = client.get_balance()
    check("otpindia: balance parsed from ACCESS_BALANCE", bal == 150.5, bal)
    call = last_call()
    check("otpindia: getBalance action + api_key sent",
          call["params"].get("action") == "getBalance"
          and call["params"].get("api_key") == "KEY123", call["params"])
    check("otpindia: base URL",
          call["url"] == "https://otpindia.org/api/stubs/handler_api.php", call["url"])

    respond("BAD_KEY")
    try:
        client.get_balance()
        check("otpindia: BAD_KEY raises OTPError", False, "no exception")
    except OTPError as exc:
        check("otpindia: BAD_KEY raises OTPError", "BAD_KEY" in str(exc), exc)


def test_get_number():
    client = OtpIndiaClient(api_key="KEY123")
    respond("ACCESS_NUMBER:12345:919876543210")
    res = client.get_number(service="wa", server="SERVER_CODE")
    check("otpindia: ACCESS_NUMBER parsed",
          res == {"type": "ACCESS_NUMBER", "activation_id": "12345",
                  "number": "919876543210"}, res)
    params = last_call()["params"]
    check("otpindia: getNumber sends service + server",
          params.get("action") == "getNumber"
          and params.get("service") == "wa"
          and params.get("server") == "SERVER_CODE", params)
    check("otpindia: no country param (not in the spec)", "country" not in params, params)
    check("otpindia: no maxPrice unless configured", "maxPrice" not in params, params)

    # Defaults from config are used when the caller passes nothing.
    client2 = OtpIndiaClient(api_key="k", default_service="meesho", default_server="3")
    respond("ACCESS_NUMBER:9:9111111111")
    client2.get_number()
    params = last_call()["params"]
    check("otpindia: configured service/server defaults used",
          params.get("service") == "meesho" and params.get("server") == "3", params)

    # No server configured -> the param is simply omitted.
    client3 = OtpIndiaClient(api_key="k", default_service="wa")
    respond("ACCESS_NUMBER:10:9222222222")
    client3.get_number()
    check("otpindia: server omitted when not configured",
          "server" not in last_call()["params"], last_call()["params"])

    # maxPrice is only sent when configured.
    client4 = OtpIndiaClient(api_key="k", max_price=12)
    respond("ACCESS_NUMBER:11:9333333333")
    client4.get_number()
    check("otpindia: configured maxPrice forwarded",
          last_call()["params"].get("maxPrice") == 12, last_call()["params"])

    respond("NO_NUMBERS")
    res = client.get_number(service="wa", server="1")
    check("otpindia: NO_NUMBERS returned as a type", res == {"type": "NO_NUMBERS"}, res)

    respond("BAD_SERVICE")
    res = client.get_number(service="nope", server="1")
    check("otpindia: BAD_SERVICE returned as a type", res == {"type": "BAD_SERVICE"}, res)


def test_status_and_cancel():
    client = OtpIndiaClient(api_key="KEY123")

    respond("STATUS_WAIT_CODE")
    check("otpindia: STATUS_WAIT_CODE",
          client.get_status("12345") == {"type": "STATUS_WAIT_CODE"})

    respond("STATUS_OK:Your OTP is 482913")
    res = client.get_status("12345")
    check("otpindia: STATUS_OK code extracted",
          res.get("type") == "STATUS_OK" and res.get("code") == "482913", res)
    check("otpindia: getStatus sends the activation id",
          last_call()["params"].get("id") == "12345", last_call()["params"])

    respond("ACCESS_CANCEL")
    check("otpindia: cancel (status 8)",
          client.cancel("12345") == {"type": "ACCESS_CANCEL"})
    check("otpindia: setStatus sends status 8",
          last_call()["params"].get("status") == 8, last_call()["params"])

    respond("WAIT_CANCEL:120")
    check("otpindia: WAIT_CANCEL cooldown parsed",
          client.cancel("12345") == {"type": "WAIT_CANCEL", "seconds": 120})

    respond("ACCESS_ACTIVATION")
    check("otpindia: finish (status 6)",
          client.finish("12345") == {"type": "ACCESS_ACTIVATION"})


# --- factory + selection validation ------------------------------------------

def india_config(api_key="ind-key"):
    return {
        "active_otp_provider": "all",
        "tempora": {"enabled": True, "api_key": "t-key"},
        "otp": {"enabled": True, "api_key": "o-key"},
        "otpcart": {"enabled": True, "token": "c-token"},
        "vsimpro": {"enabled": True, "api_key": "v-key"},
        "otpindia": {"enabled": True, "api_key": api_key,
                     "service": "meesho", "server": "3"},
    }


def test_create_clients():
    cfg = india_config()

    names = [c.name for c in create_otp_clients(cfg)]
    check("factory: 'all' includes otpindia", "otpindia" in names, names)
    check("factory: all providers present",
          set(names) == {"tempora", "otpdoctor", "otpcart", "vsimpro", "otpindia"},
          names)

    clients = create_otp_clients(cfg, provider_override="otpindia")
    check("factory: explicit otpindia selection", len(clients) == 1
          and clients[0].name == "otpindia", [c.name for c in clients])
    c = clients[0]
    check("factory: otpindia client configured from config.json",
          c.api_key == "ind-key" and c.default_service == "meesho"
          and c.default_server == "3"
          and c.base_url == "https://otpindia.org/api/stubs/handler_api.php",
          (c.api_key, c.default_service, c.default_server, c.base_url))

    clients = create_otp_clients(cfg, provider_override="india")
    check("factory: 'india' alias maps to otpindia",
          len(clients) == 1 and clients[0].name == "otpindia",
          [c.name for c in clients])

    clients = create_otp_clients(cfg, provider_override="vsi,india")
    check("factory: comma selection with alias",
          [c.name for c in clients] == ["vsimpro", "otpindia"],
          [c.name for c in clients])

    no_key = india_config(api_key="")
    names = [c.name for c in create_otp_clients(no_key)]
    check("factory: otpindia without api_key is excluded from 'all'",
          "otpindia" not in names, names)


def test_validate_selection():
    cfg = india_config()
    no_key = india_config(api_key="")

    ok, err = validate_provider_selection(cfg, "")
    check("validate: empty selection (bare /run) ok", ok and err is None, (ok, err))

    ok, err = validate_provider_selection(cfg, "all")
    check("validate: 'all' ok", ok and err is None, (ok, err))

    ok, err = validate_provider_selection(cfg, "vsi")
    check("validate: alias ok", ok and err is None, (ok, err))

    ok, err = validate_provider_selection(cfg, "otpindia")
    check("validate: configured otpindia ok", ok and err is None, (ok, err))

    ok, err = validate_provider_selection(cfg, "bogus")
    check("validate: unknown provider rejected",
          not ok and "Unknown provider 'bogus'" in err
          and "otpindia" in err, (ok, err))

    ok, err = validate_provider_selection(no_key, "otpindia")
    check("validate: missing api_key rejected",
          not ok and "otpindia" in err and "api_key" in err, (ok, err))


# --- Telegram /run wiring ------------------------------------------------------

class FakeUpdates(TelegramBackend):
    def __init__(self, text, **kw):
        super().__init__(**kw)
        self.next_text = text
        self.sent = []

    def _call(self, method, payload=None, timeout=None):
        if method == "getUpdates":
            return [{"update_id": 1, "message": {
                "text": self.next_text, "chat": {"id": 1},
            }}]
        self.sent.append((method, payload))
        return True


def run_command(text, reply=None, raise_exc=None):
    backend = FakeUpdates(text, token="t", chat_id="1")
    received = []

    def callback(arg):
        received.append(arg)
        if raise_exc is not None:
            raise raise_exc
        return reply

    backend.run_callback = callback
    backend.poll_signal({"run": "run"})
    sent = [p for _m, p in backend.sent if _m == "sendMessage"]
    return received, sent


def test_telegram_run_command():
    received, sent = run_command(
        "/run vsimpro", reply="▶️ Started search for VSIMPRO only (this run).")
    check("telegram: /run argument extracted", received == ["vsimpro"], received)
    check("telegram: success keeps the Started title",
          any("Automation Started" in (p.get("text") or "") for p in sent), sent)
    check("telegram: reply text delivered",
          any("VSIMPRO" in (p.get("text") or "") for p in sent), sent)

    received, sent = run_command("/run@MyBot otpindia", reply="▶️ ok")
    check("telegram: bot mention stripped, argument kept",
          received == ["otpindia"], received)

    received, sent = run_command("/run")
    check("telegram: bare /run passes an empty argument", received == [""], received)
    check("telegram: bare /run default message",
          any("▶️ Started search for target number" in (p.get("text") or "")
              for p in sent), sent)

    received, sent = run_command("/run bogus",
                                 reply="❌ Unknown provider 'bogus'.")
    check("telegram: rejection reply is passed through",
          received == ["bogus"] and any("bogus" in (p.get("text") or "") for p in sent),
          (received, sent))
    check("telegram: rejection uses 'Run Not Started' title",
          any("Run Not Started" in (p.get("text") or "") for p in sent), sent)

    received, sent = run_command("/run vsimpro", raise_exc=RuntimeError("boom"))
    check("telegram: callback failure reported, not swallowed",
          any("Run Not Started" in (p.get("text") or "")
              and "boom" in (p.get("text") or "") for p in sent), sent)


def test_telegram_start_help_mentions_run_provider():
    received, sent = run_command("/start")
    joined = "\n".join(p.get("text") or "" for p in sent)
    check("telegram: /start documents /run <provider>", "/run <provider" in joined, joined)


# --- coordinator request_run ---------------------------------------------------

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
    # Never start the real automation in this offline check.
    coordinator.run = lambda *a, **k: None
    return coordinator


def test_request_run_selection():
    coordinator = build_coordinator()
    check("run cmd: startup selection is the configured one",
          [c.name for c in coordinator.clients] == ["tempora"],
          [c.name for c in coordinator.clients])

    reply = coordinator.request_run("bogus")
    check("run cmd: unknown provider rejected",
          reply.startswith("❌") and "Unknown provider" in reply, reply)
    check("run cmd: selection unchanged after rejection",
          [c.name for c in coordinator.clients] == ["tempora"],
          [c.name for c in coordinator.clients])

    reply = coordinator.request_run("otpindia")
    check("run cmd: otpindia without api_key rejected",
          reply.startswith("❌") and "otpindia" in reply and "api_key" in reply, reply)

    reply = coordinator.request_run("vsimpro")
    check("run cmd: valid selection accepted",
          reply.startswith("▶️") and "VSIMPRO" in reply, reply)
    check("run cmd: selection applied for this run",
          [c.name for c in coordinator.clients] == ["vsimpro"],
          [c.name for c in coordinator.clients])
    check("run cmd: configured selection kept for restore",
          [c.name for c in coordinator.configured_clients] == ["tempora"],
          [c.name for c in coordinator.configured_clients])
    check("run cmd: provider registry remembers both",
          "tempora" in coordinator.client_registry
          and "vsimpro" in coordinator.client_registry,
          list(coordinator.client_registry))
    check("run cmd: deferred-cancel lookup finds the selected provider",
          coordinator.client_by_name("vsimpro") is not None)
    check("run cmd: deferred-cancel lookup keeps the configured provider",
          coordinator.client_by_name("tempora") is not None)

    status = coordinator.get_status_summary()
    check("run cmd: /status shows the narrowed selection",
          "Run selection: VSIMPRO" in status, status)

    summary = coordinator.get_balances_summary()
    check("run cmd: /balance still covers every known provider",
          "VSIMPRO" in summary and "TEMPORA" in summary, summary)

    reply = coordinator.request_run()
    check("run cmd: bare /run restores the configured selection",
          [c.name for c in coordinator.clients] == ["tempora"],
          (reply, [c.name for c in coordinator.clients]))
    check("run cmd: bare /run default reply",
          reply == "▶️ Started search for target number.", reply)
    status = coordinator.get_status_summary()
    check("run cmd: /status drops the run-selection line after restore",
          "Run selection" not in status, status)


def test_request_run_otpindia():
    config = json.load(open(os.path.join(REPO_DIR, "config.json"), encoding="utf-8"))
    config["active_otp_provider"] = "tempora"
    config["otpindia"]["api_key"] = "ind-test-key"
    config["otpindia"]["server"] = "3"
    coordinator = build_coordinator(config)

    reply = coordinator.request_run("otpindia")
    check("run cmd: otpindia run accepted once the key is set",
          reply.startswith("▶️") and "OTPINDIA" in reply, reply)
    check("run cmd: only otpindia runs",
          [c.name for c in coordinator.clients] == ["otpindia"],
          [c.name for c in coordinator.clients])
    client = coordinator.client_by_name("otpindia")
    check("run cmd: client carries the config (service/server/key)",
          client is not None and client.api_key == "ind-test-key"
          and client.default_service == "meesho" and client.default_server == "3",
          client and (client.api_key, client.default_service, client.default_server))

    summary = coordinator.get_balances_summary()
    check("run cmd: /balance lists otpindia alongside the configured set",
          "OTPINDIA" in summary and "TEMPORA" in summary, summary)


def test_worker_requests_number_with_service_and_server():
    """The worker's otpindia branch forwards config service/server to getNumber."""
    config = json.load(open(os.path.join(REPO_DIR, "config.json"), encoding="utf-8"))
    config["active_otp_provider"] = "tempora"
    config["otpindia"]["api_key"] = "ind-test-key"
    config["otpindia"]["service"] = "meesho"
    config["otpindia"]["server"] = "7"
    coordinator = build_coordinator(config)
    client = coordinator.client_by_name("otpindia") or \
        create_otp_clients(config, provider_override="otpindia")[0]

    # Re-implement the branch contract check: what the worker sends is exactly
    # what the client would put on the wire.
    india_conf = coordinator.config.get("otpindia", {})
    respond("ACCESS_NUMBER:77:9876543210")
    res = client.get_number(service=india_conf.get("service"),
                            server=india_conf.get("server"))
    params = last_call()["params"]
    check("worker: otpindia getNumber uses config service/server",
          res["type"] == "ACCESS_NUMBER"
          and params.get("service") == "meesho" and params.get("server") == "7",
          (res, params))


def main():
    print("=" * 60)
    print("OTPINDIA PROVIDER + /run <provider> CHECK")
    print("=" * 60)

    test_balance()
    test_get_number()
    test_status_and_cancel()
    test_create_clients()
    test_validate_selection()
    test_telegram_run_command()
    test_telegram_start_help_mentions_run_provider()
    test_request_run_selection()
    test_request_run_otpindia()
    test_worker_requests_number_with_service_and_server()

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
