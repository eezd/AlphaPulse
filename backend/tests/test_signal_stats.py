from datetime import datetime, timedelta

import pytest

from alphapulse.trading.signal_stats import Signal, dedupe, summarize

T0 = datetime(2026, 10, 16, 15, 0)


def signal(minutes: float, category: str, direction: str, entry: str = "100", after: str = "101") -> Signal:
    return Signal(T0 + timedelta(minutes=minutes), category, direction, {"BTCUSDT": {"entry": entry, "1h": after, "4h": after}})


def test_dedupe_keeps_first_report_of_same_story_per_category():
    kept = dedupe([signal(0, "event", "bullish"), signal(5, "event", "bearish"), signal(5, "price_report", "bullish"), signal(11, "event", "bullish")])
    assert [(s.published_at - T0, s.category) for s in kept] == [
        (timedelta(0), "event"), (timedelta(minutes=5), "price_report"), (timedelta(minutes=11), "event"),
    ]


def test_summarize_trades_label_direction_and_skips_neutral():
    rows = summarize([
        signal(0, "event", "bullish", after="101"),    # 做多 +100bp
        signal(20, "event", "bearish", after="101"),   # 做空 −100bp
        signal(40, "event", "bearish", after="98"),    # 做空 +200bp
        signal(60, "event", "neutral", after="150"),   # 中性不交易
    ], ("BTCUSDT",), ("4h",))
    assert len(rows) == 1
    row = rows[0]
    assert (row["n"], row["win_rate"]) == (3, pytest.approx(2 / 3))
    assert row["mean_bp"] == pytest.approx(200 / 3)
    assert row["net_bp"] == pytest.approx(200 / 3 - 8)
