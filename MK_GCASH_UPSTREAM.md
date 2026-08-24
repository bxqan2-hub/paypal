# MK GCash core provenance

The GCash execution core in this repository is a direct, unmodified copy of
[mika50000/MK-GCash-Link-OpenSource](https://github.com/mika50000/MK-GCash-Link-OpenSource)
at commit `2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5`.

Copied core files:

- `gcash_chain.py`
- `payment_monitor.py`
- `sentinel.py`
- `sentinel_bridge.js`
- `sentinel_assets/sentinel_bootstrap.js`
- `sentinel_assets/sentinel_sdk.js`

`payment_link_extractor/mk_gcash.py` is the local result-model adapter. The
application dispatches GCash to that adapter before it creates any of the
legacy checkout/Stripe transports, so the previous site GCash implementation
is not used.

The upstream MIT license is preserved at
`licenses/MK-GCash-Link-OpenSource.LICENSE`. Exact source hashes are recorded in
`mk_gcash_core_manifest.json` and enforced by the test suite.
