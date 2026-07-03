"""Omni Trades-CSV position reconstruction tests — local files only, no network."""

from __future__ import annotations

import pytest

from app.pipeline import fetch_open_positions
from config import ManualPosition
from data.omni_trades import OmniTradesError, positions_from_trades_csv
from tests.test_app import make_config
from tests.test_provider import make_router

HEADER = (
    "Trade ID,Timestamp,Side,Underlying Asset Ticker,Execution Price,Quantity,Trade Type,Status"
)


def write_csv(tmp_path, rows: list[str], header: str = HEADER):
    path = tmp_path / "omni_trades.csv"
    path.write_text("\n".join([header, *rows]) + "\n")
    return path


def test_single_buy_opens_a_long(tmp_path):
    path = write_csv(tmp_path, ["1,2026-06-01T10:00:00Z,buy,ETH,2000,1.5,trade,confirmed"])
    (pos,) = positions_from_trades_csv(path)
    assert (pos.asset, pos.side, pos.size, pos.entry) == ("omni:ETH", "long", 1.5, 2000.0)


def test_two_buys_weight_the_entry(tmp_path):
    path = write_csv(
        tmp_path,
        [
            "1,2026-06-01T10:00:00Z,buy,ETH,2000,1,trade,confirmed",
            "2,2026-06-02T10:00:00Z,buy,ETH,3000,1,trade,confirmed",
        ],
    )
    (pos,) = positions_from_trades_csv(path)
    assert pos.size == 2.0
    assert pos.entry == 2500.0  # (2000*1 + 3000*1) / 2


def test_partial_close_keeps_entry_full_close_disappears(tmp_path):
    path = write_csv(
        tmp_path,
        [
            "1,2026-06-01T10:00:00Z,buy,ETH,2000,2,trade,confirmed",
            "2,2026-06-02T10:00:00Z,sell,ETH,2500,1,trade,confirmed",
        ],
    )
    (pos,) = positions_from_trades_csv(path)
    assert (pos.side, pos.size, pos.entry) == ("long", 1.0, 2000.0)

    path = write_csv(
        tmp_path,
        [
            "1,2026-06-01T10:00:00Z,buy,ETH,2000,2,trade,confirmed",
            "2,2026-06-02T10:00:00Z,sell,ETH,2500,2,trade,confirmed",
        ],
    )
    assert positions_from_trades_csv(path) == []


def test_flip_through_zero_restarts_entry_at_fill_price(tmp_path):
    path = write_csv(
        tmp_path,
        [
            "1,2026-06-01T10:00:00Z,buy,ETH,2000,1,trade,confirmed",
            "2,2026-06-02T10:00:00Z,sell,ETH,2600,3,trade,confirmed",
        ],
    )
    (pos,) = positions_from_trades_csv(path)
    assert (pos.side, pos.size, pos.entry) == ("short", 2.0, 2600.0)


def test_sell_first_opens_a_short_and_liquidation_closes_it(tmp_path):
    path = write_csv(
        tmp_path,
        [
            "1,2026-06-01T10:00:00Z,sell,SOL,150,10,trade,confirmed",
            "2,2026-06-05T10:00:00Z,buy,SOL,180,10,liquidation,confirmed",
        ],
    )
    assert positions_from_trades_csv(path) == []


def test_rows_are_replayed_in_timestamp_order_not_file_order(tmp_path):
    # Exports are often newest-first; the flip only reconstructs correctly
    # when replayed chronologically.
    path = write_csv(
        tmp_path,
        [
            "2,2026-06-02T10:00:00Z,sell,ETH,2600,3,trade,confirmed",
            "1,2026-06-01T10:00:00Z,buy,ETH,2000,1,trade,confirmed",
        ],
    )
    (pos,) = positions_from_trades_csv(path)
    assert (pos.side, pos.size, pos.entry) == ("short", 2.0, 2600.0)


def test_dead_statuses_and_duplicate_ids_are_ignored(tmp_path):
    path = write_csv(
        tmp_path,
        [
            "1,2026-06-01T10:00:00Z,buy,ETH,2000,1,trade,confirmed",
            "1,2026-06-01T10:00:00Z,buy,ETH,2000,1,trade,confirmed",  # duplicate id
            "2,2026-06-02T10:00:00Z,buy,ETH,9999,5,trade,cancelled",  # never filled
        ],
    )
    (pos,) = positions_from_trades_csv(path)
    assert pos.size == 1.0


def test_ticker_quote_suffix_and_multiple_assets(tmp_path):
    path = write_csv(
        tmp_path,
        [
            "1,2026-06-01T10:00:00Z,buy,ETH-USDC,2000,1,trade,confirmed",
            "2,2026-06-01T11:00:00Z,sell,BTC/USDC,60000,0.5,trade,confirmed",
        ],
    )
    positions = positions_from_trades_csv(path)
    assert [(p.asset, p.side) for p in positions] == [
        ("omni:BTC", "short"),
        ("omni:ETH", "long"),
    ]


def test_alternate_headers_are_recognized(tmp_path):
    path = write_csv(
        tmp_path,
        ["2026-06-01T10:00:00Z,long,eth,2000,1"],
        header="created_at,direction,symbol,price,size",
    )
    (pos,) = positions_from_trades_csv(path)
    assert (pos.asset, pos.side, pos.size) == ("omni:ETH", "long", 1.0)


def test_unknown_headers_raise_a_helpful_error(tmp_path):
    path = write_csv(tmp_path, ["x,y"], header="foo,bar")
    with pytest.raises(OmniTradesError, match="headers found.*foo"):
        positions_from_trades_csv(path)


def test_missing_file_raises_with_guidance(tmp_path):
    with pytest.raises(OmniTradesError, match="not found"):
        positions_from_trades_csv(tmp_path / "nope.csv")


def test_unrecognized_side_raises(tmp_path):
    path = write_csv(tmp_path, ["1,2026-06-01T10:00:00Z,hodl,ETH,2000,1,trade,confirmed"])
    with pytest.raises(OmniTradesError, match="side"):
        positions_from_trades_csv(path)


# -- pipeline integration ----------------------------------------------------------


def test_fetch_open_positions_merges_csv_and_manual_wins(tmp_path):
    path = write_csv(
        tmp_path,
        [
            "1,2026-06-01T10:00:00Z,buy,ETH,2000,1,trade,confirmed",
            "2,2026-06-01T11:00:00Z,buy,AIOZ,0.05,100,trade,confirmed",
        ],
    )
    cfg = make_config(
        tmp_path,
        manual=(ManualPosition(asset="omni:ETH", side="long", size=9.0, entry=1111.0),),
        omni_trades_csv=str(path),
    )
    positions = fetch_open_positions(cfg, make_router(tmp_path))
    by_asset = {p.asset: p for p in positions}
    assert set(by_asset) == {"omni:ETH", "omni:AIOZ"}
    # the manual declaration overrides the CSV reconstruction for omni:ETH
    assert by_asset["omni:ETH"].size == 9.0 and by_asset["omni:ETH"].entry == 1111.0
    assert by_asset["omni:AIOZ"].size == 100.0
