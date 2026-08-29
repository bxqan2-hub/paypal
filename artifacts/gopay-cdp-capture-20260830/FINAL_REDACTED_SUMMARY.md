# GoPay CDP capture summary (redacted)

> This report is derived from local CDP data. Raw credentials, cookies, tokens, session IDs, customer data, order IDs, and redirect nonces are not emitted.

- source: `C:\Users\Administrator\Desktop\提链\artifacts-local\gopay-cdp-capture-browser-targets-20260830-next.har`
- size_bytes: `16491140`
- sha256: `8DF5163E0A2D57598B257435C2449EA0371A236C6114BAE85234A94108547E50`
- entries: `483`
- hosts: `{"": 6, "api.stripe.com": 12, "app.midtrans.com": 4, "b.stripecdn.com": 2, "chatgpt.com": 40, "cloudauth-device-dualstack.ap-southeast-1.aliyuncs.com": 2, "flagcdn.com": 254, "fonts.googleapis.com": 1, "fonts.gstatic.com": 1, "g.alicdn.com": 3, "global.faro.katulampa.gopay.sh": 17, "js.stripe.com": 28, "m.stripe.com": 1, "m.stripe.network": 1, "merchant-ui-api.stripe.com": 1, "o.alicdn.com": 1, "pm-redirects.stripe.com": 1, "r.stripe.com": 94, "snap-assets.midtrans.com": 7, "snap-web-raccoon.gojekapi.com": 4, "upload.captcha-open-southeast.aliyuncs.com": 2, "y1rdnbp.captcha-open-southeast.aliyuncs.com": 1}`
- statuses: `{"0": 13, "200": 442, "202": 19, "204": 7, "302": 1, "304": 1}`
- methods: `{"GET": 326, "OPTIONS": 7, "POST": 150}`

## Endpoint coverage

```json
{
  "/backend-api/sentinel/req": {
    "count": 4,
    "statuses": {
      "200": 4
    }
  },
  "/backend-api/sentinel/ping": {
    "count": 3,
    "statuses": {
      "200": 3
    }
  },
  "/backend-api/payments/checkout": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  },
  "/v1/payment_pages/cs_live_a1Dgy8O9P5RcCu3oI6bcWMVCGziwj8xo6vCfrtWfewVTRKe8iJHdHKFF25/init": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  },
  "/v1/elements/sessions": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  },
  "/v1/consumers/sessions/lookup": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  },
  "/v1/payment_pages/cs_live_a1Dgy8O9P5RcCu3oI6bcWMVCGziwj8xo6vCfrtWfewVTRKe8iJHdHKFF25": {
    "count": 8,
    "statuses": {
      "200": 8
    }
  },
  "/backend-api/payments/checkout/snapshot": {
    "count": 2,
    "statuses": {
      "204": 2
    }
  },
  "/backend-api/payments/checkout/taxes": {
    "count": 2,
    "statuses": {
      "200": 2
    }
  },
  "/v1/payment_pages/cs_live_a1Dgy8O9P5RcCu3oI6bcWMVCGziwj8xo6vCfrtWfewVTRKe8iJHdHKFF25/confirm": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  },
  "/backend-api/payments/checkout/approve": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  },
  "pm-redirects.stripe.com/authorize": {
    "count": 1,
    "statuses": {
      "302": 1
    }
  },
  "/snap/v4/redirection/<UUID>": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  },
  "/snap/v1/transactions/<UUID>": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  },
  "/snap/v1/promos/<UUID>/search": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  },
  "/snap/v3/experiment": {
    "count": 1,
    "statuses": {
      "200": 1
    }
  }
}
```

## Sentinel and identity

- flows: `{"chatgpt_checkout": 2, "checkout_session_approval": 2}`
- payload shapes: `[{"index": 11, "flow": "chatgpt_checkout", "id": "len=36 sha256=7075a99ac7eeaecc", "p": "len=617 sha256=86a325080dd70670"}, {"index": 15, "flow": "chatgpt_checkout", "id": "len=36 sha256=7075a99ac7eeaecc", "p": "len=617 sha256=86a325080dd70670"}, {"index": 44, "flow": "checkout_session_approval", "id": "len=36 sha256=7075a99ac7eeaecc", "p": "len=617 sha256=86a325080dd70670"}, {"index": 182, "flow": "checkout_session_approval", "id": "len=36 sha256=7075a99ac7eeaecc", "p": "len=617 sha256=86a325080dd70670"}]`
- header presence: `{"oai-client-build-number": 20, "oai-client-version": 20, "oai-device-id": 20, "oai-language": 20, "oai-session-id": 20, "oai-web-deployment-attestation": 6, "openai-sentinel-token": 2, "x-oai-is-client-observation": 18}`
- header lengths: `{"oai-client-build-number": [8], "oai-client-version": [45], "oai-device-id": [36], "oai-language": [5], "oai-session-id": [36], "oai-web-deployment-attestation": [291], "openai-sentinel-token": [6745, 6878], "x-oai-is-client-observation": [23]}`

