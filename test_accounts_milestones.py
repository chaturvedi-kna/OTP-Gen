"""
Linked-account ledger, milestones and Telegram verbosity.

Runs without network access (requests/websocket are stubbed) and checks:

  * LinkedAccountsStore keeps one record per linked account (10-digit
    number, provider, time), counts per provider, and stores milestones
    with the user's semantics: "/milestone <last number shared> <note>"
    means everything up to and including that number is "before", the
    rest is "new" (unknown numbers are rejected, the last occurrence of a
    repeated number wins, the last milestone can be removed),
  * the coordinator records every linked account from the PRIMES bot
    result and /accounts, /accounts list, /milestone answer with the
    totals, the count since the last milestone and the first / latest
    number after it - per Telegram instance (accounts.<instance>.json),
  * the Telegram menu registers /accounts, /linked, /milestone and /notify
    and hands their arguments to the coordinator (long lists are chunked),
  * Notifier levels: all / normal / quiet decide which tiers reach Telegram
    (routine < important < linked = critical); the default is "all",
    "OTP requested" style routine messages go out silent, 🎉 Account linked
    always goes out with sound, and /notify persists the level into the
    top-level telegram block of config.json (never an instance's block).

    python test_accounts_milestones.py
"""

import json
import os
import shutil
import sys
import tempfile
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = tempfile.mkdtemp(prefix="accounts_check_")

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

from accounts import LinkedAccountsStore, clean_number, looks_like_number  # noqa: E402
from notifier import (  # noqa: E402
    NOTIFY_LEVELS,
    NOTIFY_TIERS,
    Notifier,
    TelegramBackend,
    normalize_notify_level,
)
import main as m  # noqa: E402


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def scratch(name):
    return os.path.join(SCRATCH_DIR, name)


def headline_total(reply):
    """'👤 Linked accounts (tempora): 3' -> 3."""
    first = reply.splitlines()[0]
    return int(first.rsplit(":", 1)[1].strip())


# --- the ledger ----------------------------------------------------------------

def test_store_basics():
    check("store: number normalised to 10 digits (+91)", clean_number("+91 98765 43210") == "9876543210")
    check("store: number normalised to 10 digits (0 prefix)", clean_number("09876543210") == "9876543210")
    check("store: looks_like_number accepts a phone", looks_like_number("919876543210"))
    check("store: looks_like_number rejects a note", not looks_like_number("10 used"))

    store = LinkedAccountsStore(filename=scratch("accounts.json"))
    check("store: starts empty", store.total() == 0 and store.milestones() == [])

    records = [("919800000001", "tempora"), ("9800000002", "vsimpro"),
               ("09800000003", "tempora"), ("9800000004", "otpindia"),
               ("9800000005", "tempora")]
    for number, provider in records:
        store.add(number, provider, user_id="uid", account_number=1)

    check("store: every link recorded", store.total() == 5, store.total())
    check("store: numbers stored normalised",
          [r["number"] for r in store.accounts()][:3] == ["9800000001", "9800000002", "9800000003"],
          [r["number"] for r in store.accounts()])
    check("store: per-provider counts",
          store.count_by_provider(store.accounts()) == {"tempora": 3, "vsimpro": 1, "otpindia": 1},
          store.count_by_provider(store.accounts()))
    check("store: seq is 1-based and increasing",
          [r["seq"] for r in store.accounts()] == [1, 2, 3, 4, 5])

    reloaded = LinkedAccountsStore(filename=scratch("accounts.json"))
    check("store: survives a reload", reloaded.total() == 5 and reloaded.accounts()[-1]["number"] == "9800000005")

    other = LinkedAccountsStore(filename=scratch("accounts.vsimpro.json"))
    check("store: a second instance has its own ledger", other.total() == 0)


