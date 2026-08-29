# GoPay HAR dual comparison (redacted)

> Inputs are offline HAR data. No request was replayed. Token, cookie, session, order, and nonce values are represented only by length/hash or placeholders.

## app.midtrans.com.har
- source: `C:\Users\Administrator\Downloads\app.midtrans.com.har`
- size_bytes: `20718395`
- sha256: `521AE7D5567654242F87448F8E709DE46DCAEA1E1117244D22639CD76872D202`
- entries: `580`
- hosts: `{"08f9b7c652af.w.hcaptcha.com": 1, "890b720ef715.w.hcaptcha.com": 1, "alb.reddit.com": 1, "analytics.tiktok.com": 4, "api.hcaptcha.com": 9, "api.stripe.com": 12, "app.midtrans.com": 5, "applepay.cdn-apple.com": 4, "b.stripecdn.com": 10, "ba8e4dd5b3ce.w.hcaptcha.com": 1, "bat.bing.com": 5, "chatgpt.com": 36, "cloudauth-device-dualstack.ap-southeast-1.aliyuncs.com": 2, "connect.facebook.net": 2, "edbd071ef887.w.hcaptcha.com": 1, "flagcdn.com": 254, "fonts.googleapis.com": 1, "fonts.gstatic.com": 1, "g.alicdn.com": 3, "global.faro.katulampa.gopay.sh": 7, "googleads.g.doubleclick.net": 2, "hcaptcha.com": 1, "js.stripe.com": 52, "m.stripe.com": 4, "m.stripe.network": 2, "maps.googleapis.com": 8, "merchant-ui-api.stripe.com": 1, "newassets.hcaptcha.com": 9, "o.alicdn.com": 1, "pay.google.com": 3, "pixel-config.reddit.com": 1, "play.google.com": 8, "pm-redirects.stripe.com": 1, "r.stripe.com": 96, "smp-paymentservices.apple.com": 1, "snap-assets.midtrans.com": 7, "snap-web-raccoon.gojekapi.com": 3, "upload.captcha-open-southeast.aliyuncs.com": 1, "www.facebook.com": 3, "www.google.co.id": 2, "www.google.com": 2, "www.googleadservices.com": 2, "www.googletagmanager.com": 1, "www.gstatic.com": 7, "www.redditstatic.com": 1, "y1rdnbp.captcha-open-southeast.aliyuncs.com": 1}`
- statuses: `{"0": 3, "200": 538, "202": 13, "204": 7, "302": 5, "304": 8, "404": 6}`
- methods: `{"GET": 411, "OPTIONS": 6, "POST": 163}`

### Sentinel and ChatGPT contract
- sentinel flows: `{"chatgpt_checkout": 2, "checkout_session_approval": 2}`
- sentinel shapes: `[{"index": 8, "flow": "chatgpt_checkout", "id": "len=36 sha256=20963c199590a53a", "p": "len=777 sha256=ebfce22409231735"}, {"index": 18, "flow": "chatgpt_checkout", "id": "len=36 sha256=20963c199590a53a", "p": "len=777 sha256=ebfce22409231735"}, {"index": 59, "flow": "checkout_session_approval", "id": "len=36 sha256=20963c199590a53a", "p": "len=777 sha256=ebfce22409231735"}, {"index": 290, "flow": "checkout_session_approval", "id": "len=36 sha256=20963c199590a53a", "p": "len=777 sha256=ebfce22409231735"}]`
- header presence: `{"oai-client-build-number": 16, "oai-client-version": 16, "oai-device-id": 16, "oai-language": 16, "oai-session-id": 16, "oai-web-deployment-attestation": 6, "openai-sentinel-token": 2, "x-oai-is-client-observation": 14}`
- header lengths: `{"oai-client-build-number": [8], "oai-client-version": [45], "oai-device-id": [36], "oai-language": [5], "oai-session-id": [36], "oai-web-deployment-attestation": [291], "openai-sentinel-token": [6890, 8145], "x-oai-is-client-observation": [23]}`
- response text captured/omitted: `{"with_text": 282, "without_text": 298}`