## ChatGPT body summaries

```json
[
  {
    "index": 11,
    "path": "/backend-api/sentinel/req",
    "method": "POST",
    "status": 200,
    "request_len": 695,
    "response_len": 23459,
    "request_keys": [
      "flow",
      "id",
      "p"
    ],
    "response_keys": [
      "expire_after",
      "expire_at",
      "persona",
      "proofofwork",
      "token",
      "turnstile"
    ]
  },
  {
    "index": 12,
    "path": "/backend-api/sentinel/ping",
    "method": "POST",
    "status": 200,
    "request_len": 0,
    "response_len": 15,
    "request_keys": [],
    "response_keys": [
      "status"
    ]
  },
  {
    "index": 13,
    "path": "/backend-api/payments/checkout",
    "method": "POST",
    "status": 200,
    "request_len": 151,
    "response_len": 1812,
    "request_keys": [
      "billing_details",
      "checkout_ui_mode",
      "entry_point",
      "plan_name"
    ],
    "response_keys": [
      "automatic_tax_enabled",
      "billing_details",
      "business_maximum_seats",
      "checkout_kind",
      "checkout_provider",
      "checkout_session_id",
      "checkout_snapshot",
      "checkout_state",
      "checkout_ui_mode",
      "client_secret",
      "confirm_return_url",
      "credit_discount_offer",
      "credit_purchase_auto_top_up",
      "custom_payment_methods",
      "customer_session_client_secret",
      "entry_point",
      "experiments",
      "immediate_discount_settings",
      "info_by_stripe_line_item_id",
      "is_new_stripe_customer",
      "one_click_trial_eligible",
      "payment_method_collection",
      "payment_method_types",
      "payment_status",
      "plan_name",
      "processor_entity",
      "promo_campaign",
      "promo_credit_grant",
      "publishable_key",
      "reactivation_member_management",
      "requires_manual_approval",
      "scheduled_discount_preview",
      "selected_payment_method_type",
      "status",
      "tag",
      "tax_types",
      "upsell_context_id",
      "url"
    ],
    "safe_request": {
      "entry_point": "all_plans_pricing_modal",
      "plan_name": "chatgptplusplan",
      "checkout_ui_mode": "custom",
      "billing_country": "ID",
      "billing_currency": "IDR"
    },
    "safe_response": {
      "checkout_provider": "stripe",
      "processor_entity": "openai_llc",
      "status": "open",
      "payment_status": "unpaid",
      "requires_manual_approval": true,
      "automatic_tax_enabled": true,
      "billing_details": {
        "country": "ID",
        "currency": "IDR"
      }
    }
  },
  {
    "index": 15,
    "path": "/backend-api/sentinel/req",
    "method": "POST",
    "status": 200,
    "request_len": 695,
    "response_len": 24366,
    "request_keys": [
      "flow",
      "id",
      "p"
    ],
    "response_keys": [
      "expire_after",
      "expire_at",
      "persona",
      "proofofwork",
      "token",
      "turnstile"
    ]
  },
  {
    "index": 44,
    "path": "/backend-api/sentinel/req",
    "method": "POST",
    "status": 200,
    "request_len": 704,
    "response_len": 24130,
    "request_keys": [
      "flow",
      "id",
      "p"
    ],
    "response_keys": [
      "expire_after",
      "expire_at",
      "persona",
      "proofofwork",
      "token",
      "turnstile"
    ]
  },
  {
    "index": 48,
    "path": "/backend-api/sentinel/ping",
    "method": "POST",
    "status": 200,
    "request_len": 0,
    "response_len": 15,
    "request_keys": [],
    "response_keys": [
      "status"
    ]
  },
  {
    "index": 138,
    "path": "/backend-api/payments/checkout/snapshot",
    "method": "POST",
    "status": 204,
    "request_len": 205,
    "response_len": 0,
    "request_keys": [
      "snapshot"
    ],
    "response_keys": [],
    "safe_request": {
      "snapshot_keys": [
        "billing_address"
      ],
      "address_keys": [
        "city",
        "country",
        "line1",
        "postal_code",
        "state"
      ]
    }
  },
  {
    "index": 143,
    "path": "/backend-api/payments/checkout/snapshot",
    "method": "POST",
    "status": 204,
    "request_len": 210,
    "response_len": 0,
    "request_keys": [
      "snapshot"
    ],
    "response_keys": [],
    "safe_request": {
      "snapshot_keys": [
        "billing_address"
      ],
      "address_keys": [
        "city",
        "country",
        "line1",
        "postal_code",
        "state"
      ]
    }
  },
  {
    "index": 148,
    "path": "/backend-api/payments/checkout/taxes",
    "method": "POST",
    "status": 200,
    "request_len": 399,
    "response_len": 4689,
    "request_keys": [
      "billing_address",
      "billing_country",
      "billing_name",
      "checkout_email",
      "checkout_session_id",
      "currency",
      "processor_entity"
    ],
    "response_keys": [
      "checkout_session",
      "using_automatic_tax"
    ],
    "safe_request": {
      "billing_country": "ID",
      "currency": "idr",
      "processor_entity": "openai_llc",
      "billing_address_keys": [
        "city",
        "country",
        "line1",
        "postal_code",
        "state"
      ]
    },
    "safe_response": {
      "using_automatic_tax": true,
      "amount_subtotal": 34900000,
      "amount_total": 34900000,
      "currency": "idr",
      "payment_method_types": [
        "card",
        "gopay"
      ],
      "payment_status": "unpaid",
      "mode": "subscription",
      "approval_method": "manual",
      "automatic_tax": {
        "enabled": true,
        "status": "complete"
      },
      "amount_discount": 0,
      "amount_tax": 3458559
    }
  },
  {
    "index": 157,
    "path": "/backend-api/payments/checkout/taxes",
    "method": "POST",
    "status": 200,
    "request_len": 404,
    "response_len": 4689,
    "request_keys": [
      "billing_address",
      "billing_country",
      "billing_name",
      "checkout_email",
      "checkout_session_id",
      "currency",
      "processor_entity"
    ],
    "response_keys": [
      "checkout_session",
      "using_automatic_tax"
    ],
    "safe_request": {
      "billing_country": "ID",
      "currency": "idr",
      "processor_entity": "openai_llc",
      "billing_address_keys": [
        "city",
        "country",
        "line1",
        "postal_code",
        "state"
      ]
    },
    "safe_response": {
      "using_automatic_tax": true,
      "amount_subtotal": 34900000,
      "amount_total": 34900000,
      "currency": "idr",
      "payment_method_types": [
        "card",
        "gopay"
      ],
      "payment_status": "unpaid",
      "mode": "subscription",
      "approval_method": "manual",
      "automatic_tax": {
        "enabled": true,
        "status": "complete"
      },
      "amount_discount": 0,
      "amount_tax": 3458559
    }
  },
  {
    "index": 172,
    "path": "/backend-api/sentinel/ping",
    "method": "POST",
    "status": 200,
    "request_len": 0,
    "response_len": 15,
    "request_keys": [],
    "response_keys": [
      "status"
    ]
  },
  {
    "index": 173,
    "path": "/backend-api/payments/checkout/approve",
    "method": "POST",
    "status": 200,
    "request_len": 124,
    "response_len": 21,
    "request_keys": [
      "checkout_session_id",
      "processor_entity"
    ],
    "response_keys": [
      "result"
    ],
    "safe_request": {
      "processor_entity": "openai_llc"
    },
    "safe_response": {
      "result": "approved"
    }
  },
  {
    "index": 182,
    "path": "/backend-api/sentinel/req",
    "method": "POST",
    "status": 200,
    "request_len": 704,
    "response_len": 23690,
    "request_keys": [
      "flow",
      "id",
      "p"
    ],
    "response_keys": [
      "expire_after",
      "expire_at",
      "persona",
      "proofofwork",
      "token",
      "turnstile"
    ]
  }
]
```

