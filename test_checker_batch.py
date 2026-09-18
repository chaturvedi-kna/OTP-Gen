"""
Batch verification in the dedicated checker bot ("Meesho Xxpress Manish"):
the bot accepts several comma-separated numbers in ONE message and answers
with one verdict line per number - but NOT in the input order. The router's
batch window collects concurrent workers' checks, sends them as a single
request, and hands every worker exactly the verdict for ITS number.

    python test_checker_batch.py
"""

import sys
import threading
import types

sys.path.insert(0, __file__.rsplit("/", 1)[0] or ".")

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

from checker_router import BotChecker  # noqa: E402
from checker_client import CheckerUnavailable  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


class FakeDedicatedChecker:
    """Stand-in for CheckerBotClient with the observed bot behaviours."""

    def __init__(self, verdicts, drop=(), calls_delay=0.0):
        self.verdicts = dict(verdicts)   # {"8897747677": True/False}
        self.drop = set(drop)            # numbers the bot "forgets" to answer
        self.calls_many = []
        self.calls_single = []
        self.ready = True
        self.enabled = True
        self.start_error = ""
        self.bot_username = "MeeshoXxpressManishBot"

    def check_registration(self, number):
        self.calls_single.append(str(number))
        digits = str(number)[-10:]
        return {"success": True, "is_registered": self.verdicts[digits],
                "source": "telegram_checker"}

    def check_registration_many(self, numbers):
        self.calls_many.append([str(n) for n in numbers])
        # Reply the way the bot does: REVERSED input order (observed) and one
        # number possibly missing - matching must be by number, not position.
        out = {}
        for digits in reversed([str(n)[-10:] for n in numbers]):
            if digits in self.drop:
                continue
            out[digits] = self.verdicts[digits]
        return {"success": True, "verdicts": out, "source": "bot"}


def _make_checker(fake, **conf):
    conf.setdefault("step_timeout_seconds", 5)
    return BotChecker(
        None, conf, log_fn=None,
        preferred_getter=lambda: fake,
        preferred_username="MeeshoXxpressManishBot",
        preferred_name="Meesho Xxpress Manish",
    )


def _run_concurrent(checker, numbers):
    start = threading.Barrier(len(numbers))
    results, errors = {}, {}

    def worker(n):
        try:
            start.wait(timeout=5)
            results[n] = checker.check(n)
        except Exception as exc:
            errors[n] = exc

    threads = [threading.Thread(target=worker, args=(n,), daemon=True)
               for n in numbers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results, errors


VERDICTS = {
    "8898845614": False,   # NEW USER
    "8897747677": True,    # REGISTERED
    "7977748514": True,    # REGISTERED
}


def scenario_batch_window():
    fake = FakeDedicatedChecker(VERDICTS)
    checker = _make_checker(fake, batch_enabled=True, batch_size=3,
                            batch_wait_seconds=2.0)
    numbers = list(VERDICTS)
    results, errors = _run_concurrent(checker, numbers)
    check("batch: every worker got a result, none an error",
          not errors and len(results) == 3, errors)
    check("batch: ONE message carried all three numbers",
          len(fake.calls_many) == 1 and sorted(fake.calls_many[0]) == sorted(numbers),
          (fake.calls_many, fake.calls_single))
    check("batch: no single-number fallback happened",
          not fake.calls_single, fake.calls_single)
    check("batch: verdicts matched by number despite the shuffled reply",
          all(results[n]["is_registered"] == VERDICTS[n] for n in numbers),
          results)


def scenario_batch_disabled():
    fake = FakeDedicatedChecker(VERDICTS)
    checker = _make_checker(fake, batch_enabled=False)
    numbers = list(VERDICTS)
    results, errors = _run_concurrent(checker, numbers)
    check("batch off: sequential single checks, one per number",
          not errors and sorted(fake.calls_single) == sorted(numbers)
          and not fake.calls_many,
          (fake.calls_single, fake.calls_many, errors))
    check("batch off: verdicts still right per number",
          all(results[n]["is_registered"] == VERDICTS[n] for n in numbers),
          results)


def scenario_missing_verdict():
    fake = FakeDedicatedChecker(VERDICTS, drop={"8898845614"})
    checker = _make_checker(fake, batch_enabled=True, batch_size=3,
                            batch_wait_seconds=2.0)
    numbers = list(VERDICTS)
    results, errors = _run_concurrent(checker, numbers)
    check("batch: a missing verdict fails just that one number",
          isinstance(errors.get("8898845614"), CheckerUnavailable),
          errors)
    check("batch: numbers with a verdict still succeed",
          results.get("8897747677", {}).get("is_registered") is True
          and results.get("7977748514", {}).get("is_registered") is True,
          results)


def scenario_lone_check_still_works():
    # With batching on but nobody else checking, a lone check must not hang:
    # after the wait window it runs as a normal single check.
    fake = FakeDedicatedChecker({"9000000001": False})
    checker = _make_checker(fake, batch_enabled=True, batch_size=3,
                            batch_wait_seconds=0.3)
    res = checker.check("9000000001")
    check("batch: a lone check falls back to a single request",
          res["is_registered"] is False and fake.calls_single == ["9000000001"],
          (res, fake.calls_single, fake.calls_many))


def main():
    scenario_batch_window()
    scenario_batch_disabled()
    scenario_missing_verdict()
    scenario_lone_check_still_works()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED")
        return 1
    print("\nAll checker-batch checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
