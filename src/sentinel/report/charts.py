"""Sparkline PNGs — CID-embedded in email, data-URI in the HTML artifact."""
from __future__ import annotations

import base64
import io

import matplotlib

matplotlib.use("Agg")  # headless: no display on runners
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

COLOR_UP = "#1a7f37"
COLOR_DOWN = "#c0392b"


def sparkline_png(series: pd.Series | None, width_px: int = 120, height_px: int = 30) -> bytes | None:
    """Minimal axis-free line chart; green when the period is net-up, red when down."""
    if series is None:
        return None
    s = series.dropna()
    if len(s) < 2:
        return None
    color = COLOR_UP if s.iloc[-1] >= s.iloc[0] else COLOR_DOWN
    fig, ax = plt.subplots(figsize=(width_px / 100, height_px / 100), dpi=100)
    ax.plot(range(len(s)), s.values, linewidth=0.9, color=color)
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return buf.getvalue()


def data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