def test_store_milestones():
    store = LinkedAccountsStore(filename=scratch("accounts.milestones.json"))
    for number, provider in [("9800000001", "tempora"), ("9800000002", "vsimpro"),
                             ("9800000003", "tempora"), ("9800000004", "otpindia")]:
        store.add(number, provider)

    check("milestone: none yet -> since() is everything", len(store.since()) == 4)

    try:
        store.add_milestone("9111111111", "typo")
        rejected = False
    except ValueError:
        rejected = True
    check("milestone: unknown number is rejected", rejected)

    milestone = store.add_milestone("+919800000002", "10 used + 40 shared")
    check("milestone: cut is the given number (inclusive)",
          milestone["through_seq"] == 2 and milestone["last_number"] == "9800000002", milestone)
    check("milestone: total up to the cut", milestone["total"] == 2, milestone)
    check("milestone: per-provider up to the cut",
          milestone["by_provider"] == {"tempora": 1, "vsimpro": 1}, milestone["by_provider"])
    check("milestone: note kept", milestone["note"] == "10 used + 40 shared")

    fresh = store.since()
    check("milestone: since() = numbers after the cut",
          [r["number"] for r in fresh] == ["9800000003", "9800000004"], [r["number"] for r in fresh])

    # A number linked twice (re-run) - the LAST occurrence is the cut.
    store.add("9800000002", "vsimpro")
    second = store.add_milestone("9800000002", "again")
    check("milestone: repeated number -> last occurrence wins",
          second["through_seq"] == 5 and second["total"] == 5, second)
    check("milestone: nothing after the newest cut", store.since() == [])

    removed = store.remove_last_milestone()
    check("milestone: remove drops the last one",
          removed["seq"] == 2 and len(store.milestones()) == 1, (removed, store.milestones()))
    check("milestone: since() falls back to the previous milestone",
          [r["number"] for r in store.since()] == ["9800000003", "9800000004", "9800000002"],
          [r["number"] for r in store.since()])

    reloaded = LinkedAccountsStore(filename=scratch("accounts.milestones.json"))
    check("milestone: persisted with the ledger",
          len(reloaded.milestones()) == 1 and reloaded.last_milestone()["through_seq"] == 2)


# --- coordinator commands -----------------------------------------------------

def build_coordinator(instance=None):
    config = json.load(open(os.path.join(REPO_DIR, "config.json"), encoding="utf-8"))
    config["active_otp_provider"] = "tempora"
    config["telegram"]["enabled"] = False
    config["termux"]["enabled"] = False
    config["instances"] = {"vsimpro": {"telegram": {"bot_token": "T2", "chat_id": "C2"}}}
    os.chdir(SCRATCH_DIR)
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    kwargs = {"provider_override": "tempora"}
    if instance:
        kwargs["instance"] = instance
    coordinator = m.ParallelAutomationCoordinator(config, **kwargs)
    coordinator.sent = []
    coordinator.notify.send = lambda title, message, **kw: coordinator.sent.append((title, message, kw))
    coordinator.notify.alert = lambda title, message, **kw: coordinator.sent.append((title, message, kw))
    coordinator.run = lambda *a, **k: None
    return coordinator


class FakeClient:
    name = "tempora"

    def finish(self, activation_id):
        return True

    def get_balance(self):
        return 42.0


class FakeBot:
    def __init__(self, result):
        self.result = result

    def submit_otp(self, code):
        return dict(self.result)

    def return_to_menu(self):
        return True


def link_via_bot(coordinator, number, provider="tempora", user_id="U1", account_number=7):
    context = types.SimpleNamespace(
        clean_number=number, provider_name=provider, provider=provider,
        activation_id="A" + number[-4:], client=FakeClient(), late=False,
    )
    fake = FakeBot({"status": "linked", "user_id": user_id, "account_number": account_number})
    # Keep the real bot object (status summary reads .ready etc.), only script
    # the two calls the linked branch makes.
    coordinator.bot.submit_otp = fake.submit_otp
    coordinator.bot.return_to_menu = fake.return_to_menu
    return coordinator._bot_submit_code(context, "123456", "Your OTP is 123456")