### Stripe contract
- Elements query: `[{"index": 48, "keys": ["_stripe_version", "browser_timezone", "checkout_session_id", "client_betas[0]", "client_betas[1]", "currency", "deferred_intent[amount]", "deferred_intent[currency]", "deferred_intent[mode]", "deferred_intent[payment_method_configuration][id]", "deferred_intent[payment_method_types][0]", "deferred_intent[payment_method_types][1]", "deferred_intent[setup_future_usage]", "elements_init_source", "key", "locale", "referrer_host", "stripe_js_id", "type"], "selected": {"deferred_intent[mode]": "subscription", "deferred_intent[amount]": "34900000", "deferred_intent[currency]": "idr", "deferred_intent[setup_future_usage]": "off_session", "deferred_intent[payment_method_types][0]": "card", "deferred_intent[payment_method_types][1]": "gopay", "currency": "idr", "key": "<redacted>", "_stripe_version": "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1", "elements_init_source": "custom_checkout", "referrer_host": "chatgpt.com", "locale": "id", "type": "deferred_intent"}}]`
- progressive tax steps: `[{"index": 151, "fields": ["tax_region[country]"], "field_count": 1}, {"index": 210, "fields": ["tax_region[country]", "tax_region[line1]"], "field_count": 2}, {"index": 219, "fields": ["tax_region[country]", "tax_region[line1]", "tax_region[city]"], "field_count": 3}, {"index": 228, "fields": ["tax_region[country]", "tax_region[line1]", "tax_region[city]", "tax_region[state]"], "field_count": 4}, {"index": 239, "fields": ["tax_region[country]", "tax_region[line1]", "tax_region[city]", "tax_region[state]", "tax_region[postal_code]"], "field_count": 5}]`
- confirm summary: `[{"index": 250, "key_count": 62, "expected_amount": "34900000", "expected_payment_method_type": "gopay", "link_brand": "link", "payment_method_data[type]": "gopay", "payment_method_data[time_on_page]": "41223", "version": "b0f5e7abe5", "_stripe_version": "2025-03-31.basil%3B+checkout_server_update_beta%3Dv1%3B+checkout_manual_approval_preview%3Dv1", "init_checksum": "len=32 sha256=80ef3b4723ab9dc0", "js_checksum": "len=96 sha256=3d940585b1de8649"}]`

### Provider contract
- Stripe redirects: `[{"index": 287, "status": 302, "location_host": "app.midtrans.com", "location_path": "/snap/v4/redirection/<UUID>"}]`
- Midtrans transactions: `[{"index": 300, "status": 200, "amount": "349000", "currency": "IDR", "recommended": "gopay", "enabled_types": ["gopay", "qris"]}]`

### Relevant sequence
```text
1:/backend-api/sentinel/sdk.js -> 6:/backend-api/sentinel/frame.html -> 8:/backend-api/sentinel/req -> 9:/backend-api/sentinel/ping -> 10:/backend-api/payments/checkout -> 18:/backend-api/sentinel/req -> 32:/v1/payment_pages/cs_<CHECKOUT_SESSION>/init -> 48:/v1/elements/sessions -> 59:/backend-api/sentinel/req -> 82:/backend-api/sentinel/ping -> 96:/v1/consumers/sessions/lookup -> 151:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 210:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 219:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 222:/backend-api/payments/checkout/taxes -> 226:/backend-api/payments/checkout/snapshot -> 227:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 228:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 233:/backend-api/payments/checkout/taxes -> 234:/backend-api/payments/checkout/snapshot -> 235:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 239:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 250:/v1/payment_pages/cs_<CHECKOUT_SESSION>/confirm -> 261:/backend-api/sentinel/ping -> 263:/backend-api/payments/checkout/approve -> 283:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 287:pm-redirects.stripe.com/authorize -> 290:/backend-api/sentinel/req -> 293:/snap/v4/redirection -> 300:/snap/v1/transactions -> 301:/snap/v1/promos -> 302:/snap/v3/experiment
```

