import inspect

import pytest

from lip import client as client_mod
from lip.client import KalshiRO, parse_orderbook
from lip.scoring import quote_share, score_side, snapshot_share


def test_reference_price_and_discount():
    # 300 at 50c reaches target/5 = 200 -> Reference = 0.50. 1000 at 48c is 2 ticks below -> 0.5^2.
    r = score_side([(0.50, 300), (0.48, 1000)], target_size=1000, discount=0.5)
    assert r.qualifies and r.reference == 0.50 and r.best == 0.50
    assert r.score_total == pytest.approx(300 + 1000 * 0.25)


def test_reference_can_sit_below_best():
    # A thin best level doesn't set the reference; everything at/above it scores in full.
    r = score_side([(0.50, 100), (0.48, 1000)], target_size=1000, discount=0.5)
    assert r.reference == 0.48 and r.ref_gap_ticks == 2
    assert r.score_total == pytest.approx(1100)


def test_side_below_target_scores_nothing():
    r = score_side([(0.40, 500)], target_size=1000, discount=0.5)
    assert not r.qualifies and r.score_total == 0
    assert quote_share([(0.40, 400)], 0.40, 100, 1000, 0.5) == 0  # 500 < target, still nothing


def test_levels_past_target_do_not_qualify():
    # 50c alone fills the target, so the procedure stops; a bid at 49c earns zero.
    book = [(0.50, 1000)]
    assert quote_share(book, 0.49, 10, 1000, 0.5) == 0
    assert quote_share(book, 0.50, 10, 1000, 0.5) == pytest.approx(10 / 1010)


def test_whole_levels_no_time_priority():
    # Joining the back of a deep level scores the same as being first in it.
    assert quote_share([(0.30, 5000)], 0.30, 100, 1000, 0.5) == pytest.approx(100 / 5100)


def test_real_book_improving_price_buys_no_extra_score():
    """Kalshi 'Above 90' book, 2026-09-28: YES bids 85c x2972, 83c x600; YES asks 88c x1, 92c x1507.
    NO bids are the mirror of YES asks: 12c x1, 8c x1507."""
    yes = [(0.85, 2972), (0.83, 600)]
    no = [(0.12, 1), (0.08, 1507)]
    join = quote_share(yes, 0.85, 10, 1000, 0.5)
    improve = quote_share(yes, 0.86, 10, 1000, 0.5)
    assert join == pytest.approx(improve) == pytest.approx(10 / 2982)
    n = quote_share(no, 0.12, 10, 1000, 0.5)
    assert n == pytest.approx(10 / 1518)  # ref walks down to 8c; the 12c bid is above it -> full credit
    assert snapshot_share(join, n) == pytest.approx((10 / 2982 + 10 / 1518) / 2)


def test_parse_orderbook_shapes():
    fp = {"orderbook_fp": {"yes_dollars": [["0.1500", "100.00"]], "no_dollars": [["0.8500", "75.00"]]}}
    assert parse_orderbook(fp) == ([(0.15, 100.0)], [(0.85, 75.0)])
    legacy = {"orderbook": {"yes": [[15, 100]], "no": None}}
    assert parse_orderbook(legacy) == ([(0.15, 100.0)], [])


def test_client_is_get_only():
    src = inspect.getsource(client_mod)
    for bad in ('"POST"', '"PUT"', '"DELETE"', "def place", "def cancel", "def amend", "/portfolio/orders"):
        assert bad not in src
    with pytest.raises(PermissionError):
        object.__new__(KalshiRO)._request("POST", "/portfolio/orders")


def test_big_quote_improving_does_pay():
    # A target-size quote one tick in front becomes the whole qualifying set.
    yes = [(0.85, 2972), (0.83, 600)]
    assert quote_share(yes, 0.85, 1000, 1000, 0.5) == pytest.approx(1000 / 3972)
    assert quote_share(yes, 0.86, 1000, 1000, 0.5) == pytest.approx(1.0)
