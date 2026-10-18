"""Keep the GMX wallet protocol separate from historical CCXT adapters."""
from config import Settings


def build_client(settings: Settings):
    if settings.exchange_id == "gmx":
        from gmx_client import GmxClient
        return GmxClient(settings)
    from exchange_client import ExchangeClient
    return ExchangeClient(settings)