def test_coordinator_records_and_reports():
    for name in ("accounts.json", "accounts.vsimpro.json", "stats.json", "stats.vsimpro.json"):
        try:
            os.remove(scratch(name))
        except FileNotFoundError:
            pass
    coordinator = build_coordinator()

    reply = coordinator.command_accounts("")
    check("accounts cmd: empty ledger explains itself",
          headline_total(reply) == 0 and "No milestone yet" in reply, reply)

    status = link_via_bot(coordinator, "9800000001", "tempora")
    check("linked: bot result still classified as linked", status == "linked", status)
    check("linked: ledger records the account",
          coordinator.accounts.total() == 1
          and coordinator.accounts.accounts()[0]["provider"] == "tempora"
          and coordinator.accounts.accounts()[0]["meesho_user_id"] == "U1",
          coordinator.accounts.accounts())
    linked_msgs = [s for s in coordinator.sent if "Account linked" in s[0]]
    check("linked: notification sent on the 'linked' tier with sound",
          len(linked_msgs) == 1 and linked_msgs[0][2].get("level") == "linked"
          and not linked_msgs[0][2].get("silent"), linked_msgs)
    check("linked: notification is short and carries per-provider totals",
          "TEMPORA 1" in linked_msgs[0][1] and linked_msgs[0][1].count("\n") <= 3, linked_msgs[0][1])

    link_via_bot(coordinator, "9800000002", "vsimpro")
    link_via_bot(coordinator, "9800000003", "tempora")

    status_text = coordinator.get_status_summary()
    check("status: per-provider linked counts",
          "Accounts linked: 3 (TEMPORA 2, VSIMPRO 1)" in status_text, status_text)

    reply = coordinator.command_accounts("")
    check("accounts cmd: totals per provider",
          headline_total(reply) == 3 and "TEMPORA: 2" in reply and "VSIMPRO: 1" in reply, reply)

    # Accounts linked before the ledger existed are only in the counter.
    coordinator.stats.increment("accounts_linked")
    coordinator.stats.increment("accounts_linked")
    reply = coordinator.command_accounts("")
    check("accounts cmd: counter-only accounts shown as untracked",
          headline_total(reply) == 5 and "before per-provider tracking: 2" in reply, reply)
    coordinator.stats.increment("accounts_linked", -2)

    reply = coordinator.command_milestone("9800000002 10 used + 40 shared")
    check("milestone cmd: accepted",
          reply.startswith("✅") and "9800000002" in reply and "2 linked" in reply
          and "After it: 1" in reply, reply)

    link_via_bot(coordinator, "9800000004", "otpindia")
    link_via_bot(coordinator, "9800000005", "vsimpro")

    reply = coordinator.command_accounts("")
    check("accounts cmd: count since the milestone",
          "Since then: 3" in reply and "TEMPORA 1" in reply and "OTPINDIA 1" in reply
          and "VSIMPRO 1" in reply, reply)
    check("accounts cmd: first number after the milestone",
          "First: `9800000003`" in reply, reply)
    check("accounts cmd: last number till now",
          "Last:  `9800000005`" in reply, reply)
    check("accounts cmd: milestone note and cut shown",
          "10 used + 40 shared" in reply and "up to 9800000002 = 2" in reply, reply)

    listing = coordinator.command_accounts("list")
    rows = listing.splitlines()[1:]
    check("accounts list: exactly the numbers after the cut",
          [r.split("`")[1] for r in rows] == ["9800000003", "9800000004", "9800000005"], listing)

    reply = coordinator.command_milestone("")
    check("milestone cmd: bare shows the milestones",
          "Milestones (1)" in reply and "Since #1: 3" in reply, reply)

    reply = coordinator.command_milestone("9111111111 typo")
    check("milestone cmd: unknown number rejected with a hint",
          reply.startswith("❌") and "not in this instance" in reply and "9800000005" in reply, reply)
    reply = coordinator.command_milestone("shared 40")
    check("milestone cmd: usage shown when the first word is not a number",
          reply.startswith("❌") and "Usage" in reply, reply)

    reply = coordinator.command_milestone("remove")
    check("milestone cmd: remove drops the last milestone",
          reply.startswith("🗑") and coordinator.accounts.milestones() == [], reply)
    reply = coordinator.command_milestone("remove")
    check("milestone cmd: nothing left to remove", reply.startswith("❌"), reply)

    # Per instance: a second tab keeps its own ledger next to its stats file.
    other = build_coordinator(instance="vsimpro")
    check("instance: ledger file namespaced",
          str(other.accounts.path).endswith("accounts.vsimpro.json"), other.accounts.path)
    check("instance: starts from its own (empty) ledger",
          other.accounts.total() == 0 and "(vsimpro): 0" in other.command_accounts(""),
          other.command_accounts(""))
    check("instance: /accounts names the instance",
          "(vsimpro)" in other.command_accounts(""), other.command_accounts(""))


# --- Telegram wiring -------------------------------------------------------------

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


def run_command(text, **replies):
    backend = FakeUpdates(text, token="t", chat_id="1")
    received = {}

    def make(name):
        def callback(arg=""):
            received[name] = arg
            return replies.get(name, f"{name} reply")
        return callback

    backend.accounts_callback = make("accounts")
    backend.milestone_callback = make("milestone")
    backend.notify_callback = make("notify")
    backend.poll_signal({"run": "run"})
    sent = [p for _m, p in backend.sent if _m == "sendMessage"]
    return received, sent


