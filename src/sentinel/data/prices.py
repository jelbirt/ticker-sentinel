"""Daily close prices: yfinance batch download with Twelve Data fallback.

Phase 1 uses prices only for the report header (benchmark change); Phase 2's
technical overlay consumes the same frame.
"""
from __future__ import annotations

import logging
import os
import time

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
TWELVEDATA_FREE_PER_MIN = 8  # free-tier request cap; pacing kicks in above this


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20), reraise=True)
def _yf_download(tickers: list[str], period: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    import yfinance as yf

    raw = yf.download(tickers, period=period, auto_adjust=True, progress=False, group_by="column")
    if raw is None or raw.empty:
        raise RuntimeError("yfinance returned no data")
    if isinstance(raw.columns, pd.MultiIndex):
        close, volume = raw["Close"], raw["Volume"]
    else:  # single ticker: flat columns
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})
        volume = raw[["Volume"]].rename(columns={"Volume": tickers[0]})
    return close.dropna(how="all"), volume.dropna(how="all")


def _twelvedata_series(ticker: str, api_key: str, bars: int = 260) -> tuple[pd.Series, pd.Series] | None:
    try:
        resp = requests.get(
            TWELVEDATA_URL,
            params={
                "symbol": ticker,
                "interval": "1day",
                "outputsize": bars,
                "apikey": api_key,
            },
            timeout=30,
        )
        payload = resp.json()
        values = payload.get("values")
        if not values:
            log.warning("twelvedata: no data for %s: %s", ticker, payload.get("message"))
            return None
        idx = [pd.to_datetime(v["datetime"]) for v in values]
        close = pd.Series([float(v["close"]) for v in values], index=idx, name=ticker)
        volume = pd.Series([float(v.get("volume") or "nan") for v in values], index=idx, name=ticker)
        return close.sort_index(), volume.sort_index()
    except Exception as exc:
        log.warning("twelvedata fetch failed for %s: %s", ticker, exc)
        return None


def fetch_prices(
    tickers: list[str], period: str = "1y"
) -> tuple[pd.DataFrame | None, pd.DataFrame | None, list[str]]:
    """One batched pull; per-ticker Twelve Data fallback for whatever is missing.

    Returns (close frame, volume frame, human-readable degradation notes).
    """
    notes: list[str] = []
    close: pd.DataFrame | None = None
    volume: pd.DataFrame | None = None
    try:
        close, volume = _yf_download(tickers, period)
    except Exception as exc:
        notes.append(f"yfinance price download failed ({exc})")

    missing = [
        t for t in tickers if close is None or t not in close.columns or close[t].dropna().empty
    ]
    if missing:
        api_key = os.environ.get("TWELVEDATA_API_KEY")
        if not api_key:
            notes.append(f"prices missing for {', '.join(missing)} (no TWELVEDATA_API_KEY set)")
        else:
            recovered = []
            # a full-Yahoo outage on a larger watchlist would blow the free
            # tier's per-minute cap; pace only then (a paced 26-name sweep
            # takes ~3 minutes, still well inside the job timeout)
            pace = len(missing) > TWELVEDATA_FREE_PER_MIN
            for i, t in enumerate(missing):
                if pace and i:
                    time.sleep(60 / TWELVEDATA_FREE_PER_MIN)
                series = _twelvedata_series(t, api_key)
                if series is not None:
                    c, v = series
                    close = c.to_frame() if close is None else close.join(c, how="outer")
                    volume = v.to_frame() if volume is None else volume.join(v, how="outer")
                    recovered.append(t)
            if recovered:
                notes.append(f"prices via Twelve Data fallback: {', '.join(recovered)}")
            still = sorted(set(missing) - set(recovered))
            if still:
                notes.append(f"prices unavailable: {', '.join(still)}")
    return close, volume, notes
