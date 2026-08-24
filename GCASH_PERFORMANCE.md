# GCash performance diagnosis

## Observed cause

The recent slow extraction recorded in the local service log was blocked before
a successful ChatGPT connection. It was not spending the time calculating
billing or inside the payment-method adapter.

Sanitized timeline for the most recent five-attempt task:

| Attempt | Start | End | Duration | Result |
|---|---:|---:|---:|---|
| 1 | 18:53:10.832 | 18:53:40.847 | 30.015 s | proxy connection timeout |
| 2 | 18:53:40.848 | 18:54:10.853 | 30.005 s | proxy connection timeout |
| 3 | 18:54:10.853 | 18:54:40.860 | 30.007 s | proxy connection timeout |
| 4 | 18:54:40.860 | 18:55:10.873 | 30.013 s | proxy connection timeout |
| 5 | 18:55:10.873 | 18:55:31.948 | 21.075 s | proxy could not connect to ChatGPT |

Total wall time was **141.116 seconds**. Four entries exhausted the upstream
30-second request timeout consecutively. The final error also explicitly
reported that the proxy could not connect to `chatgpt.com:443`. Therefore the
dominant delay was an unreachable/slow proxy pool combined with five sequential
attempts.

## Optimizations applied

1. The shared upstream Playwright Chromium is prewarmed when the service starts,
   removing first-GCash-task browser cold-start work.
2. The upstream 30-second HTTP response deadline remains unchanged, but the TCP
   proxy connection phase is capped at 8 seconds by default. A dead five-node
   pool now reaches rotation/failure much earlier instead of consuming roughly
   30 seconds per node.
3. Each task stage is now written to the log by name, and every attempt records
   `elapsed_ms`.
4. Successful GCash results expose `stage_offsets_ms`, `total_elapsed_ms`, and
   `connect_timeout_ms`, so the next real run identifies whether time is spent
   in proxy preflight, checkout, taxes, confirmation, redirect, or QR capture.

Environment controls:

```dotenv
MK_GCASH_PREWARM=true
MK_GCASH_CONNECT_TIMEOUT_MS=8000
```

The connect cap is bounded to 3–15 seconds. These integration optimizations do
not alter `gcash_chain.py`, `payment_monitor.py`, Sentinel code, checkout body,
tax/confirm/start order, payment monitor, or callback behavior.
