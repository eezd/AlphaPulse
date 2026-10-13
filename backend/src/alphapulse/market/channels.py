from alphapulse.core.redis import KEY_PREFIX


def kline_channel(market: str, symbol: str, interval: str) -> str:
    return f"{KEY_PREFIX}pub:kline:{market}:{symbol}:{interval}"


def ticker_channel(market: str, symbol: str) -> str:
    return f"{KEY_PREFIX}pub:ticker:{market}:{symbol}"
