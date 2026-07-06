"""Sparkline generation — output shape only, no pixel assertions."""
from __future__ import annotations

import pandas as pd

from sentinel.report.charts import data_uri, sparkline_png


def test_sparkline_is_png_bytes():
    png = sparkline_png(pd.Series([1.0, 2.0, 3.0, 2.5, 4.0]))
    assert png is not None
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_sparkline_needs_two_points():
    assert sparkline_png(pd.Series([1.0])) is None
    assert sparkline_png(pd.Series(dtype="float64")) is None
    assert sparkline_png(None) is None


def test_data_uri_prefix():
    png = sparkline_png(pd.Series([1.0, 2.0]))
    assert data_uri(png).startswith("data:image/png;base64,")
