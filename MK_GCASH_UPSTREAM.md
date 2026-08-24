# MK GCash upstream project provenance

The complete GCash extraction project is copied into
`payment_link_extractor/mk_gcash_open_source/` from
[mika50000/MK-GCash-Link-OpenSource](https://github.com/mika50000/MK-GCash-Link-OpenSource)
at commit `2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5`.

The copied directory contains all 22 tracked upstream files, including
`app.py`, `web/`, `tests/`, startup scripts, documentation, license, and the
complete GCash runtime. The site calls the copied `app.create_job()` and polls
the copied `app.public_job()` directly. It does not merge the upstream checkout
chain into the site's legacy provider modules.

The six protocol files are additionally recorded byte-for-byte in the root
manifest for compatibility with the existing provenance checks:

- `gcash_chain.py`
- `payment_monitor.py`
- `sentinel.py`
- `sentinel_bridge.js`
- `sentinel_assets/sentinel_bootstrap.js`
- `sentinel_assets/sentinel_sdk.js`

`payment_link_extractor/mk_gcash.py` is only a boundary adapter: it builds the
upstream app payload, starts the upstream task, and maps its completed task
snapshot into this site's result model. The application dispatches GCash to
that direct upstream entry point before it creates any legacy checkout/Stripe
transport, so the previous site GCash implementation is not used.

The upstream MIT license is preserved at
`licenses/MK-GCash-Link-OpenSource.LICENSE`. Exact source hashes are recorded in
`mk_gcash_core_manifest.json` and enforced by the test suite. The full copied
project remains source-identical to the recorded upstream commit.

The browser workbench also follows the upstream single `proxy_pool` contract,
1–10 maximum-attempt semantics, purple MK visual system, and account/task
layout. The upstream `web/assets/mikael-mail-logo.webp` is copied byte-for-byte.
The payment-method selector is the intentional local entry-point difference;
every selected method shares the same proxy pool.

GCash account metadata now follows the copied upstream `app.py`: explicit account
`email`/`name` wins, then JWT profile claims; expired JWTs are rejected before
checkout. No synthetic Philippine address is injected. The integration only
adds runtime observability, shared-Chromium prewarming, and an 8-second proxy
TCP connect cap; the vendored protocol files and their recorded hashes remain
unchanged.