## app.midtrans.com1.har
- source: `C:\Users\Administrator\Downloads\app.midtrans.com1.har`
- size_bytes: `19414223`
- sha256: `FD33ECDD26D93688A9208FF66457673CAA676A7C428B87BDBD54CAF605DD4499`
- entries: `587`
- hosts: `{"00d949a766ae.w.hcaptcha.com": 1, "292b88e65dee.w.hcaptcha.com": 1, "50d64741de9a.w.hcaptcha.com": 1, "87b4b6c08ca3.w.hcaptcha.com": 1, "alb.reddit.com": 1, "analytics.tiktok.com": 4, "api.hcaptcha.com": 9, "api.stripe.com": 12, "app.midtrans.com": 5, "applepay.cdn-apple.com": 3, "b.stripecdn.com": 10, "bat.bing.com": 5, "chatgpt.com": 35, "cloudauth-device-dualstack.ap-southeast-1.aliyuncs.com": 1, "connect.facebook.net": 2, "flagcdn.com": 254, "fonts.googleapis.com": 1, "fonts.gstatic.com": 1, "g.alicdn.com": 1, "global.faro.katulampa.gopay.sh": 13, "hcaptcha.com": 1, "js.stripe.com": 52, "m.stripe.com": 4, "m.stripe.network": 2, "maps.googleapis.com": 8, "merchant-ui-api.stripe.com": 1, "newassets.hcaptcha.com": 9, "o.alicdn.com": 1, "pay.google.com": 3, "pixel-config.reddit.com": 1, "play.google.com": 15, "pm-redirects.stripe.com": 1, "r.stripe.com": 105, "snap-assets.midtrans.com": 7, "snap-web-raccoon.gojekapi.com": 4, "ws.chatgpt.com": 1, "www.facebook.com": 2, "www.gstatic.com": 7, "www.redditstatic.com": 1, "y1rdnbp.captcha-open-southeast.aliyuncs.com": 1}`
- statuses: `{"0": 4, "101": 1, "200": 544, "202": 18, "204": 10, "302": 1, "304": 3, "404": 6}`
- methods: `{"GET": 396, "OPTIONS": 17, "POST": 174}`

### Sentinel and ChatGPT contract
- sentinel flows: `{"chatgpt_checkout": 2, "checkout_session_approval": 2}`
- sentinel shapes: `[{"index": 6, "flow": "chatgpt_checkout", "id": "len=36 sha256=91087ec5b081ab71", "p": "len=613 sha256=ae042615347b9c27"}, {"index": 13, "flow": "chatgpt_checkout", "id": "len=36 sha256=91087ec5b081ab71", "p": "len=613 sha256=ae042615347b9c27"}, {"index": 51, "flow": "checkout_session_approval", "id": "len=36 sha256=91087ec5b081ab71", "p": "len=613 sha256=ae042615347b9c27"}, {"index": 289, "flow": "checkout_session_approval", "id": "len=36 sha256=91087ec5b081ab71", "p": "len=613 sha256=ae042615347b9c27"}]`
- header presence: `{"oai-client-build-number": 18, "oai-client-version": 18, "oai-device-id": 18, "oai-language": 18, "oai-session-id": 18, "oai-web-deployment-attestation": 6, "openai-sentinel-token": 2, "x-oai-is-client-observation": 16}`
- header lengths: `{"oai-client-build-number": [8], "oai-client-version": [45], "oai-device-id": [36], "oai-language": [5], "oai-session-id": [36], "oai-web-deployment-attestation": [291], "openai-sentinel-token": [5893, 6146], "x-oai-is-client-observation": [23]}`
- response text captured/omitted: `{"with_text": 278, "without_text": 309}`

### Stripe contract
- Elements query: `[{"index": 44, "keys": ["_stripe_version", "browser_timezone", "checkout_session_id", "client_betas[0]", "client_betas[1]", "currency", "deferred_intent[amount]", "deferred_intent[currency]", "deferred_intent[mode]", "deferred_intent[payment_method_configuration][id]", "deferred_intent[payment_method_types][0]", "deferred_intent[payment_method_types][1]", "deferred_intent[setup_future_usage]", "elements_init_source", "key", "locale", "referrer_host", "stripe_js_id", "type"], "selected": {"deferred_intent[mode]": "subscription", "deferred_intent[amount]": "34900000", "deferred_intent[currency]": "idr", "deferred_intent[setup_future_usage]": "off_session", "deferred_intent[payment_method_types][0]": "card", "deferred_intent[payment_method_types][1]": "gopay", "currency": "idr", "key": "<redacted>", "_stripe_version": "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1", "elements_init_source": "custom_checkout", "referrer_host": "chatgpt.com", "locale": "id", "type": "deferred_intent"}}]`
- progressive tax steps: `[{"index": 127, "fields": ["tax_region[country]"], "field_count": 1}, {"index": 222, "fields": ["tax_region[country]", "tax_region[line1]"], "field_count": 2}, {"index": 229, "fields": ["tax_region[country]", "tax_region[line1]", "tax_region[city]"], "field_count": 3}, {"index": 236, "fields": ["tax_region[country]", "tax_region[line1]", "tax_region[city]", "tax_region[state]"], "field_count": 4}, {"index": 259, "fields": ["tax_region[country]", "tax_region[line1]", "tax_region[city]", "tax_region[state]", "tax_region[postal_code]"], "field_count": 5}]`
- confirm summary: `[{"index": 270, "key_count": 62, "expected_amount": "34900000", "expected_payment_method_type": "gopay", "link_brand": "link", "payment_method_data[type]": "gopay", "payment_method_data[time_on_page]": "47421", "version": "b0f5e7abe5", "_stripe_version": "2025-03-31.basil%3B+checkout_server_update_beta%3Dv1%3B+checkout_manual_approval_preview%3Dv1", "init_checksum": "len=32 sha256=8cd37a4aff70ad96", "js_checksum": "len=100 sha256=8432dd57c52ed698"}]`

