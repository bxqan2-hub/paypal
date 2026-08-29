from payment_link_extractor.config import (
    OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION,
    STRIPE_RUNTIME_VERSION,
)


def test_paypal_and_gopay_use_restored_sdk_version() -> None:
    assert STRIPE_RUNTIME_VERSION == "692f102a8f"
    assert OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION == "692f102a8f"
