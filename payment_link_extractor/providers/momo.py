from .base import ProviderAdapter


MOMO = ProviderAdapter(
    name="momo",
    result_field="momo_url",
    preferred_hosts=("payment.momo.vn", "momo.vn"),
)
