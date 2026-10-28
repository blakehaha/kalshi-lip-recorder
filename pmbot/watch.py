"""Watch the sources tech/finance/culture markets resolve on, and log the
Polymarket US price at the moment each source changes. No trading.

    python -m pmbot.watch --config pmbot/watchlist.json --out data/watch
    python -m pmbot.watch --once            # one poll of everything, handy for testing

Each line of <out>/signals.jsonl is one source change plus the BBO of every
market linked to it, fetched right after. <out>/prices-YYYY-MM-DD.jsonl gets a
BBO snapshot of every watched market every --price-interval seconds. Together
they answer the only question that matters before any trading: when a source
changes, how long does the market take to price it?

Sources:
  edgar   SEC full-text search (efts.sec.gov) for filings by named companies.
          IPO markets resolve on S-1 effectiveness, the 424B4, or an exchange
          listing approval.
  hf      New Hugging Face model repos from named orgs.
  rss     New entries in an RSS/Atom feed (openai.com/news/rss.xml).
  arena   The #1 model on LMArena's published leaderboard dataset; fires when
          the leader changes. About a day behind arena.ai, which blocks bots.
  page    Any URL; fires when the page's text changes. Crude, but needs no
          per-site parser.

The first poll of each source only primes its "seen" set: nothing fires for
items that were already there at startup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("pmbot.watch")

GATEWAY = "https://gateway.polymarket.us"
EFTS = "https://efts.sec.gov/LATEST/search-index"
HF = "https://huggingface.co/api/models"
# SEC fair-access policy asks for a contact in the User-Agent: set PMBOT_UA="pmbot you@example.com"
UA = os.environ.get("PMBOT_UA") or "pmbot-watch/0.1 research"
IPO_FORMS = ("S-1", "S-1/A", "F-1", "F-1/A", "424B4", "EFFECT", "DRS", "DRS/A")


def fetch(url: str, params: dict | None = None, timeout: float = 15, raw: bool = False, ua: str = UA,
          binary: bool = False):
    q = {k: v for k, v in (params or {}).items() if v is not None}
    if q:
        url += "?" + urllib.parse.urlencode(q, doseq=True)
    plain = raw or binary
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*" if plain else "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    if binary:
        return body
    return body.decode(errors="replace") if raw else json.loads(body)


class Gateway:
    """Public Polymarket US market data. 25 req/s limit per IP; we stay well under."""

    def __init__(self, min_interval_s: float = 0.1):
        self.min_interval_s, self._last = min_interval_s, 0.0

    def get(self, path: str, **params):
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        return fetch(GATEWAY + path, params)

    def market_slugs(self, event_slug: str) -> list[str]:
        e = self.get(f"/v1/events/slug/{urllib.parse.quote(event_slug)}")
        e = e.get("event", e)
        return [m["slug"] for m in e.get("markets") or [] if not m.get("closed")]

    def bbo(self, slug: str) -> dict:
        d = self.get(f"/v1/markets/{urllib.parse.quote(slug)}/bbo").get("marketData") or {}
        px = lambda k: float(d[k]["value"]) if d.get(k) else None
        return {"slug": slug, "bid": px("bestBid"), "ask": px("bestAsk"), "last": px("lastTradePx"),
                "bid_shares": d.get("bidShares"), "ask_shares": d.get("askShares"),
                "traded": d.get("sharesTraded"), "state": d.get("state")}


class Source:
    name = "source"

    def __init__(self, events: list[str], every_s: float = 60):
        self.events, self.every_s = events, every_s
        self.seen: set[str] | None = None
        self.next_at = 0.0

    def items(self) -> dict[str, dict]:
        """Current items keyed by a stable id."""
        raise NotImplementedError

    def poll(self, now: float) -> list[dict]:
        if now < self.next_at:
            return []
        self.next_at = now + self.every_s
        cur = self.items()
        if self.seen is None:            # first poll: prime, don't fire
            self.seen = set(cur)
            log.info("%s primed with %d items", self.name, len(cur))
            return []
        new = [dict(id=k, **v) for k, v in cur.items() if k not in self.seen]
        self.seen |= set(cur)
        return new


class EdgarSource(Source):
    """Filings BY one company (entityName filters on the filer, so filings that
    merely mention it don't count). EFFECT and 424B4 are both indexed."""

    def __init__(self, company: str, events: list[str], forms=IPO_FORMS, every_s: float = 60,
                 lookback_days: int = 30, ua: str = UA):
        super().__init__(events, every_s)
        self.company, self.forms, self.lookback_days, self.ua = company, forms, lookback_days, ua
        self.name = "edgar:" + company

    def items(self) -> dict[str, dict]:
        today = datetime.now(timezone.utc).date()
        q = {"entityName": self.company, "forms": ",".join(self.forms), "dateRange": "custom",   # startdt is
             "startdt": (today - timedelta(days=self.lookback_days)).isoformat(),               # ignored without
             "enddt": (today + timedelta(days=1)).isoformat()}                                  # an enddt
        try:
            d = fetch(EFTS, q, ua=self.ua)
        except urllib.error.HTTPError as e:     # EFTS throws the odd 500; one retry
            if e.code < 500:
                raise
            time.sleep(1)
            d = fetch(EFTS, q, ua=self.ua)
        out = {}
        for h in d.get("hits", {}).get("hits", []):
            s = h.get("_source", {})
            out[h["_id"]] = {"company": self.company, "form": s.get("form"), "file_date": s.get("file_date"),
                             "filer": (s.get("display_names") or [""])[0], "adsh": s.get("adsh")}
        return out


class HFSource(Source):
    def __init__(self, authors: list[str], events: list[str], every_s: float = 120):
        super().__init__(events, every_s)
        self.authors = authors
        self.name = "hf:" + ",".join(authors)

    def items(self) -> dict[str, dict]:
        out = {}
        for a in self.authors:
            for m in fetch(HF, {"author": a, "sort": "createdAt", "direction": -1, "limit": 20}):
                out[m["id"]] = {"author": a, "created": m.get("createdAt")}
        return out


def page_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


class PageSource(Source):
    def __init__(self, url: str, events: list[str], every_s: float = 60):
        super().__init__(events, every_s)
        self.url = url
        self.name = "page:" + url

    def items(self) -> dict[str, dict]:
        text = page_text(fetch(self.url, raw=True))
        h = hashlib.sha256(text.encode()).hexdigest()[:16]
        return {h: {"url": self.url, "chars": len(text), "head": text[:300]}}


class RSSSource(Source):
    """One signal per new feed entry (RSS <item> or Atom <entry>)."""

    def __init__(self, url: str, events: list[str], every_s: float = 60):
        super().__init__(events, every_s)
        self.url = url
        self.name = "rss:" + url

    def items(self) -> dict[str, dict]:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(fetch(self.url, raw=True))
        out = {}
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag not in ("item", "entry"):
                continue
            kid = {c.tag.rsplit("}", 1)[-1]: c for c in el}
            link = kid.get("link")
            href = (link.get("href") or link.text or "").strip() if link is not None else ""
            key = (kid["guid"].text if "guid" in kid else None) or (kid["id"].text if "id" in kid else None) or href
            text = lambda k: (kid[k].text or "").strip() if k in kid else ""
            out[key.strip()] = {"title": text("title"), "link": href,
                                "published": text("pubDate") or text("published") or text("updated")}
        return out


ARENA_DATASET = "lmarena-ai/leaderboard-dataset"


class ArenaHFSource(Source):
    """#1 on an Arena leaderboard, read from LMArena's published Hugging Face
    dataset (arena.ai itself sits behind a Cloudflare challenge). The dataset is
    refreshed about daily, roughly a day behind the site, so this tracks state,
    not speed. Fires only when the leader changes. `text` is the raw board
    (Style Control Off), which the "#1 AI model" markets settle on."""

    def __init__(self, events: list[str], subset: str = "text", category: str = "overall", every_s: float = 300):
        super().__init__(events, every_s)
        self.subset, self.category = subset, category
        self.name = f"arena:{subset}/{category}"
        self._modified = None
        self._leader = None

    def board(self):
        import io

        import pandas as pd
        url = f"https://huggingface.co/datasets/{ARENA_DATASET}/resolve/main/{self.subset}/latest-00000-of-00001.parquet"
        df = pd.read_parquet(io.BytesIO(fetch(url, binary=True, timeout=60)))
        board = df[df["category"] == self.category]
        if board.empty:
            raise ValueError(f"no category {self.category!r}; have {sorted(df['category'].unique())[:30]}")
        df = board
        # market tie rule: rank first, then the higher score
        return df.sort_values(["rank", "rating"], ascending=[True, False])

    def poll(self, now: float) -> list[dict]:
        if now < self.next_at:
            return []
        self.next_at = now + self.every_s
        modified = fetch(f"https://huggingface.co/api/datasets/{ARENA_DATASET}").get("lastModified")
        if modified == self._modified:
            return []
        b = self.board()
        self._modified = modified
        top = b.head(3)
        leader = f"{top.iloc[0]['organization']}|{top.iloc[0]['model_name']}"
        item = {"id": f"{top.iloc[0]['leaderboard_publish_date']}|{leader}", "leader": leader,
                "published": str(top.iloc[0]["leaderboard_publish_date"]),
                "gap_to_2nd": float(top.iloc[0]["rating"] - top.iloc[1]["rating"]) if len(top) > 1 else None,
                "top3": [f"{r.organization}|{r.model_name}|{r.rating:.1f}" for r in top.itertuples()]}
        prev, self._leader = self._leader, leader
        if prev is None:
            log.info("%s primed: %s", self.name, item["top3"])
            return []
        return [item] if leader != prev else []


def build_sources(cfg: dict) -> list[Source]:
    out: list[Source] = []
    for s in cfg.get("sources", []):
        ev, every = s.get("events", []), s.get("every_s")
        kw = {"every_s": every} if every else {}
        if s["type"] == "edgar":   # one source per company, so one failing name can't stall the rest
            out += [EdgarSource(n, ev, tuple(s.get("forms", IPO_FORMS)), **kw) for n in s["names"]]
        elif s["type"] == "hf":
            out.append(HFSource(s["authors"], ev, **kw))
        elif s["type"] == "page":
            out.append(PageSource(s["url"], ev, **kw))
        elif s["type"] == "rss":
            out.append(RSSSource(s["url"], ev, **kw))
        elif s["type"] == "arena":
            out.append(ArenaHFSource(ev, s.get("subset", "text"), s.get("category", "overall"), **kw))
        else:
            raise ValueError(f"unknown source type {s['type']!r}")
    return out


def append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


class Watcher:
    def __init__(self, gw: Gateway, sources: list[Source], out: Path, price_interval_s: float = 60):
        self.gw, self.sources, self.out, self.price_interval_s = gw, sources, out, price_interval_s
        self.markets: dict[str, list[str]] = {}       # event slug -> market slugs
        self.next_prices = 0.0

    def slugs(self, events: list[str]) -> list[str]:
        out = []
        for e in events:
            if e not in self.markets:
                try:
                    self.markets[e] = self.gw.market_slugs(e)
                except Exception as ex:
                    log.warning("event %s: %s", e, ex)
                    self.markets[e] = []
            out += self.markets[e]
        return out

    def snapshot(self, slugs: list[str]) -> list[dict]:
        rows = []
        for s in slugs:
            try:
                rows.append(self.gw.bbo(s))
            except Exception as ex:
                log.warning("bbo %s: %s", s, ex)
        return rows

    def tick(self, now: float) -> int:
        fired = 0
        for src in self.sources:
            try:
                new = src.poll(now)
            except Exception as ex:
                log.warning("%s: %s", src.name, ex)
                continue
            for item in new:
                t_seen = time.time()
                books = self.snapshot(self.slugs(src.events))
                row = {"ts": t_seen, "source": src.name, "item": item, "books": books,
                       "books_latency_s": round(time.time() - t_seen, 3)}
                append(self.out / "signals.jsonl", row)
                log.warning("SIGNAL %s %s | %s", src.name, json.dumps(item)[:200],
                            " ".join(f"{b['slug']}={b['bid']}/{b['ask']}" for b in books[:6]))
                fired += 1
        if now >= self.next_prices:
            self.next_prices = now + self.price_interval_s
            t = time.time()
            all_slugs = sorted(set(self.slugs(sorted({e for s in self.sources for e in s.events}))))
            day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
            for b in self.snapshot(all_slugs):
                append(self.out / f"prices-{day}.jsonl", {"ts": t, **b})
        return fired


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Log source changes next to Polymarket US prices.")
    ap.add_argument("--config", default=str(Path(__file__).with_name("watchlist.json")))
    ap.add_argument("--out", default="data/watch")
    ap.add_argument("--tick", type=float, default=5, help="seconds between loop passes")
    ap.add_argument("--price-interval", type=float, default=60)
    ap.add_argument("--once", action="store_true", help="prime every source, snapshot prices, exit")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(Path(a.config).read_text())
    w = Watcher(Gateway(), build_sources(cfg), Path(a.out), a.price_interval)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    while True:
        w.tick(time.monotonic())
        if a.once or stop["now"]:
            return 0
        time.sleep(a.tick)


if __name__ == "__main__":
    sys.exit(main())
