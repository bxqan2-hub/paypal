from .base import ProviderAdapter


GOPAY_PRO = ProviderAdapter(
    name="gopay_pro",
    result_field="gopay_pro_url",
    preferred_hosts=(
        "app.midtrans.com",
        "gopay.co.id",
        "app.gopay.co.id",
        "gojek.link",
        "gopayapp.page.link",
        "gojek.page.link",
        "pm-redirects.stripe.com",
    ),
)