### Provider contract
- Stripe redirects: `[{"index": 294, "status": 302, "location_host": "app.midtrans.com", "location_path": "/snap/v4/redirection/<UUID>"}]`
- Midtrans transactions: `[{"index": 304, "status": 200, "amount": "349000", "currency": "IDR", "recommended": "gopay", "enabled_types": ["gopay", "qris"]}]`

### Relevant sequence
```text
0:/backend-api/sentinel/sdk.js -> 3:/backend-api/sentinel/frame.html -> 6:/backend-api/sentinel/req -> 7:/backend-api/sentinel/ping -> 8:/backend-api/payments/checkout -> 13:/backend-api/sentinel/req -> 29:/v1/payment_pages/cs_<CHECKOUT_SESSION>/init -> 44:/v1/elements/sessions -> 51:/backend-api/sentinel/req -> 71:/backend-api/sentinel/ping -> 105:/v1/consumers/sessions/lookup -> 127:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 222:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 229:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 233:/backend-api/payments/checkout/snapshot -> 236:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 240:/backend-api/payments/checkout/snapshot -> 241:/backend-api/payments/checkout/taxes -> 255:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 259:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 263:/backend-api/payments/checkout/taxes -> 266:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 270:/v1/payment_pages/cs_<CHECKOUT_SESSION>/confirm -> 275:/backend-api/sentinel/ping -> 277:/backend-api/payments/checkout/approve -> 288:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 289:/backend-api/sentinel/req -> 294:pm-redirects.stripe.com/authorize -> 298:/snap/v4/redirection -> 304:/snap/v1/transactions -> 305:/snap/v1/promos -> 306:/snap/v3/experiment
```

## Direct differences

| Metric | app.midtrans.com.har | app.midtrans.com1.har |
|---|---:|---:|
| HAR entries | 580 | 587 |
| ChatGPT entries | 36 | 35 |
| WebSocket entries | 0 | 1 |
| Sentinel token lengths | [6890, 8145] | [5893, 6146] |
| Approve/checkout statuses | [{'index': 263, 'method': 'POST', 'status': 200}] | [{'index': 277, 'method': 'POST', 'status': 200}] |
| Midtrans gross amount | [{'index': 300, 'status': 200, 'amount': '349000', 'currency': 'IDR', 'recommended': 'gopay', 'enabled_types': ['gopay', 'qris']}] | [{'index': 304, 'status': 200, 'amount': '349000', 'currency': 'IDR', 'recommended': 'gopay', 'enabled_types': ['gopay', 'qris']}] |

### Stable observations
- Both captures use `cs_live` Checkout, not `oaics_`.
- Both use `chatgpt_checkout` and `checkout_session_approval` Sentinel flows.
- Both keep `id-ID`, client build `10012890`, the same client version, and `Asia/Jakarta` Stripe browser timezone.
- Both have five progressive Stripe tax-region POSTs: country, line1, city, state, postal_code.
- Both end with a 302 from `pm-redirects.stripe.com` to an `app.midtrans.com` redirection page and a transaction response recommending GoPay.

### Variable observations
- Sentinel payload lengths, proof lengths, device/session identifiers, and Stripe checksums differ between captures.
- The second capture includes one `ws.chatgpt.com` WebSocket and more telemetry/FARO traffic.
- The first capture orders the second taxes/snapshot refresh before confirm differently from the second capture; stage code should not assume those background telemetry calls are globally serialized.
- ChatGPT payment request bodies and ChatGPT response bodies are not present in these HAR entries; their exact JSON fields cannot be inferred from this pair.
