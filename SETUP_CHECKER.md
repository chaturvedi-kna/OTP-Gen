# Checker setup: rate limits, retries & multiple API keys

## 1. The problem this solves

The checker service (superassets.in) rate limits **per API key per service**.
With several provider workers (Tempora, VSImpro, OTPCart, OTPDoctor) validating
numbers in parallel through a single key, the server answers:

```
HTTP 429: {"detail":"Rate limit exceeded. Please wait 4.6s for service 'meesho'"}
```

Previously **any** checker error — including this harmless 429 — cancelled the
activation. That threw away paid numbers (and attempts) over a temporary
throttle, which showed up as a storm of:

```
[VSIMPRO] Cancelling activation ... (Reason: Checker error: HTTP 429: ...)
```

## 2. How it works now

`checker_client.py` handles rate limits internally so a 429 almost never
reaches the worker's cancel path:

1. **Retry the same number.** On HTTP 429 the client reads the wait the server
   asks for (from the `"Please wait Xs"` hint in the body, or a `Retry-After`
   header), sleeps exactly that long (+ a small buffer), and retries the same
   check. No number is cancelled, nothing is wasted.
2. **Key rotation.** With several API keys configured, a rate-limited key is
   skipped in favour of a free one immediately — N keys give roughly N times
   the checking throughput. A key rejected with 401/403 is retired for the
   rest of the run and the others keep going.
3. **Learned pacing.** After a 429 the client remembers the spacing the server
   demanded per (key, service). Later workers wait their turn instead of
   bursting into more 429s — the limit is respected proactively, not just
   reacted to.
4. **Transient-error retries.** Network errors and HTTP 5xx are retried with a
   short backoff for the same reason: never cancel a paid number over
   something that fixes itself in a second.
5. **Bounded budget.** All retries are capped by `max_retries` and
   `max_retry_wait_seconds` (default: 10 attempts / 45 s, far below the number
   rental window). Only if the budget is genuinely exhausted does the check
   fail — and the existing cancel/refund path takes over as the last resort.
   Definitive failures (`success=false`, `is_down=true`, other 4xx, auth on
   every key) still fail immediately, exactly as before.

## 3. Configuration (`config.json` → `"checker"`)

```json
"checker": {
  "base_url": "https://superassets.in",
  "api_keys": [
    "AK__3b9HMjWPCtV8_kNsN4npV64lwPVqdAa",
    "AK__second_key_if_you_have_one"
  ],
  "service": "meesho",
  "max_retries": 10,
  "max_retry_wait_seconds": 45.0,
  "min_interval_seconds": 1.0,
  "rate_limit_buffer_seconds": 0.5,
  "network_backoff_seconds": 1.5
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `api_keys` | – | **Recommended.** List of checker API keys; requests rotate across them with independent pacing per key. Get more keys from the checker provider to scale throughput. |
| `api_key` | – | Legacy single key. Still honoured if `api_keys` is absent (and merged in if both are present). |
| `max_retries` | `10` | Maximum attempts per check (across 429s, 5xx and network errors). |
| `max_retry_wait_seconds` | `45` | Hard ceiling on total time spent retrying one check. Keep well below the provider's number rental window. |
| `min_interval_seconds` | `1.0` | Conservative minimum spacing between two requests on the same key before a 429 has taught the real spacing. |
| `rate_limit_buffer_seconds` | `0.5` | Safety margin added on top of the server-asked wait. |
| `network_backoff_seconds` | `1.5` | Backoff between retries after network errors / 5xx. |
| `timeout` | `15` | Per-request HTTP timeout (seconds). |

## 4. What you'll see in the logs

Instead of cancellations, bursts now look like this and resolve on their own:

```
[...] Checker rate limited on key ...VqdAa: server asked to wait 4.6s; retry 1/10
[...] Checker: pacing - waiting 4.6s for key ...VqdAa (meesho)
[...] Checker result: is_registered=False (Target: False)
```

Startup reports the configuration:

```
[...] Checker ready: 2 API key(s), service 'meesho', retry budget 45s
```

A key that is revoked shows up once and is then skipped:

```
[...] Checker key ...Xy12 rejected (HTTP 403); rotating - 1 usable key(s) left
```

## 5. Verify

Offline check (no network, scripted responses):

```bash
python test_checker_client.py
```
