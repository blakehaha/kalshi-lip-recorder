"""Implied-vol surface for BTC from Deribit option marks.

Deribit lists daily, weekly and monthly BTC options expiring 08:00 UTC. Each
book summary carries `mark_iv` (percent) and the expiry's `underlying_price`.
For each expiry we keep out-of-the-money marks (calls above the forward, puts
below), read the smile as iv against log-moneyness ln(K/F), and interpolate
linearly with flat extrapolation at the wings.

Across expiries we interpolate total variance (iv^2 * t) linearly in time.
Before the first listed expiry we use that expiry's smile; past the last one we
hold its vol flat.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timezone

YEAR_S = 365.0 * 24 * 3600
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def parse_instrument(name: str) -> tuple[float, float, str] | None:
    """'BTC-30OCT26-70000-C' -> (expiry_unix_s, strike, 'C'), or None."""
    parts = name.split("-")
    if len(parts) != 4:
        return None
    _, d, k, cp = parts
    try:
        day, mon, yr = int(d[:-5]), _MONTHS[d[-5:-2]], 2000 + int(d[-2:])
        exp = datetime(yr, mon, day, 8, 0, tzinfo=timezone.utc).timestamp()
        return exp, float(k), cp
    except (KeyError, ValueError):
        return None


@dataclass
class Smile:
    expiry: float                 # unix seconds
    forward: float
    ks: list[float]               # log-moneyness, sorted
    ivs: list[float]              # annualised vol (fraction)

    def vol(self, strike: float) -> float:
        k = math.log(strike / self.forward)
        if k <= self.ks[0]:
            return self.ivs[0]
        if k >= self.ks[-1]:
            return self.ivs[-1]
        i = bisect.bisect_right(self.ks, k)
        k0, k1, v0, v1 = self.ks[i - 1], self.ks[i], self.ivs[i - 1], self.ivs[i]
        return v0 + (v1 - v0) * (k - k0) / (k1 - k0)


class VolSurface:
    def __init__(self, smiles: list[Smile]):
        if not smiles:
            raise ValueError("no smiles")
        self.smiles = sorted(smiles, key=lambda s: s.expiry)

    @classmethod
    def from_deribit(cls, summaries: list[dict], min_points: int = 3) -> "VolSurface":
        by_exp: dict[float, dict] = {}
        for s in summaries:
            p = parse_instrument(s.get("instrument_name", ""))
            iv, fwd = s.get("mark_iv"), s.get("underlying_price")
            if not p or not iv or not fwd or iv <= 0:
                continue
            exp, strike, cp = p
            otm = (cp == "C" and strike >= fwd) or (cp == "P" and strike < fwd)
            if not otm:
                continue
            e = by_exp.setdefault(exp, {"fwd": fwd, "pts": {}})
            e["pts"][math.log(strike / fwd)] = iv / 100.0
        smiles = []
        for exp, e in by_exp.items():
            if len(e["pts"]) < min_points:
                continue
            ks = sorted(e["pts"])
            smiles.append(Smile(exp, e["fwd"], ks, [e["pts"][k] for k in ks]))
        return cls(smiles)

    def forward(self, at: float, now: float, spot: float) -> float:
        """Options-implied forward for unix time `at`: linear in time from spot (now)
        through each listed expiry's forward, held flat past the last one."""
        pts = [(now, spot)] + [(s.expiry, s.forward) for s in self.smiles if s.expiry > now]
        if at <= now:
            return spot
        for (t0, f0), (t1, f1) in zip(pts, pts[1:]):
            if at <= t1:
                return f0 + (f1 - f0) * (at - t0) / (t1 - t0)
        return pts[-1][1]

    def vol(self, strike: float, at: float, now: float) -> float:
        """Annualised vol for `strike` at unix time `at`, as seen from `now`."""
        sm = [s for s in self.smiles if s.expiry > now]
        if not sm:
            raise ValueError("all smiles expired")
        if at <= sm[0].expiry:
            return sm[0].vol(strike)
        if at >= sm[-1].expiry:
            return sm[-1].vol(strike)
        i = bisect.bisect_left([s.expiry for s in sm], at)
        a, b = sm[i - 1], sm[i]
        ta, tb, t = (a.expiry - now) / YEAR_S, (b.expiry - now) / YEAR_S, (at - now) / YEAR_S
        wa, wb = a.vol(strike) ** 2 * ta, b.vol(strike) ** 2 * tb
        w = wa + (wb - wa) * (t - ta) / (tb - ta)
        return math.sqrt(max(w, 0.0) / t)