def test_telegram_commands():
    received, sent = run_command("/accounts")
    check("telegram: /accounts calls the accounts callback", received.get("accounts") == "", received)
    check("telegram: /accounts reply delivered",
          any("accounts reply" in (p.get("text") or "") for p in sent), sent)

    received, sent = run_command("/linked list")
    check("telegram: /linked alias passes the argument", received.get("accounts") == "list", received)

    received, sent = run_command("/milestone 9876543210 10 used + 40 shared")
    check("telegram: /milestone passes number and note",
          received.get("milestone") == "9876543210 10 used + 40 shared", received)

    received, sent = run_command("/notify quiet")
    check("telegram: /notify passes the level", received.get("notify") == "quiet", received)

    long_reply = "\n".join(f"`98000{i:05d}`  TEMPORA  01 Jan 00:00" for i in range(200))
    received, sent = run_command("/accounts list", accounts=long_reply)
    check("telegram: long /accounts list is chunked under the Telegram limit",
          len(sent) >= 2 and all(len(p.get("text") or "") <= 4096 for p in sent),
          [len(p.get("text") or "") for p in sent])
    check("telegram: chunks keep every line",
          "".join(p.get("text") or "" for p in sent).count("TEMPORA") == 200)

    received, sent = run_command("/start")
    joined = "\n".join(p.get("text") or "" for p in sent)
    check("telegram: /start documents the new commands",
          "/accounts" in joined and "/milestone" in joined and "/notify" in joined, joined)

    backend = FakeUpdates("/start", token="t", chat_id="1")
    notifier = Notifier({"telegram": {"enabled": True}, "termux": {"enabled": False}})
    notifier.telegram = backend
    notifier.set_command_callbacks(
        status_cb=lambda: "status", balance_cb=lambda: "balance",
        run_cb=lambda arg: "run", stop_cb=lambda: None,
        referral_cb=lambda arg: "referral", checker_cb=lambda arg: "checker",
        accounts_cb=lambda arg: "accounts", milestone_cb=lambda arg: "milestone",
        notify_cb=lambda arg: "notify",
    )
    registrations = [payload for method, payload in backend.sent if method == "setMyCommands"]
    registered = {item["command"]: item["description"]
                  for item in registrations[-1]["commands"]} if registrations else {}
    check("telegram: menu registers accounts/milestone/notify",
          {"accounts", "milestone", "notify"} <= set(registered), registered)
    check("telegram: callbacks wired on the backend",
          backend.accounts_callback is not None and backend.milestone_callback is not None
          and backend.notify_callback is not None)


# --- verbosity levels -------------------------------------------------------------

class RecordingTelegram:
    def __init__(self):
        self.sent = []
        self.last_error = None

    def send(self, title, message, buttons=None, silent=False):
        self.sent.append({"title": title, "silent": silent})
        return True


def notifier_with(level=None, silent_routine=None):
    telegram = {"enabled": True, "bot_token": "t", "chat_id": "1"}
    if level is not None:
        telegram["notify_level"] = level
    if silent_routine is not None:
        telegram["silent_routine"] = silent_routine
    notifier = Notifier({"telegram": telegram, "termux": {"enabled": False}})
    notifier.telegram = RecordingTelegram()
    return notifier


def fire_all(notifier):
    notifier.send("📲 OTP requested via PRIMES bot", "x", level="routine", silent=True)
    notifier.send("🔄 Changing number", "x", level="routine")
    notifier.send("⏳ Cancel refused - deferred", "x", level="routine")
    notifier.alert("❌ Wrong OTP (#1)", "x", level="important")
    notifier.alert("⚠️ OTP Timed Out", "x", level="important")
    notifier.alert("🎉 Account linked! (#1)", "x", level="linked")
    notifier.alert("🛑 REFUND DID NOT TALLY", "x")
    return [s["title"] for s in notifier.telegram.sent]


