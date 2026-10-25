import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from lip.client import KalshiRO
from lip.ledger import (Fill, book_mid, fetch, ledger, load_rewards, main, markouts, mid_at, candle_mids,
                        normalize_fill, report, settlement_value)

T0 = 1_759_000_000  # 2025-09-27 19:06:40 UTC, on a minute boundary


def fp_fill(fid, ticker, book_side, count, yes, fee="0", taker=False, ts=T0):
    return {"fill_id": fid, "trade_id": fid, "order_id": "o", "ticker": ticker, "market_ticker": ticker,
            "outcome_side": "yes", "book_side": book_side, "count_fp": f"{count:.2f}",
            "yes_price_dollars": f"{yes:.4f}", "no_price_dollars": f"{1 - yes:.4f}", "is_taker": taker,
            "fee_cost": fee, "ts": ts}


def test_normalize_fixed_point_shape():
    f = normalize_fill(dict(fp_fill("a", "KXRT-X", "ask", 10, 0.45, fee="0.18", taker=True),
                            created_time="2025-09-27T19:06:40Z"))
    assert f == Fill("a", "KXRT-X", T0, -10.0, 0.45, 0.18, True)


def test_normalize_legacy_cents_and_side_action():
    # buy NO at 60c == sell YES at 40c
    f = normalize_fill({"trade_id": "b", "ticker": "KXRT-X", "side": "no", "action": "buy", "count": 7,
                        "yes_price": 40, "no_price": 60, "is_taker": False, "ts": T0})
    assert (f.fill_id, f.qty, f.yes_price, f.fee) == ("b", -7.0, pytest.approx(0.40), 0.0)
    assert normalize_fill({"trade_id": "c", "ticker": "T", "side": "no", "action": "sell", "count": 1,
                           "yes_price": 40, "ts": T0}).qty == 1.0  # sell NO == buy YES


def test_settlement_value():
    assert settlement_value({"market_result": "yes"}) == 1.0
    assert settlement_value({"market_result": "no"}) == 0.0
    assert settlement_value({"market_result": "scalar", "value": 37}) == pytest.approx(0.37)
    assert settlement_value({"market_result": ""}) is None


def test_book_mid():
    assert book_mid({"orderbook_fp": {"yes_dollars": [["0.40", "5"], ["0.42", "1"]],
                                      "no_dollars": [["0.55", "3"]]}}) == pytest.approx((0.42 + 0.45) / 2)
    assert book_mid({"orderbook_fp": {"yes_dollars": [["0.40", "5"]], "no_dollars": []}}) is None


def test_ledger_round_trip_open_settled_and_unmarked():
    fills = [normalize_fill(fp_fill("a1", "A", "bid", 10, 0.40)),                           # maker buy YES
             normalize_fill(fp_fill("a2", "A", "ask", 10, 0.45, fee="0.18", taker=True)),   # taker buy NO @55
             normalize_fill(fp_fill("b1", "B", "bid", 5, 0.30)),
             normalize_fill(fp_fill("c1", "C", "bid", 10, 0.90)),
             normalize_fill(fp_fill("d1", "D", "bid", 1, 0.50))]
    rows = {r["ticker"]: r for r in ledger(fills, {"C": 1.0}, {"B": 0.20}, {"A": 1.25, "E": 0.40}, {})}
    a = rows["A"]
    assert a["status"] == "flat" and a["position"] == 0 and a["taker_fills"] == 1
    assert a["trading_pnl"] == pytest.approx(-4.00 + 4.50 - 0.18) and a["net_pnl"] == pytest.approx(0.32 + 1.25)
    assert a["fees"] == a["taker_fees"] == pytest.approx(0.18)
    assert rows["B"]["status"] == "open" and rows["B"]["trading_pnl"] == pytest.approx(-1.50 + 5 * 0.20)
    assert rows["C"]["status"] == "settled" and rows["C"]["trading_pnl"] == pytest.approx(1.0)
    assert rows["D"]["trading_pnl"] is None and rows["D"]["net_pnl"] is None
    assert rows["E"]["fills"] == 0 and rows["E"]["net_pnl"] == pytest.approx(0.40)  # rewards, no fills
    md = report(list(rows.values()), 0.10, 5, "2025-09-27")
    assert "Left out of the totals (open, no mark): D" in md
    # net = trading (0.32 - 0.50 + 1.00) + rewards (1.25 + 0.40 + 0.10)
    assert "| **Net** | **$2.57** |" in md and "| Taker fills (crossed the spread) | 1 |" in md


