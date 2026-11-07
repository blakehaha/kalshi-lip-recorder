import json

from pmbot.leadtime import at, big_moves, load_prices, main, signal_table

T0 = 1_793_000_000.0


def write(tmp_path, prices, signals):
    with open(tmp_path / "prices-2026-11-01.jsonl", "w") as f:
        for r in prices:
            f.write(json.dumps(r) + "\n")
    with open(tmp_path / "signals.jsonl", "w") as f:
        for s in signals:
            f.write(json.dumps(s) + "\n")


def series(slug, mids, start=T0 - 7200):
    return [{"ts": start + 60 * i, "slug": slug, "bid": m - 0.005, "ask": m + 0.005} for i, m in enumerate(mids)]


def test_signal_led_vs_market_led(tmp_path):
    flat_then_jump = [0.30] * 125 + [0.45] * 300        # moves 5 min AFTER the signal at minute 120
    jumped_before = [0.30] * 90 + [0.45] * 335          # moved 30 min BEFORE it
    prices = series("led", flat_then_jump) + series("late", jumped_before)
    sigs = [{"ts": T0, "source": "edgar:Anthropic", "item": {"company": "Anthropic", "form": "S-1"},
             "books": [{"slug": "led", "bid": 0.295, "ask": 0.305}]},
            {"ts": T0, "source": "rss:x", "item": {"title": "Introducing Claude 6"},
             "books": [{"slug": "late", "bid": 0.445, "ask": 0.455}]}]
    write(tmp_path, prices, sigs)
    p = load_prices(tmp_path)
    assert at(p["led"], T0) == 0.30 and at(p["led"], T0 + 15 * 60) == 0.45
    led, late = signal_table(sigs, p)
    assert led["post"] > led["pre"] and led["what"] == "Anthropic S-1"
    assert late["pre"] > late["post"]


def test_big_moves_and_report(tmp_path, capsys):
    prices = series("quiet", [0.50] * 400) + series("jumpy", [0.30] * 200 + [0.40] * 200)
    write(tmp_path, prices, [])
    (mv,) = big_moves(load_prices(tmp_path), cents=5)
    assert mv["slug"] == "jumpy" and round(mv["to"] - mv["from"], 2) == 0.10
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "NO SIGNAL" in out and "0/1 of the largest moves" in out
