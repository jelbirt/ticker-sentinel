"""Pure technical indicators — PROJECT_PLAN.md section 5 technical overlay.

Plain pandas implementations (SMA/RSI are trivial; pandas-ta was skipped
deliberately — it is unmaintained against numpy 2.x). Series in, values out;
no I/O here. All ratios are fractions, RSI is 0–100.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

TREND_UP = "uptrend"
TREND_DOWN = "downtrend"
TREND_MIXED = "mixed"

CROSS_LOOKBACK = 10          # sessions to call a golden/death cross "recent"
RS_WINDOW = 63               # ~3 months of trading days
HIGH_WINDOW = 252            # ~52 weeks of trading days
VOL_FAST, VOL_SLOW = 20, 100


@dataclass
class TechnicalSnapshot:
    last_close: float | None = None
    last_bar: date | None = None
    sma50: float | None = None
    sma200: float | None = None
    trend_state: str | None = None       # uptrend / downtrend / mixed
    golden_cross_recent: bool = False
    death_cross_recent: bool = False
    rsi14: float | None = None
    rel_strength_3m: float | None = None
    dist_52w_high: float | None = None   # ≤ 0; 0 == at the 52-week high
    dist_52w_low: float | None = None    # ≥ 0; 0 == at the 52-week low
    vol_ratio: float | None = None


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(window=n, min_periods=n).mean()


def rsi14(close: pd.Series, n: int = 14) -> float | None:
    """Wilder's RSI (ewm alpha=1/n) on the last bar."""
    p = close.dropna()
    if len(p) < n + 1:
        return None
    delta = p.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().iloc[-1]
    if pd.isna(avg_gain) or pd.isna(avg_loss):
        return None
    if avg_loss == 0:
        return 100.0
    return float(100.0 - 100.0 / (1.0 + avg_gain / avg_loss))


def trend_state(price: float | None, sma50_v: float | None, sma200_v: float | None) -> str | None:
    if price is None or sma50_v is None or sma200_v is None:
        return None
    if price > sma50_v and price > sma200_v:
        return TREND_UP
    if price < sma50_v and price < sma200_v:
        return TREND_DOWN
    return TREND_MIXED


def recent_cross(
    sma_fast: pd.Series, sma_slow: pd.Series, lookback: int = CROSS_LOOKBACK
) -> str | None:
    """'golden' / 'death' if the fast SMA crossed the slow one within `lookback` sessions."""
    diff = (sma_fast - sma_slow).dropna()
    if len(diff) < 2:
        return None
    window = diff.iloc[-(lookback + 1) :]
    event: str | None = None
    for prev, cur in zip(window.iloc[:-1], window.iloc[1:]):
        if prev <= 0 < cur:
            event = "golden"
        elif prev >= 0 > cur:
            event = "death"
    return event


def rel_strength_3m(close: pd.Series, bench_close: pd.Series | None) -> float | None:
    """(P/P_63d) / (SPY/SPY_63d) − 1"""
    if bench_close is None:
        return None
    p, b = close.dropna(), bench_close.dropna()
    if len(p) < RS_WINDOW + 1 or len(b) < RS_WINDOW + 1:
        return None
    p_ratio = p.iloc[-1] / p.iloc[-(RS_WINDOW + 1)]
    b_ratio = b.iloc[-1] / b.iloc[-(RS_WINDOW + 1)]
    if b_ratio == 0:
        return None
    return float(p_ratio / b_ratio - 1)


def dist_52w_high(close: pd.Series) -> float | None:
    """P / max(P, 252d) − 1"""
    p = close.dropna()
    if p.empty:
        return None
    return float(p.iloc[-1] / p.iloc[-HIGH_WINDOW:].max() - 1)


def dist_52w_low(close: pd.Series) -> float | None:
    p = close.dropna()
    if p.empty:
        return None
    low = p.iloc[-HIGH_WINDOW:].min()
    if low == 0:
        return None
    return float(p.iloc[-1] / low - 1)


def vol_ratio(volume: pd.Series | None) -> float | None:
    """mean(volume, 20d) / mean(volume, 100d)"""
    if volume is None:
        return None
    v = volume.dropna()
    if len(v) < VOL_SLOW:
        return None
    slow = v.iloc[-VOL_SLOW:].mean()
    if slow == 0:
        return None
    return float(v.iloc[-VOL_FAST:].mean() / slow)


def compute_technicals(
    close: pd.Series | None,
    volume: pd.Series | None = None,
    bench_close: pd.Series | None = None,
) -> TechnicalSnapshot | None:
    """Full section-5 technical overlay for one ticker. None when no prices at all."""
    p = close.dropna() if close is not None else pd.Series(dtype="float64")
    if p.empty:
        return None
    s50, s200 = sma(p, 50), sma(p, 200)
    sma50_v = float(s50.iloc[-1]) if pd.notna(s50.iloc[-1]) else None
    sma200_v = float(s200.iloc[-1]) if pd.notna(s200.iloc[-1]) else None
    cross = recent_cross(s50, s200)
    last_bar = p.index[-1]
    return TechnicalSnapshot(
        last_close=float(p.iloc[-1]),
        last_bar=last_bar.date() if isinstance(last_bar, pd.Timestamp) else last_bar,
        sma50=sma50_v,
        sma200=sma200_v,
        trend_state=trend_state(float(p.iloc[-1]), sma50_v, sma200_v),
        golden_cross_recent=cross == "golden",
        death_cross_recent=cross == "death",
        rsi14=rsi14(p),
        rel_strength_3m=rel_strength_3m(p, bench_close),
        dist_52w_high=dist_52w_high(p),
        dist_52w_low=dist_52w_low(p),
        vol_ratio=vol_ratio(volume),
    )
