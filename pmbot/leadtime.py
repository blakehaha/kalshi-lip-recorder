"""Did the watcher's sources beat the market? Read the watcher's logs and measure.

    python -m pmbot.leadtime /var/lib/lip/pmbot

For every signal, take each market linked to it and compare the mid (bid+ask)/2
an hour BEFORE the signal, at the signal, and 5 / 15 / 60 / 240 minutes after.
  - Most of the move AFTER the signal: the source led, there was time to trade.
  - Most of the move BEFORE: the market already knew; the source is useless for speed.
  - No move: the signal didn't matter to that market.

Then the reverse: every big move (>= --move cents within an hour) in a watched
market, and whether any signal for that market came in the 12 hours before it.
Big moves with no signal are information the watcher isn't seeing.

Reads the logs line by line; needs about 200 MB on the droplet.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

AFTER_MIN = (5, 15, 60, 240)


def mid(bid, ask) -> float | None:
    if bid is None and ask is None:
        return None
    return ((bid or 0.0) + (ask if ask is not None else 1.0)) / 2


def load_prices(d: Path) -> dict[str, tuple[list[float], list[float]]]:
    """slug -> (sorted timestamps, mids)."""
    series: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for f in sorted(glob.glob(str(d / "prices-*.jsonl"))):
        with open(f) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                m = mid(r.get("bid"), r.get("ask"))
                if m is not None:
                    ts, ms = series[r["slug"]]
                    ts.append(r["ts"])
                    ms.append(m)
    for ts, ms in series.values():   # files are in time order already; be safe anyway
        if any(b < a for a, b in zip(ts, ts[1:])):
            order = sorted(range(len(ts)), key=ts.__getitem__)
            ts[:], ms[:] = [ts[i] for i in order], [ms[i] for i in order]
    return dict(series)


def at(series, t: float, tol_s: float = 150) -> float | None:
    """Mid at the snapshot nearest t (within tol_s), else None."""
    ts, ms = series
    i = bisect.bisect_left(ts, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(ts) and abs(ts[j] - t) <= tol_s and (best is None or abs(ts[j] - t) < abs(ts[best] - t)):
            best = j
    return ms[best] if best is not None else None


def describe(sig: dict) -> str:
    it = sig.get("item", {})
    for k in ("title", "form", "leader", "text", "id"):
        if it.get(k):
            v = f"{it.get('company', '')} {it[k]}".strip() if k == "form" else str(it[k])
            return v[:60]
    return ""


def signal_table(signals: list[dict], prices) -> list[dict]:
    rows = []
    for s in signals:
        t0 = s["ts"]
        best = None
        for b in s.get("books", []):
            ser = prices.get(b["slug"])
            m0 = mid(b.get("bid"), b.get("ask"))
            if ser is None or m0 is None:
                continue
            before = at(ser, t0 - 3600)
            after = {k: at(ser, t0 + 60 * k) for k in AFTER_MIN}
            moves = [abs(v - m0) for v in after.values() if v is not None]
            pre = abs(m0 - before) if before is not None else 0.0
            size = max(moves + [pre])
            if best is None or size > best["size"]:
                best = {"slug": b["slug"], "size": size, "before": before, "m0": m0, "after": after, "pre": pre,
                        "post": max(moves) if moves else 0.0}
        rows.append({"ts": t0, "source": s["source"].split(":")[0], "what": describe(s), **(best or {"size": 0.0})})
    return rows


def big_moves(prices, cents: float, window_min: int = 60) -> list[dict]:
    """Non-overlapping episodes where a market's mid moved >= cents within window_min.
    Snapshots are ~1/min, so the window is window_min points; spans across a gap
    in the log (watcher down) are dropped."""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view
    out, thr, w = [], cents / 100, window_min + 1
    for slug, (ts, ms) in prices.items():
        if len(ms) < 2:
            continue
        t, m = np.asarray(ts), np.asarray(ms)
        pad = np.concatenate([m, np.full(w - 1, np.nan)])
        win = sliding_window_view(pad, w)                       # win[i] = m[i : i + w]
        dev = np.nanmax(np.abs(win - m[:, None]), axis=1)
        i = 0
        for i0 in np.flatnonzero(dev >= thr):
            if i0 < i:
                continue
            k = int(i0 + np.nanargmax(np.abs(win[i0] - m[i0])))
            if t[k] - t[i0] <= window_min * 90:
                out.append({"slug": slug, "t_start": float(t[i0]), "t_end": float(t[k]),
                            "from": float(m[i0]), "to": float(m[k])})
            i = k + 1
    return out


def fmt_t(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")


def c(x: float | None) -> str:
    return "  .  " if x is None else f"{100 * x:5.1f}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Lead time of watcher signals vs market prices.")
    ap.add_argument("dir", nargs="?", default="data/watch")
    ap.add_argument("--move", type=float, default=5.0, help="big-move threshold in cents (default 5)")
    ap.add_argument("--min-signal-move", type=float, default=2.0, help="list signals whose markets moved this much")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    signals = [json.loads(line) for line in open(d / "signals.jsonl")] if (d / "signals.jsonl").exists() else []
    prices = load_prices(d)
    if not prices:
        sys.exit(f"no prices-*.jsonl under {d}")
    t_lo = min(ts[0] for ts, _ in prices.values())
    t_hi = max(ts[-1] for ts, _ in prices.values())
    print(f"## Coverage: {fmt_t(t_lo)} -> {fmt_t(t_hi)} UTC | {len(prices)} markets | {len(signals)} signals\n")

    rows = signal_table(signals, prices)
    by_src = defaultdict(list)
    for r in rows:
        by_src[r["source"]].append(r)
    print("## Signals by source (largest linked-market move, cents)")
    print(f"{'source':8s} {'n':>4s} {'moved>=2c':>9s} {'mostly after':>12s} {'mostly before':>13s}")
    for src, rs in sorted(by_src.items()):
        moved = [r for r in rs if r["size"] >= a.min_signal_move / 100]
        after = [r for r in moved if r.get("post", 0) > r.get("pre", 0)]
        print(f"{src:8s} {len(rs):4d} {len(moved):9d} {len(after):12d} {len(moved) - len(after):13d}")

    print(f"\n## Signals whose markets moved >= {a.min_signal_move:g}c  (mid in cents: -60m | signal | +5 +15 +60 +240m)")
    for r in sorted(rows, key=lambda r: r["ts"]):
        if r["size"] < a.min_signal_move / 100:
            continue
        af = r["after"]
        print(f"{fmt_t(r['ts'])} {r['source']:6s} {r['what'][:38]:38s} {r['slug'][:34]:34s} "
              f"{c(r['before'])} | {c(r['m0'])} | {c(af[5])} {c(af[15])} {c(af[60])} {c(af[240])}")

    moves = big_moves(prices, a.move)
    sig_by_slug = defaultdict(list)
    for s in signals:
        for b in s.get("books", []):
            sig_by_slug[b["slug"]].append(s)
    print(f"\n## Big moves (>= {a.move:g}c within 60 min): {len(moves)}. Signal in the 12h before?")
    explained = 0
    for mv in sorted(moves, key=lambda m: -abs(m["to"] - m["from"]))[:30]:
        prior = [s for s in sig_by_slug.get(mv["slug"], []) if mv["t_start"] - 12 * 3600 <= s["ts"] <= mv["t_end"]]
        p = max(prior, key=lambda s: s["ts"]) if prior else None
        explained += bool(p)
        tag = f"{(mv['t_start'] - p['ts']) / 60:+6.0f}m {p['source'].split(':')[0]}: {describe(p)[:30]}" if p else "NO SIGNAL"
        print(f"{fmt_t(mv['t_start'])}->{fmt_t(mv['t_end'])[6:]} {mv['slug'][:36]:36s} "
              f"{100 * mv['from']:5.1f} -> {100 * mv['to']:5.1f}  {tag}")
    shown = min(len(moves), 30)
    if shown:
        print(f"\n{explained}/{shown} of the largest moves had a signal for that market in the 12h before.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
