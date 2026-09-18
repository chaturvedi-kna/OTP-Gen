"""
End-to-end replay of the reported recovery bug (real coordinator + real
MeeshoBotClient, fake Telethon bot, no network):

    [VSIMPRO] Cancellation response: {'type': 'ACCESS_CANCEL'}
    [BALANCE-GUARD] Refund tallied: balance 35.9177 >= expected 35.9177
    [VSIMPRO] Change Number failed in bot: Expected number prompt after Change
              Number, got unknown; full flow will restart.
    [NOTIFICATION: 🔄 Changing number] 9416424569 (otp_timeout). The bot flow
              will restart from the menu.

The cancellation and the refund tally were fine - the recovery then threw the
bot back to the main menu, so the next number paid for a full flow (Add Account
-> Login with Number -> Normal -> offer rerolls) before it could be sent.

What is verified here:
  * a Change Number the bot answers with a number prompt classify() does not
    recognise is accepted as the prompt: the replacement number goes straight
    in, no menu restart and no offer reroll;
  * a Change Number that genuinely cannot be recovered only resets the bot to
    the main menu when the BOT checker is needed for the next number check
    (checker.mode "bot", or "auto" while the API is down / cooling down);
  * with the checker API answering, the bot is left in-flow and the next login
    reuses the prompt it is sitting on - and still recovers by itself (walking
    back to the menu) when the bot really is lost.

    python test_change_number_recovery.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCRATCH_DIR = tempfile.mkdtemp(prefix="change_number_recovery_")

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

import main as m  # noqa: E402
from test_primes_referral_flow import (  # noqa: E402
    MAIN_MENU,
    PROMPT_ALT_COPY,
    BROKEN_SCREEN,
    build_client,
)


FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


class FakeProviderClient:
    name = "vsimpro"

    def __init__(self, balance=35.9177):
        self._balance = balance
        self.cancelled = []
        self.finished = []

    def get_balance(self):
        return self._balance

    def cancel(self, activation_id):
        self.cancelled.append(activation_id)
        return {"type": "ACCESS_CANCEL"}

    def finish(self, activation_id):
        self.finished.append(activation_id)
        return True

    def get_status(self, activation_id):
        return {"type": "STATUS_WAIT"}


class FakeNumberContext:
    def __init__(self, number="9416424569", client=None):
        self.client = client or FakeProviderClient()
        self.provider_name = "vsimpro"
        self.activation_id = "act-9416424569"
        self.raw_number = f"91{number}"
        self.clean_number = number


def menu_walks(bot):
    """How many full menu walks the flow paid for (one 'Add Account' tap each)."""
    return sum(1 for tap in bot.tapped if "Add Account" in tap)


def build(checker_mode="auto", change_number_screen=PROMPT_ALT_COPY,
          cooldown=False, **client_kwargs):
    """
    A real coordinator driving a real MeeshoBotClient against the fake
    Telethon bot, with fast timings and the notification/cancellation side
    effects stubbed out.
    """
    config = json.load(open(os.path.join(REPO_DIR, "config.json"), encoding="utf-8"))
    config["meesho_bot"]["enabled"] = True
    config["active_otp_provider"] = "vsimpro"
    config["checker"]["mode"] = checker_mode
    config["automation"]["refund_check_delay_seconds"] = 0
    os.chdir(SCRATCH_DIR)
    coordinator = m.ParallelAutomationCoordinator(config, provider_override="vsimpro")

    sent = []
    coordinator.notify.send = lambda title, message, **kw: sent.append((title, message)) or ["stub"]
    coordinator.notify.alert = lambda title, message, **kw: sent.append((title, message)) or ["stub"]
    coordinator.notify.telegram.send = lambda *a, **k: False
    coordinator.notify.termux.send = lambda *a, **k: False

    # The real MeeshoBotClient (fake Telethon underneath), as the coordinator
    # uses it. build_client's config is already fast; the session file check is
    # bypassed because there is no Telegram account in this check.
    bot_client_kwargs = {
        "step_timeout_seconds": 5,
        "poll_interval_seconds": 0.01,
        "human_delay_seconds": [0, 0],
        "change_number_timeout_seconds": 1,
        "change_number_budget_seconds": 5,
    }
    bot_client_kwargs.update(client_kwargs)
    bot_client, fake_bot = build_client(
        referral_link=None, referral_script="absent",
        change_number_screen=change_number_screen, **bot_client_kwargs
    )
    bot_client._session_string = lambda: "fake-session"
    coordinator.bot = bot_client  # checker.bot_getter() picks this up lazily

    if cooldown:
        coordinator.checker._enter_cooldown()

    return coordinator, bot_client, fake_bot, sent


def otp_wait_taps(bot):
    """Every tap the bot saw, in order."""
    return list(bot.tapped)


def scenario_reported_bug_alt_prompt_copy():
    """
    The reported sequence, with the bot answering Change Number in a copy
    classify() does not know: the recovery must land on the prompt and the next
    number must be sent from there - no menu restart, no offer reroll.
    """
    coordinator, bot_client, bot, sent = build()
    context = FakeNumberContext()

    # The run so far: number sent, OTP never arrived (the bot waits on its
    # "OTP on its way" screen).
    res = bot_client.prepare_login(context.clean_number)
    check("replay: first number reached the OTP screen",
          res["stage"] == "otp_sent" and bot_client.screen_state() == "otp_wait", res)
    taps_after_login = otp_wait_taps(bot)
    walks_after_login = menu_walks(bot)
    check("replay: the first number paid for exactly one menu walk",
          walks_after_login == 1 and bot.starts == 0,
          f"walks={walks_after_login} starts={bot.starts}")

    # Cancellation + refund tally (stubbed: covered by the coordinator checks),
    # then the recovery itself.
    coordinator.handle_cancellation = (
        lambda *a, **kw: {"tally_ok": True, "salvaged": None, "balance": 35.9177}
    )
    coordinator._recover_change_number(context, "otp_timeout")

    check("replay: Change Number was tapped once",
          bot.change_taps == 1, bot.change_taps)
    check("replay: bot is at the number prompt",
          coordinator.bot_at_number_prompt is True and bot_client.at_number_prompt(),
          bot_client.screen_state())
    check("replay: bot NOT sent back to the main menu",
          bot_client.screen_state() != "main_menu" and bot.starts == 0
          and menu_walks(bot) == walks_after_login,
          f"screen={bot_client.screen_state()} starts={bot.starts} walks={menu_walks(bot)}")
    check("replay: only the Change Number tap was added",
          otp_wait_taps(bot) == taps_after_login + ["\U0001f504 Change Number"],
          otp_wait_taps(bot))
    title, message = sent[-1]
    check("replay: notification says the bot waits for the replacement number",
          title == "🔄 Changing number"
          and "waiting for the replacement number" in message, message)
    check("replay: notification does NOT announce a menu restart",
          "restart from the menu" not in message, message)

    # The next found number goes straight into the prompt the bot is on.
    taps_before = otp_wait_taps(bot)
    next_context = FakeNumberContext(number="9416424570")
    res2 = coordinator._bot_send_number(next_context,
                                        from_prompt=coordinator.bot_at_number_prompt)
    check("replay: replacement number sent",
          bot.sent_numbers[-1] == "9416424570", bot.sent_numbers)
    check("replay: replacement reached the OTP screen",
          res2 is not None and res2["stage"] == "otp_sent", res2)
    check("replay: no menu walk for the replacement number",
          otp_wait_taps(bot) == taps_before, otp_wait_taps(bot))
    check("replay: no offer reroll for the replacement number",
          res2.get("rerolls", 0) == 0, res2)
    check("replay: no menu walk and no /start reset for the replacement number",
          menu_walks(bot) == walks_after_login and bot.starts == 0,
          f"walks={menu_walks(bot)} starts={bot.starts}")
    check("replay: OTP-requested notification went out",
          any("OTP requested via PRIMES bot" in t for t, _m in sent),
          [t for t, _m in sent])


def scenario_unrecoverable_keeps_flow_when_api_answers():
    """
    A Change Number that genuinely cannot be recovered (dead-end screen) with a
    healthy checker API: the bot is left in-flow, and the next login still gets
    where it has to be - by reusing a prompt or, when there is none, by walking
    back to the menu itself.
    """
    coordinator, bot_client, bot, sent = build(change_number_screen=BROKEN_SCREEN)
    context = FakeNumberContext()
    bot_client.prepare_login(context.clean_number)
    walks_after_login = menu_walks(bot)

    coordinator.handle_cancellation = (
        lambda *a, **kw: {"tally_ok": True, "salvaged": None, "balance": 35.9177}
    )
    started = time.time()
    coordinator._recover_change_number(context, "otp_timeout")
    took = time.time() - started

    check("dead end + healthy API: bot NOT reset to the main menu",
          bot_client.screen_state() != "main_menu" and bot.starts == 0
          and menu_walks(bot) == walks_after_login,
          f"screen={bot_client.screen_state()} starts={bot.starts} walks={menu_walks(bot)}")
    check("dead end + healthy API: prompt flag cleared",
          coordinator.bot_at_number_prompt is False)
    check("dead end + healthy API: counted as kept in-flow",
          coordinator.stats.snapshot().get("bot_flow_kept", 0) >= 1,
          coordinator.stats.snapshot())
    check("dead end + healthy API: recovery stayed inside its budget",
          took < 12, f"{took:.1f}s")
    title, message = sent[-1]
    check("dead end + healthy API: notification says the bot stays in-flow",
          "stays in-flow" in message and "restart from the menu" not in message,
          message)

    # The next number: prepare_login walks back to the menu itself (the bot is
    # on a dead-end screen), so nothing is lost by keeping the flow.
    next_context = FakeNumberContext(number="9416424571")
    res = coordinator._bot_send_number(next_context,
                                       from_prompt=coordinator.bot_at_number_prompt)
    check("dead end + healthy API: next number still sent",
          bot.sent_numbers[-1] == "9416424571", bot.sent_numbers)
    check("dead end + healthy API: next number reached the OTP screen",
          res is not None and res["stage"] == "otp_sent", res)
    check("dead end + healthy API: the menu walk happened only when it was needed",
          menu_walks(bot) == walks_after_login + 1,
          f"walks={menu_walks(bot)} (was {walks_after_login})")


def scenario_unrecoverable_resets_when_bot_checker_needed():
    """
    Same dead end, but the next number check has to go through the PRIMES bot
    (checker.mode "bot", or "auto" while the API cools down): the bot checker
    works from the main menu, so the reset is done right away.
    """
    for label, kwargs in (("checker.mode=bot", {"checker_mode": "bot"}),
                          ("API cooling down", {"checker_mode": "auto", "cooldown": True})):
        coordinator, bot_client, bot, sent = build(change_number_screen=BROKEN_SCREEN,
                                                   **kwargs)
        context = FakeNumberContext()
        bot_client.prepare_login(context.clean_number)
        walks_after_login = menu_walks(bot)
        coordinator.handle_cancellation = (
            lambda *a, **kw: {"tally_ok": True, "salvaged": None, "balance": 35.9177}
        )
        coordinator._recover_change_number(context, "otp_timeout")

        check(f"dead end + {label}: bot reset to the main menu",
              bot_client.screen_state() == "main_menu",
              f"screen={bot_client.screen_state()} walks={menu_walks(bot)} "
              f"(was {walks_after_login}) starts={bot.starts}")
        check(f"dead end + {label}: prompt flag cleared",
              coordinator.bot_at_number_prompt is False)
        title, message = sent[-1]
        check(f"dead end + {label}: notification says the flow restarts from the menu",
              "restart from the menu" in message, message)


def scenario_prompt_reuse_after_kept_flow():
    """
    The payoff of keeping the flow: when the bot is left sitting on a good
    offer/number prompt, the next login sends its number from there instead of
    re-rolling an offer from the main menu.
    """
    coordinator, bot_client, bot, sent = build()
    bot.state = "offer"
    bot.offer_prices = [45]
    bot._push(bot._offer_screen())
    walks_before = menu_walks(bot)

    context = FakeNumberContext(number="9416424572")
    res = coordinator._bot_send_number(context, from_prompt=False)
    check("kept flow: number sent from the prompt the bot was on",
          bot.sent_numbers == ["9416424572"], bot.sent_numbers)
    check("kept flow: OTP screen reached",
          res is not None and res["stage"] == "otp_sent", res)
    check("kept flow: no menu walk, no reroll",
          bot.tapped == [] and res.get("rerolls", 0) == 0
          and menu_walks(bot) == walks_before,
          f"taps={bot.tapped} res={res}")
    check("kept flow: no /start reset", bot.starts == 0, bot.starts)
    check("kept flow: reuse reported by the flow",
          res.get("reused_prompt") is True, res)

def scenario_prompt_lost_to_a_bot_check():
    """
    A bot number check resets the bot to its main menu, so the prompt the
    coordinator remembers can be gone by the time the next number is ready. The
    paid number must not be typed into the menu: the screen is re-read and the
    full flow runs.
    """
    coordinator, bot_client, bot, sent = build()
    context = FakeNumberContext()
    bot_client.prepare_login(context.clean_number)
    coordinator.handle_cancellation = (
        lambda *a, **kw: {"tally_ok": True, "salvaged": None, "balance": 35.9177}
    )
    coordinator._recover_change_number(context, "otp_timeout")
    check("prompt lost: the recovery reached the number prompt",
          coordinator.bot_at_number_prompt is True)

    # A bot check (checker.mode "bot", or an "auto" fallback) resets the bot.
    bot.state = "menu"
    bot._edit_last(MAIN_MENU)
    walks_before = menu_walks(bot)

    next_context = FakeNumberContext(number="9416424573")
    res = coordinator._bot_send_number(next_context, from_prompt=True)
    check("prompt lost: number NOT typed into the main menu",
          bot.sent_numbers[-1] == "9416424573", bot.sent_numbers)
    check("prompt lost: the full flow ran instead",
          menu_walks(bot) == walks_before + 1,
          f"walks={menu_walks(bot)} (was {walks_before}) taps={bot.tapped}")
    check("prompt lost: OTP screen reached",
          res is not None and res["stage"] == "otp_sent", res)
    check("prompt lost: prompt flag corrected",
          coordinator.bot_at_number_prompt is False)
    check("prompt lost: number not cancelled for it",
          not any("needs attention" in title or "bot error" in title.lower()
                  for title, _m in sent),
          [title for title, _m in sent])


def main():
    print("=== Change Number recovery (coordinator + real bot client) ===\n")
    print(f"(scratch dir: {SCRATCH_DIR})\n")
    shutil.copy(os.path.join(REPO_DIR, "config.json"),
                os.path.join(SCRATCH_DIR, "config.json"))
    scenario_reported_bug_alt_prompt_copy()
    scenario_unrecoverable_keeps_flow_when_api_answers()
    scenario_unrecoverable_resets_when_bot_checker_needed()
    scenario_prompt_reuse_after_kept_flow()
    scenario_prompt_lost_to_a_bot_check()

    print()
    shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All Change Number recovery checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
