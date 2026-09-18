"""
Parallel runs must not corrupt each other's files.

The usual setup is two Termux tabs - one for TemporaSMS, one for VSImpro -
running `python main.py --provider tempora` and `python main.py --provider
vsimpro`. Before, both wrote the same stats.json / state.json / .signals/, so
the counters merged, one tab's recovery record overwrote the other's, and the
manual-trigger file signals crossed tabs.

This check drives the real coordinator twice (two instances, one scratch
directory) and verifies that every runtime artefact is separated, and that an
optional config["instances"][name] block is merged for that instance only.

    python test_instance_isolation.py
"""

import json
import os
import shutil
import sys
import tempfile
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = tempfile.mkdtemp(prefix="instance_isolation_check_")

if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)


# --- stub the optional runtime dependencies (no network in this check) -------

if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        requests = types.ModuleType("requests")

        class _Session:
            def get(self, *a, **k):
                raise RuntimeError("network disabled in this check")

            def post(self, *a, **k):
                raise RuntimeError("network disabled in this check")

        requests.Session = _Session
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

import runtime  # noqa: E402
import main as m  # noqa: E402


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


BASE_CONFIG = {
    "active_otp_provider": "tempora,vsimpro",
    "tempora": {"enabled": True, "api_key": "k", "max_attempts": 5},
    "vsimpro": {"enabled": True, "api_key": "k", "max_attempts": 5},
    "checker": {"mode": "api", "api_keys": ["k"], "service": "meesho"},
    "automation": {"max_attempts": 5},
    "telegram": {"enabled": False},
    "termux": {"enabled": False},
}


def config_with_instances():
    config = json.loads(json.dumps(BASE_CONFIG))
    config["instances"] = {
        "tempora": {
            "meesho_bot": {"session_file": "userbot.tempora.session.txt"},
            "telegram": {"chat_id": "111"},
        },
        "vsimpro": {
            "meesho_bot": {"session_file": "userbot.vsimpro.session.txt"},
            "telegram": {"chat_id": "222"},
        },
    }
    return config


# ---------------------------------------------------------------------------
# 1. name derivation
# ---------------------------------------------------------------------------

def scenario_derive_instance():
    check("derive: --provider tempora -> instance 'tempora'",
          runtime.instance_from_provider("tempora") == "tempora",
          runtime.instance_from_provider("tempora"))
    check("derive: --provider vsimpro -> instance 'vsimpro'",
          runtime.instance_from_provider("vsimpro") == "vsimpro",
          runtime.instance_from_provider("vsimpro"))
    check("derive: two providers give no instance (one process, many providers)",
          runtime.instance_from_provider("tempora,vsimpro") == "",
          runtime.instance_from_provider("tempora,vsimpro"))
    check("derive: 'all' gives no instance",
          runtime.instance_from_provider("all") == "")
    check("derive: no --provider gives no instance",
          runtime.instance_from_provider(None) == "")
    check("derive: --instance wins over --provider",
          runtime.resolve_instance("tab1", "tempora") == "tab1")
    check("derive: an odd instance name is sanitised",
          runtime.normalize_instance("Tab 1/!!") == "Tab-1",
          runtime.normalize_instance("Tab 1/!!"))


def scenario_file_names():
    check("files: stats.json -> stats.tempora.json",
          runtime.stats_filename("tempora") == "stats.tempora.json",
          runtime.stats_filename("tempora"))
    check("files: state.json -> state.tempora.json",
          runtime.state_filename("tempora") == "state.tempora.json")
    check("files: pending_cancels.json -> pending_cancels.tempora.json",
          runtime.pending_filename("tempora") == "pending_cancels.tempora.json")
    check("files: .signals -> .signals-tempora",
          runtime.signal_dirname("tempora") == ".signals-tempora")
    check("files: no instance keeps the legacy names",
          runtime.stats_filename("") == "stats.json"
          and runtime.state_filename(None) == "state.json"
          and runtime.signal_dirname("") == ".signals")


def scenario_instance_overrides():
    config = config_with_instances()
    tempora = runtime.apply_instance_overrides(config, "tempora")
    vsimpro = runtime.apply_instance_overrides(config, "vsimpro")
    plain = runtime.apply_instance_overrides(config, None)

    check("overrides: the instance block is merged over the config",
          tempora["telegram"]["chat_id"] == "111", tempora["telegram"])
    check("overrides: another instance keeps its own values",
          vsimpro["telegram"]["chat_id"] == "222", vsimpro["telegram"])
    check("overrides: untouched keys survive the merge",
          tempora["telegram"].get("enabled") is False
          and tempora["checker"]["mode"] == "api")
    check("overrides: the instances block itself is stripped",
          "instances" not in tempora and "instances" not in plain)
    check("overrides: no instance leaves the config untouched",
          plain == {k: v for k, v in config.items() if k != "instances"} or
          plain["telegram"].get("chat_id") is None,
          plain.get("telegram"))
    check("overrides: an unknown instance changes nothing",
          runtime.apply_instance_overrides(config, "otpcart")["telegram"].get("chat_id") is None)


