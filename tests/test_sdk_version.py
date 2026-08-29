from payment_link_extractor.config import (
    OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION,
    STRIPE_RUNTIME_VERSION,
)


def test_paypal_and_gopay_use_sdk_version_0810() -> None:
    assert STRIPE_RUNTIME_VERSION == "0810"
    assert OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION == "0810"
