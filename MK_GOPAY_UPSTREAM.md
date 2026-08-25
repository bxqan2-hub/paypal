# GoPay upstream project provenance

The complete GoPay extraction project is copied into
`payment_link_extractor/mk_gopay_open_source/` from
[eatWhitePorridge/link-gp](https://github.com/eatWhitePorridge/link-gp) at
commit `3d2af69d848e6f292ef5abcb763c89dac3fbbea5`.

The workbench keeps its existing UI and task/WebSocket lifecycle. Selecting
**GoPay（印度尼西亚）** forces country `ID` / currency `IDR`, bypasses the
legacy checkout-update branch, and calls the copied project's public
`gopay.gopay_extract.run_gopay_flow()` directly through
`payment_link_extractor/mk_gopay.py`. The upstream protocol files are not
modified or merged into the existing PayPal/GCash extraction core.

`mk_gopay_project_manifest.json` records the 19 copied source/docs/test files
and their SHA-256 hashes. The upstream MIT license remains in the vendored
directory. The adapter only translates the existing task configuration and
result fields (`gopay_url`, `provider_url`) and supplies the site's Sentinel
compatibility import when the optional upstream `nicepay` package is absent.