# ---------------------------------------------------------------------------
# 2. two coordinators, one directory
# ---------------------------------------------------------------------------

def scenario_two_coordinators():
    os.chdir(SCRATCH_DIR)
    config = config_with_instances()

    tempora = m.ParallelAutomationCoordinator(config, provider_override="tempora")
    vsimpro = m.ParallelAutomationCoordinator(
        json.loads(json.dumps(config)), provider_override="vsimpro", instance="vsimpro")

    check("two runs: each derives its own instance",
          tempora.instance == "tempora" and vsimpro.instance == "vsimpro",
          f"{tempora.instance} / {vsimpro.instance}")
    check("two runs: separate stats files",
          tempora.stats.path.name == "stats.tempora.json"
          and vsimpro.stats.path.name == "stats.vsimpro.json",
          f"{tempora.stats.path.name} / {vsimpro.stats.path.name}")
    check("two runs: separate state files",
          tempora.state.path.name == "state.tempora.json"
          and vsimpro.state.path.name == "state.vsimpro.json")
    check("two runs: separate deferred-cancel files",
          tempora.pending_cancels.store.path == "pending_cancels.tempora.json"
          and vsimpro.pending_cancels.store.path == "pending_cancels.vsimpro.json",
          tempora.pending_cancels.store.path)
    check("two runs: separate signal directories",
          tempora.notify.signal_dir.name == ".signals-tempora"
          and vsimpro.notify.signal_dir.name == ".signals-vsimpro",
          tempora.notify.signal_dir.name)

    # Counters must not leak between the two runs.
    tempora.stats.increment("targets_found", 3)
    vsimpro.stats.increment("targets_found", 7)
    reloaded_tempora = m.StatsStore(filename=runtime.stats_filename("tempora"))
    reloaded_vsimpro = m.StatsStore(filename=runtime.stats_filename("vsimpro"))
    check("two runs: counters are not merged",
          reloaded_tempora.snapshot()["targets_found"] == 3
          and reloaded_vsimpro.snapshot()["targets_found"] == 7,
          f"{reloaded_tempora.snapshot()['targets_found']} / "
          f"{reloaded_vsimpro.snapshot()['targets_found']}")

    # The instance block must reach the pieces it configures.
    check("two runs: the instance block reaches the userbot session file",
          tempora.bot.session_file == "userbot.tempora.session.txt"
          and vsimpro.bot.session_file == "userbot.vsimpro.session.txt",
          f"{tempora.bot.session_file} / {vsimpro.bot.session_file}")
    check("two runs: the instance block reaches the Telegram chat",
          tempora.notify.telegram.chat_id == "111"
          and vsimpro.notify.telegram.chat_id == "222")

    # Termux notification ids are namespaced (tabs must not replace each
    # other's notification) - and the namespacing is idempotent.
    check("two runs: notification ids are namespaced",
          tempora.notify.notif_id("meesho-otp") == "meesho-otp-tempora"
          and vsimpro.notify.notif_id("meesho-otp") == "meesho-otp-vsimpro",
          tempora.notify.notif_id("meesho-otp"))
    check("two runs: notification namespacing is idempotent",
          tempora.notify.notif_id("meesho-otp-tempora") == "meesho-otp-tempora")
    check("two runs: the default instance keeps plain ids",
          m.Notifier({"telegram": {"enabled": False}, "termux": {"enabled": False}})
          .notif_id("meesho-otp") == "meesho-otp")

    state_tempora = {"status": "ACQUIRED", "number": "9999999999"}
    state_vsimpro = {"status": "ACQUIRED", "number": "8888888888"}
    tempora.state.save(state_tempora)
    vsimpro.state.save(state_vsimpro)
    check("two runs: the state of one does not overwrite the other",
          tempora.state.load()["number"] == "9999999999"
          and vsimpro.state.load()["number"] == "8888888888",
          f"{tempora.state.load()} / {vsimpro.state.load()}")

    files = sorted(os.listdir(SCRATCH_DIR))
    check("two runs: no shared stats.json/state.json was created",
          "stats.json" not in files and "state.json" not in files, files)


def scenario_status_shows_instance():
    os.chdir(SCRATCH_DIR)
    coordinator = m.ParallelAutomationCoordinator(
        json.loads(json.dumps(BASE_CONFIG)), provider_override="tempora")
    status = coordinator.get_status_summary()
    check("status: the instance is reported",
          "Instance: tempora" in status, status.splitlines()[:4])
    plain = m.ParallelAutomationCoordinator(
        json.loads(json.dumps(BASE_CONFIG)), provider_override="tempora,vsimpro")
    check("status: no instance line for a shared (multi-provider) run",
          "Instance:" not in plain.get_status_summary())


def main():
    scenario_derive_instance()
    scenario_file_names()
    scenario_instance_overrides()
    scenario_two_coordinators()
    scenario_status_shows_instance()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All instance isolation checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