## Midtrans

```json
[
  {
    "index": 192,
    "status": 200,
    "gross_amount": "349000",
    "currency": "IDR",
    "recommended_payment_method": "gopay",
    "enabled_payments": [
      "gopay",
      "qris"
    ],
    "gopay_keys": [
      "blacklist_country_codes",
      "enforce_tokenization",
      "tokenization",
      "whitelist_country_codes"
    ]
  }
]
```

## Relevant sequence

```text
11:/backend-api/sentinel/req -> 12:/backend-api/sentinel/ping -> 13:/backend-api/payments/checkout -> 15:/backend-api/sentinel/req -> 30:/v1/payment_pages/cs_<CHECKOUT_SESSION>/init -> 38:/v1/elements/sessions -> 44:/backend-api/sentinel/req -> 48:/backend-api/sentinel/ping -> 58:/v1/consumers/sessions/lookup -> 110:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 128:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 133:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 138:/backend-api/payments/checkout/snapshot -> 139:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 143:/backend-api/payments/checkout/snapshot -> 148:/backend-api/payments/checkout/taxes -> 151:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 154:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 157:/backend-api/payments/checkout/taxes -> 160:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 169:/v1/payment_pages/cs_<CHECKOUT_SESSION>/confirm -> 172:/backend-api/sentinel/ping -> 173:/backend-api/payments/checkout/approve -> 176:/v1/payment_pages/cs_<CHECKOUT_SESSION> -> 181:pm-redirects.stripe.com/authorize -> 182:/backend-api/sentinel/req -> 185:/snap/v4/redirection/<UUID> -> 192:/snap/v1/transactions/<UUID> -> 193:/snap/v1/promos/<UUID>/search -> 194:/snap/v3/experiment
```

## Coverage finding

- `api.stripe.com` entries: `12`
- `js.stripe.com` entries: `28`
- ChatGPT, Stripe API init/elements/tax_region/confirm, and Midtrans bodies are present in this capture.
- The critical GoPay completeness audit is complete; no critical checkpoint is missing.
