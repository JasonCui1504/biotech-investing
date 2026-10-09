"""Offline tests for universe parsing and price-row conversion (no network)."""
import pandas as pd

from app.ingest.prices import frame_to_rows
from app.ingest.universe import is_plain_common_stock, parse_pipe_file

SAMPLE = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
SMMT|Summit Therapeutics Inc. - Common Stock|G|N|N|100|N|N
ABCDW|Abcd Bio Inc. - Warrants|S|N|N|100|N|N
File Creation Time: 1009202605:00|||||||
"""


def test_parse_pipe_file_skips_footer():
    rows = parse_pipe_file(SAMPLE)
    assert len(rows) == 2
    assert rows[0]["Symbol"] == "SMMT"


def test_common_stock_filter():
    assert is_plain_common_stock("Summit Therapeutics Inc. - Common Stock", "SMMT")
    assert not is_plain_common_stock("Abcd Bio Inc. - Warrants", "ABCDW")
    assert not is_plain_common_stock("Abcd Bio Inc. - Units", "ABCDU")
    assert not is_plain_common_stock("Abcd Bio - Preferred Stock Common", "ABCD$A")


def test_frame_to_rows():
    frame = pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5],
         "Adj Close": [1.4], "Volume": [1000]},
        index=pd.to_datetime(["2026-01-02"]))
    rows = frame_to_rows("SMMT", frame)
    assert rows == [("SMMT", "2026-01-02", 1.0, 2.0, 0.5, 1.5, 1.4, 1000.0)]