def test_settlement_fee_counts():
    fills = [normalize_fill(fp_fill("c1", "C", "bid", 10, 0.90))]
    r = ledger(fills, {"C": 1.0}, {}, {}, {}, {"C": 0.05})[0]
    assert r["fees"] == pytest.approx(0.05) and r["trading_pnl"] == pytest.approx(0.95)


def candle(end, bid, ask):
    return {"end_period_ts": end, "yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask}}


def test_mid_at_uses_last_candle_and_rejects_stale():
    s = candle_mids([candle(T0 + 60, "0.40", "0.44"), candle(T0, "0.41", "0.43"),
                     {"end_period_ts": T0 + 120, "yes_bid": {"close": "0.30"}, "yes_ask": {"close": "0.34"}},
                     {"end_period_ts": T0 + 180, "yes_bid": {"close_dollars": None}, "yes_ask": {}},
                     candle(T0 + 240, "0.0000", "0.05")])
    assert s[0] == [T0, T0 + 60, T0 + 120]  # sorted; empty and bidless candles dropped; historical keys parse
    assert mid_at(s, T0 + 59) == pytest.approx(0.42) and mid_at(s, T0 + 60) == pytest.approx(0.42)
    assert mid_at(s, T0 + 130) == pytest.approx(0.32)
    assert mid_at(s, T0 - 1) is None and mid_at(s, T0 + 120 + 15 * 60 + 1) is None


def test_edge_and_adverse_split():
    # bid filled at 40 with the mid at 42: +2c edge on 10 = $0.20. Five minutes later the mid is 35: -$0.70.
    candles = {"A": [candle(T0, "0.41", "0.43"), candle(T0 + 300, "0.34", "0.36")]}
    fills = [normalize_fill(fp_fill("a1", "A", "bid", 10, 0.40, ts=T0 + 30)),
             normalize_fill(fp_fill("z", "Z", "bid", 1, 0.50, ts=T0))]  # no candles: left unmarked
    mk = markouts(fills, candles, 300)
    assert mk == {"a1": (pytest.approx(0.42), pytest.approx(0.35))}
    r = ledger(fills[:1], {}, {"A": 0.35}, {}, mk)[0]
    assert r["edge"] == pytest.approx(0.20) and r["adverse"] == pytest.approx(-0.70) and r["marked_fills"] == 1


def test_load_rewards(tmp_path):
    p = tmp_path / "rewards.csv"
    p.write_text("date,ticker,amount_dollars,note\n2025-09-28,A,1.00,\n2025-09-29,A,0.25,x\n"
                 "2025-09-29,,0.10,bulk\n2025-09-30,B,,pending\n")
    r = load_rewards(p)
    assert r["A"] == pytest.approx(1.25) and r[""] == pytest.approx(0.10) and "B" not in r
    assert load_rewards(None) == {}


class _Fake(BaseHTTPRequestHandler):
    methods, paths = [], []

    def do_GET(self):
        type(self).methods.append(self.command)
        path = self.path.split("?")[0].removeprefix("/trade-api/v2")
        type(self).paths.append(path)
        page2 = "cursor=p2" in self.path
        if not self.headers.get("KALSHI-ACCESS-SIGNATURE"):
            return self._send(401, {})
        if path == "/portfolio/fills":  # two pages, to exercise the cursor
            body = ({"fills": [fp_fill("a2", "KXRT-A", "ask", 10, 0.45, fee="0.18", taker=True, ts=T0 + 90)],
                     "cursor": ""} if page2 else
                    {"fills": [fp_fill("a1", "KXRT-A", "bid", 10, 0.40, ts=T0 + 30)], "cursor": "p2"})
        elif path == "/portfolio/settlements":
            body = {"settlements": [], "cursor": ""}
        elif path == "/portfolio/positions":
            body = {"market_positions": [{"ticker": "KXRT-A", "fees_paid_dollars": "0.1800",
                                          "realized_pnl_dollars": "0.3200"}], "event_positions": [], "cursor": ""}
        elif path == "/markets/KXRT-A/orderbook":
            body = {"orderbook_fp": {"yes_dollars": [["0.39", "50"]], "no_dollars": [["0.57", "50"]]}}
        elif path == "/historical/markets/KXRT-A/candlesticks":
            body = {"ticker": "KXRT-A", "candlesticks": [candle(T0, "0.41", "0.43"), candle(T0 + 300, "0.40", "0.42"),
                                                         candle(T0 + 360, "0.38", "0.40")]}
        elif path == "/series/KXRT/markets/KXRT-A/candlesticks":
            body = {"ticker": "KXRT-A", "candlesticks": []}  # settled before the cutoff: only /historical has it
        else:  # /historical/fills, /historical/positions
            return self._send(404, {})
        self._send(200, body)

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def signed(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem = tmp_path / "k.pem"
    pem.write_bytes(rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for k, v in {"KALSHI_API_BASE": f"http://127.0.0.1:{srv.server_port}/trade-api/v2",
                 "KALSHI_KEY_ID": "kid", "KALSHI_PRIVATE_KEY_PATH": str(pem)}.items():
        monkeypatch.setenv(k, v)
    _Fake.methods.clear(), _Fake.paths.clear()
    yield
    srv.shutdown()


def test_end_to_end_fetch_and_report(signed, tmp_path, capsys):
    rewards = tmp_path / "rewards.csv"
    rewards.write_text("date,ticker,amount_dollars\n2025-09-28,KXRT-A,1.50\n")
    cache, out = tmp_path / "cache", tmp_path / "ledger.md"
    assert main(["--cache", str(cache), "--rewards", str(rewards), "--out", str(out)]) == 0
    md = out.read_text()
    assert md == capsys.readouterr().out
    # flat after buying YES @40 and buying NO @55 (taker, 18c fee): 0.50 - 0.18 = 0.32, plus $1.50 rewards
    assert "| **Net** | **$1.82** |" in md and "| Taker fills (crossed the spread) | 1 |" in md
    # a1 (bid @40 at +30s, mid 42 before, 41 at +330s):     edge +0.20, adverse -0.10
    # a2 (sell YES @45 at +90s, mid 42 before, 39 at +390s): edge +0.30, adverse +0.30
    assert "| Edge captured at fill | $0.50 |" in md
    assert "| Mid move 5 min after fill (adverse; negative = picked off) | $0.20 |" in md
    assert "| $0.18 / $0.32 |" in md  # Kalshi's numbers agree with ours
    assert set(_Fake.methods) == {"GET"}
    assert "/historical/markets/KXRT-A/candlesticks" in _Fake.paths
    assert (cache / "fills_marked.csv").read_text().count("\n") == 3
    # offline re-run reproduces the report from the cache alone
    n = len(_Fake.paths)
    main(["--cache", str(cache), "--rewards", str(rewards), "--offline"])
    assert len(_Fake.paths) == n and "| **Net** | **$1.82** |" in capsys.readouterr().out


def test_fetch_requires_signing(tmp_path, monkeypatch):
    monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
    with pytest.raises(SystemExit, match="KALSHI_KEY_ID"):
        fetch(KalshiRO(key_id=""), tmp_path, None, 300, None)
