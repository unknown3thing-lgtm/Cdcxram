"""API layer: CoinDCX public market-data client, models, validators,
and a clearly-labelled synthetic data fallback.
"""

from .models import Candle, PairInfo, DataProvenance, SourceKind  # noqa: F401
from .client import CoinDCXClient, CoinDCXAPIError, RateLimitError  # noqa: F401
from .validate import (  # noqa: F401
    validate_candles,
    drop_incomplete_latest,
    drop_duplicate_timestamps,
    dedupe_and_sort,
    find_gaps,
    OHLCVValidationError,
)
from .synthetic import SyntheticGenerator  # noqa: F401

__all__ = [
    "Candle",
    "PairInfo",
    "DataProvenance",
    "SourceKind",
    "CoinDCXClient",
    "CoinDCXAPIError",
    "RateLimitError",
    "validate_candles",
    "drop_incomplete_latest",
    "drop_duplicate_timestamps",
    "dedupe_and_sort",
    "find_gaps",
    "OHLCVValidationError",
    "SyntheticGenerator",
]