def test_notify_levels():
    check("levels: ordering routine < important < linked = critical",
          NOTIFY_TIERS["routine"] > NOTIFY_TIERS["important"] > NOTIFY_TIERS["linked"]
          and NOTIFY_TIERS["linked"] == NOTIFY_TIERS["critical"], NOTIFY_TIERS)
    check("levels: the three switch values", set(NOTIFY_LEVELS) == {"all", "normal", "quiet"}, NOTIFY_LEVELS)
    check("levels: normalize accepts aliases",
          normalize_notify_level("ALL") == "all" and normalize_notify_level("silent") == "quiet"
          and normalize_notify_level("bogus", default=None) is None)

    default = notifier_with()
    check("levels: default is all", default.level == "all", default.level)
    titles = fire_all(default)
    check("levels: all -> nothing dropped", len(titles) == 7, titles)
    silent = {s["title"]: s["silent"] for s in default.telegram.sent}
    check("levels: OTP requested goes out silent", silent["📲 OTP requested via PRIMES bot"] is True, silent)
    check("levels: routine tier silent by default",
          silent["🔄 Changing number"] is True and silent["⏳ Cancel refused - deferred"] is True, silent)
    check("levels: Account linked keeps its sound", silent["🎉 Account linked! (#1)"] is False, silent)
    check("levels: important/critical keep their sound",
          silent["❌ Wrong OTP (#1)"] is False and silent["🛑 REFUND DID NOT TALLY"] is False, silent)

    loud = notifier_with(silent_routine=False)
    fire_all(loud)
    silent = {s["title"]: s["silent"] for s in loud.telegram.sent}
    check("levels: silent_routine=false restores the sound (except the explicit OTP requested)",
          silent["🔄 Changing number"] is False and silent["📲 OTP requested via PRIMES bot"] is True, silent)

    normal = notifier_with(level="normal")
    titles = fire_all(normal)
    check("levels: normal drops routine only",
          titles == ["❌ Wrong OTP (#1)", "⚠️ OTP Timed Out", "🎉 Account linked! (#1)",
                     "🛑 REFUND DID NOT TALLY"], titles)
    check("levels: muted messages counted", normal.muted_count == 3, normal.muted_count)

    quiet = notifier_with(level="quiet")
    titles = fire_all(quiet)
    check("levels: quiet keeps Account linked + critical",
          titles == ["🎉 Account linked! (#1)", "🛑 REFUND DID NOT TALLY"], titles)

    quiet.set_level("all")
    quiet.telegram.sent.clear()
    check("levels: set_level switches live", len(fire_all(quiet)) == 7)
    try:
        quiet.set_level("loud")
        bad = False
    except ValueError:
        bad = True
    check("levels: set_level rejects unknown values", bad)
    check("levels: describe_levels names every level",
          all(word in quiet.describe_levels() for word in ("all", "normal", "quiet")), quiet.describe_levels())


def test_notify_command_persists():
    coordinator = build_coordinator()
    coordinator.notify.telegram = RecordingTelegram()

    reply = coordinator.command_notify_level("")
    check("notify cmd: bare shows the level", "ALL" in reply and "/notify all|normal|quiet" in reply, reply)

    reply = coordinator.command_notify_level("bogus")
    check("notify cmd: unknown level rejected", reply.startswith("❌") and coordinator.notify.level == "all", reply)

    reply = coordinator.command_notify_level("quiet")
    check("notify cmd: level switched", reply.startswith("✅") and coordinator.notify.level == "quiet", reply)
    saved = json.load(open(scratch("config.json"), encoding="utf-8"))
    check("notify cmd: saved to the top-level telegram block",
          saved["telegram"]["notify_level"] == "quiet", saved["telegram"])
    check("notify cmd: the instance's telegram block is untouched",
          saved["instances"]["vsimpro"]["telegram"] == {"bot_token": "T2", "chat_id": "C2"},
          saved["instances"])
    check("notify cmd: config keys and order preserved",
          list(saved["telegram"])[:3] == ["enabled", "bot_token", "chat_id"], list(saved["telegram"]))

    coordinator.command_notify_level("normal")
    saved = json.load(open(scratch("config.json"), encoding="utf-8"))
    check("notify cmd: rewriting replaces the value", saved["telegram"]["notify_level"] == "normal")

    fresh = Notifier(saved)
    check("notify cmd: a restart picks the saved level up", fresh.level == "normal", fresh.level)


def main():
    print("=" * 60)
    print("LINKED ACCOUNTS / MILESTONES / NOTIFY LEVELS CHECK")
    print("=" * 60)
    try:
        test_store_basics()
        test_store_milestones()
        test_coordinator_records_and_reports()
        test_telegram_commands()
        test_notify_levels()
        test_notify_command_persists()
    finally:
        os.chdir(REPO_DIR)
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
