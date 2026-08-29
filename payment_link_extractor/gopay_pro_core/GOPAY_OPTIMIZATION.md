# GoPay Checkout proof and validation path

## Active path

1. Chromium loads the bundled Sentinel SDK as an init script.
2. The script publishes `SentinelSDK` explicitly on `window` and `globalThis`.
3. Protected Checkout calls request `token("chatgpt_checkout")`; empty,
   `default`, and `__default__` inputs are normalized to that flow.
4. The same browser session supplies the short-lived Sentinel token, optional
   session-observer token, deployment attestation, HttpOnly cookies, and
   `oai-did`/`oai-device-id` continuity.
5. Checkout responses detect `oaics_` versus `cs_`, merge duplicate method
   lists, and classify retryable versus terminal failures.

## Failure modes

| Mode | Retry | Action |
|---|---:|---|
| `unusual_activity` | yes | New full attempt, proxy route and browser identity |
| `rate_limited` | yes | New full attempt using the next proxy route |
| `upstream_transient` | yes | New full attempt |
| `access_token_invalid` | no | Stop and replace the AT |
| `access_denied` | no | Stop; retain the upstream diagnostic |
| `payment_method_unavailable` | no | Stop because GoPay is absent for that Checkout |

## Alternatives

| Approach | Status | Trade-off |
|---|---|---|
| Real Chromium + injected SDK | selected | Preserves browser cookies, device ID and proof lifecycle |
| Captured static Sentinel token | fallback only | Short-lived and not bound to the current browser session |
| Node/browser shim | diagnostic only | Does not reproduce a real Chromium fingerprint/session |
| Manual browser handoff | recovery path | Highest fidelity but not suitable for unattended batches |

Use `validation.validate_checkout_batch` for sanitized offline comparison of
successful `oaics_`/`cs_` samples and differentiated failure modes.
