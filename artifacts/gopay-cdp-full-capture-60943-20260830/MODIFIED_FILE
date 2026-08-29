# GoPay CDP capture summary (redacted)

> This report is derived from local CDP data. Raw credentials, cookies, tokens, session IDs, customer data, order IDs, and redirect nonces are not emitted.

- source: `C:\Users\Administrator\Desktop\提链\artifacts-local\gopay-cdp-capture-browser-targets-20260830-60943-fixed.har`
- size_bytes: `16155304`
- sha256: `7E7AB2715B3728314C67CBAD5C477E44FC353F0AC00ADAACC0320F06FA3A48C1`
- entries: `401`
- hosts: `{"alb.reddit.com": 1, "analytics.tiktok.com": 4, "app.midtrans.com": 5, "bat.bing.com": 5, "chatgpt.com": 64, "cloudauth-device-dualstack.ap-southeast-1.aliyuncs.com": 2, "connect.facebook.net": 2, "flagcdn.com": 254, "fonts.googleapis.com": 1, "fonts.gstatic.com": 1, "g.alicdn.com": 3, "global.faro.katulampa.gopay.sh": 19, "googleads.g.doubleclick.net": 2, "js.stripe.com": 12, "o.alicdn.com": 1, "pixel-config.reddit.com": 1, "pm-redirects.stripe.com": 1, "snap-assets.midtrans.com": 7, "snap-web-raccoon.gojekapi.com": 4, "upload.captcha-open-southeast.aliyuncs.com": 1, "www.facebook.com": 2, "www.google.co.id": 2, "www.google.com": 2, "www.googleadservices.com": 2, "www.googletagmanager.com": 1, "www.redditstatic.com": 1, "y1rdnbp.captcha-open-southeast.aliyuncs.com": 1}`
- statuses: `{"0": 6, "200": 355, "202": 18, "204": 14, "302": 5, "304": 3}`
- methods: `{"GET": 343, "OPTIONS": 11, "POST": 47}`

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
  "/backend-api/payments/checkout/taxes": {
    "count": 2,
    "statuses": {
      "200": 2
    }
  },
  "/backend-api/payments/checkout/snapshot": {
    "count": 2,
    "statuses": {
      "204": 2
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
- payload shapes: `[{"index": 33, "flow": "chatgpt_checkout", "id": "len=36 sha256=7075a99ac7eeaecc", "p": "len=625 sha256=f2759791be38a01d"}, {"index": 37, "flow": "chatgpt_checkout", "id": "len=36 sha256=7075a99ac7eeaecc", "p": "len=625 sha256=f2759791be38a01d"}, {"index": 58, "flow": "checkout_session_approval", "id": "len=36 sha256=7075a99ac7eeaecc", "p": "len=625 sha256=f2759791be38a01d"}, {"index": 97, "flow": "checkout_session_approval", "id": "len=36 sha256=7075a99ac7eeaecc", "p": "len=625 sha256=f2759791be38a01d"}]`
- header presence: `{"oai-client-build-number": 21, "oai-client-version": 21, "oai-device-id": 21, "oai-language": 21, "oai-session-id": 21, "oai-web-deployment-attestation": 6, "openai-sentinel-token": 2, "x-oai-is-client-observation": 19}`
- header lengths: `{"oai-client-build-number": [8], "oai-client-version": [45], "oai-device-id": [36], "oai-language": [5], "oai-session-id": [36], "oai-web-deployment-attestation": [291], "openai-sentinel-token": [6421, 6858], "x-oai-is-client-observation": [23]}`

## ChatGPT body summaries

```json
[
  {
    "index": 33,
    "path": "/backend-api/sentinel/req",
    "method": "POST",
    "status": 200,
    "request_len": 703,
    "response_len": 22170,
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
    "index": 34,
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
    "index": 35,
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
    "index": 37,
    "path": "/backend-api/sentinel/req",
    "method": "POST",
    "status": 200,
    "request_len": 703,
    "response_len": 27850,
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
    "index": 58,
    "path": "/backend-api/sentinel/req",
    "method": "POST",
    "status": 200,
    "request_len": 712,
    "response_len": 20581,
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
    "index": 59,
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
    "index": 66,
    "path": "/backend-api/payments/checkout/taxes",
    "method": "POST",
    "status": 200,
    "request_len": 400,
    "response_len": 4687,
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
    "index": 67,
    "path": "/backend-api/payments/checkout/snapshot",
    "method": "POST",
    "status": 204,
    "request_len": 206,
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
    "index": 68,
    "path": "/backend-api/payments/checkout/snapshot",
    "method": "POST",
    "status": 204,
    "request_len": 211,
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
    "index": 77,
    "path": "/backend-api/payments/checkout/taxes",
    "method": "POST",
    "status": 200,
    "request_len": 405,
    "response_len": 4687,
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
    "index": 91,
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
    "index": 92,
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
    "index": 97,
    "path": "/backend-api/sentinel/req",
    "method": "POST",
    "status": 200,
    "request_len": 712,
    "response_len": 24682,
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
    "index": 108,
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
33:/backend-api/sentinel/req -> 34:/backend-api/sentinel/ping -> 35:/backend-api/payments/checkout -> 37:/backend-api/sentinel/req -> 58:/backend-api/sentinel/req -> 59:/backend-api/sentinel/ping -> 66:/backend-api/payments/checkout/taxes -> 67:/backend-api/payments/checkout/snapshot -> 68:/backend-api/payments/checkout/snapshot -> 77:/backend-api/payments/checkout/taxes -> 91:/backend-api/sentinel/ping -> 92:/backend-api/payments/checkout/approve -> 97:/backend-api/sentinel/req -> 99:pm-redirects.stripe.com/authorize -> 102:/snap/v4/redirection/<UUID> -> 108:/snap/v1/transactions/<UUID> -> 109:/snap/v1/promos/<UUID>/search -> 110:/snap/v3/experiment
```

## Coverage finding

- `api.stripe.com` entries: `0`
- `js.stripe.com` entries: `12`
- ChatGPT and Midtrans bodies are present; Stripe API init/elements/tax_region/confirm are absent from this capture.
- The absence is an observed target-coverage result, not a fabricated response or replayed request.
