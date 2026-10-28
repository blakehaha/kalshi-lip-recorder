import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

from pmbot.auth import HDR_SIG, HDR_TS, Signer
from pmbot.backtest import fill_pnl
from pmbot.scoring import book_pays, quote_share, score_side
from pmbot.strategy import Limits, MarketState, Params, Quote, allocate, evaluate, killed


def test_docs_worked_example():
    # discount 0.30: 1,000 at each of 0..3 ticks -> 1000+300+90+27; best holds 70.6%.
    r = score_side([(0.50, 1000), (0.49, 1000), (0.48, 1000), (0.47, 1000)], target_size=4000, discount=0.3)
    assert r.score == pytest.approx(1417)
    assert quote_share([(0.49, 1000), (0.48, 1000), (0.47, 1000)], 0.50, 1000, 4000, 0.3) == pytest.approx(1000 / 1417)
    # asks walk upward from the best ask
    a = score_side([(0.53, 1000), (0.51, 1000)], target_size=2000, discount=0.3, asks=True)
    assert a.best == 0.51 and a.size_adjusted == 0.53 and a.score == pytest.approx(1000 + 90)


def test_walk_stops_at_target():
    # docs: 25,000 at the best with a 20,000 target -> the second level scores zero
    assert quote_share([(0.50, 25000)], 0.49, 1000, 20000, 0.3) == 0
    assert score_side([(0.40, 50)], target_size=100, discount=0.5).qualifies is False


def test_max_spread_docs_examples():
    side = lambda p, asks=False: score_side([(p, 1000)], 1000, 0.3, asks=asks)
    assert book_pays(side(0.49), side(0.51, True), 0.035)
    assert not book_pays(side(0.49), side(0.57, True), 0.035)
    assert book_pays(side(0.47), side(0.54, True), 0.035)          # exactly on the limit pays
    assert not book_pays(side(0.49), score_side([], 1000, 0.3, asks=True), 0.035)
    assert book_pays(side(0.49), score_side([], 1000, 0.3, asks=True), None)


def _m(**kw):
    base = dict(market="M", yes_best=0.45, no_best=0.53, yes_depth=500, no_depth=500, target_size=100,
                reward_per_hour=1.0, share=0.2, hours_left=24, category="culture")
    return MarketState(**{**base, **kw})


def test_evaluate_passes_good_book_and_prices_quote():
    d = evaluate(_m(), Params())
    assert d.quote and d.quote.size == 100 and d.quote.capital == pytest.approx(98)
    assert d.quote.yield_day_per_1k == pytest.approx(0.2 * 24 / 98 * 1000)


@pytest.mark.parametrize("kw,reason", [
    (dict(yes_best=0.03, no_best=0.95), "decided"), (dict(hours_left=1), "ending"),
    (dict(no_best=0.40), "wide"), (dict(yes_depth=50), "thin"), (dict(category="sports"), "category"),
    (dict(share=0.0001), "low_yield"),
])
def test_evaluate_skips(kw, reason):
    p = Params(categories=frozenset({"culture", "tech", "politics"}))
    d = evaluate(_m(**kw), p)
    assert d.quote is None and reason in d.reasons


def test_bait_yield_cap_and_one_sided():
    assert "bait_yield" in evaluate(_m(share=1.0), Params(max_yield=50)).reasons
    assert evaluate(_m(no_best=None), Params()).reasons == ["one_sided"]


def test_allocate_caps_never_shrinks():
    q = lambda name, cap, y: Quote(name, 0.5, 0.5, 100, cap, 0, y)
    picked = allocate([q("a", 150, 90), q("b", 80, 50), q("c", 80, 40), q("d", 80, 30)],
                      Limits(max_capital=200, max_capital_per_market=100, max_markets=5))
    assert [x.market for x in picked] == ["b", "c"]
    assert killed(-50, Limits(max_daily_loss=50)) and not killed(-49.9, Limits(max_daily_loss=50))


def test_fill_pnl():
    assert fill_pnl(0.45, 0.53, 100, 0.45, 0.53) == (False, False, 0.0)
    assert fill_pnl(0.45, 0.53, 100, 0.40, 0.58) == (True, False, pytest.approx(-5.0))
    # both levels vanished: charge the worse side, never credit the pair
    assert fill_pnl(0.45, 0.53, 100, 0.44, None) == (True, True, pytest.approx(-53.0))


def test_signer_signs_ts_method_path_without_query():
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    s = Signer("kid", base64.b64encode(seed + b"\0" * 32).decode())   # 64-byte secret: first 32 are the seed
    h = s.headers("get", "/v1/trading/orders?limit=5", now=1_700_000_000.0)
    assert h[HDR_TS] == "1700000000000"
    k.public_key().verify(base64.b64decode(h[HDR_SIG]), b"1700000000000GET/v1/trading/orders")
    with pytest.raises(ValueError):
        Signer("kid", base64.b64encode(b"short").decode())
