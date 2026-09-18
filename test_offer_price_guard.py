"""
A paid number must NEVER be typed into an offer whose UPI price is above the
target (the pre-warmed offer may have drifted while the workers were still
hunting, or the parked prompt may show a price line that was not checked).

The continue-from-prompt login path now verifies the prompt's price BEFORE
typing: above target it rerolls in place until the offer fits (or raises
without typing); an unreadable price with no reroll button is accepted as-is
(the price was already agreed earlier in that same login).

    python test_offer_price_guard.py
"""

import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0] or ".")

import test_primes_referral_flow as replay  # noqa: E402
from meesho_bot_client import MeeshoBotError  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def _park_bot(bot, screen, prices=None):
    """Put the fake bot on `screen` as if pre-warm had parked it there."""
    bot.state = "offer"
    if prices is not None:
        bot.offer_prices = list(prices)
    bot._edit_last(screen)


def scenario_reroll_down_before_typing():
    # Parked prompt now sits at Rs.60 (above the Rs.47 target), but a reroll
    # brings back an in-budget offer: the number is typed only after that.
    client, bot = replay.build_client(referral_link=None, referral_script="absent")
    _park_bot(bot, replay.OFFER_60, prices=[60, 45])
    res = client.continue_with_number("9876543210")
    check("guard: the offer was rerolled in place before typing",
          bot.reroll_count == 1, bot.reroll_count)
    check("guard: the number finally went out at the fitting offer",
          bot.sent_numbers == ["9876543210"], bot.sent_numbers)
    check("guard: login succeeded at the guarded price",
          res.get("stage") == "otp_sent" and res.get("upi") == 45, res)
    check("guard: the result reports the in-place reroll",
          res.get("rerolls", 0) >= 1, res)


def scenario_too_expensive_never_typed():
    # Parked at Rs.60 with NO reroll budget: fail loud, and never type.
    client, bot = replay.build_client(referral_link=None, referral_script="absent",
                                      max_offer_rerolls=0)
    _park_bot(bot, replay.OFFER_60, prices=[60, 60])
    raised = None
    try:
        client.continue_with_number("9876543210")
    except MeeshoBotError as exc:
        raised = exc
    check("guard: an unaffordable parked offer raises instead of typing",
          raised is not None, raised)
    check("guard: the number was NOT typed into the expensive offer",
          not bot.sent_numbers, bot.sent_numbers)


def scenario_unreadable_price_no_reroll_accepted():
    # A genuine Change Number prompt: no price line, no reroll button - the
    # price was already agreed for this login, so typing must proceed.
    client, bot = replay.build_client(referral_link=None, referral_script="absent")
    _park_bot(bot, replay.CHANGE_NUMBER_SCREENSHOT, prices=[45])
    res = client.continue_with_number("9876543210")
    check("guard: price-less prompt (no reroll button) still accepts the number",
          bot.sent_numbers == ["9876543210"], (bot.sent_numbers, res))
    check("guard: login succeeded without a readable price",
          res.get("stage") == "otp_sent", res)


def scenario_in_budget_no_reroll():
    # Parked at Rs.45 (within target): typed immediately, no reroll spent.
    client, bot = replay.build_client(referral_link=None, referral_script="absent")
    _park_bot(bot, replay.OFFER_45, prices=[45])
    res = client.continue_with_number("9876543210")
    check("guard: in-budget offer needs no reroll",
          bot.reroll_count == 0, bot.reroll_count)
    check("guard: number typed, price carried into the result",
          bot.sent_numbers == ["9876543210"] and res.get("upi") == 45,
          (bot.sent_numbers, res))


def main():
    scenario_reroll_down_before_typing()
    scenario_too_expensive_never_typed()
    scenario_unreadable_price_no_reroll_accepted()
    scenario_in_budget_no_reroll()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED")
        return 1
    print("\nAll offer-price-guard checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
