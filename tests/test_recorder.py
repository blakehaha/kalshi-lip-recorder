import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pandas as pd
import pytest

from lip.client import KalshiRO
from lip.recorder import COLUMNS, build_row, program_rate_cents_per_hour, select_programs, sweep

PROG = {"id": "p1", "market_ticker": "KXTEST-26SEP30-T90", "incentive_type": "liquidity",
        "incentive_description": "series_lip", "start_date": "2026-09-28T00:00:00Z",
        "end_date": "2026-09-29T00:00:00Z", "period_reward": 2400000,  # centi-cents = $240/day
        "discount_factor_bps": 5000, "target_size_fp": "1000.00"}
BOOK = {"orderbook_fp": {"yes_dollars": [["0.85", "2972"], ["0.83", "600"]],
                         "no_dollars": [["0.12", "1"], ["0.08", "1507"]]}}


def test_program_rate():
    assert program_rate_cents_per_hour(PROG) == pytest.approx(1000)  # $10/hr


def test_select_dedupes_and_ranks():
    a = dict(PROG, id="a", period_reward=100)
    b = dict(PROG, id="b")
    c = dict(PROG, id="c", market_ticker="OTHER", period_reward=500)
    assert [p["id"] for p in select_programs([a, b, c], 5)] == ["b", "c"]


def test_build_row_matches_schema():
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc).timestamp()
    r = build_row(PROG, BOOK, now)
    assert list(r) == COLUMNS
    assert r["yes_best_e4"] == 8500 and r["no_best_e4"] == 1200 and r["implied_spread_e4"] == 300
    assert r["yes_ref_e4"] == 8500 and r["no_ref_e4"] == 800 and r["no_ref_gap_ticks"] == 4
    assert r["qualifies_scoring"] and r["remaining_seconds"] == 12 * 3600
    # quote 1000 each side. YES joins 85c: 1000/3972. NO joins the 1-lot at 12c: that level alone now
    # reaches the target, so the procedure stops there and the quote holds 1000/1001 of the NO side.
    sy, sn = 1000 / 3972, 1000 / 1001
    assert r["share_bps"] == int(min(sy, sn) * 10000) and r["share_avg_bps"] == int((sy + sn) / 2 * 10000)
    assert r["subsidy_avg_cents_per_hour"] == int(1000 * (sy + sn) / 2)
    assert r["capital_cents"] == 1000 * 97


class _Fake(BaseHTTPRequestHandler):
    methods = []

    def do_GET(self):
        type(self).methods.append(self.command)
        path = self.path.split("?")[0]
        if path.endswith("/incentive_programs"):
            body = {"incentive_programs": [PROG], "next_cursor": ""}
        elif path.endswith("/orderbook"):
            body = BOOK
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):
        pass


def test_sweep_writes_parquet(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = KalshiRO(base=f"http://127.0.0.1:{srv.server_port}/trade-api/v2", key_id="", min_interval_s=0)
    path = sweep(c, tmp_path, 10)
    srv.shutdown()
    df = pd.read_parquet(path)
    assert list(df.columns) == COLUMNS and len(df) == 1 and df.ticker[0] == PROG["market_ticker"]
    assert set(_Fake.methods) == {"GET"}
