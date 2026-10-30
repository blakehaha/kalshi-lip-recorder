import json

from pmbot import watch
from pmbot.watch import EdgarSource, PageSource, Source, Watcher, page_text


class Fixed(Source):
    name = "fixed"

    def __init__(self, batches, events=("ev",)):
        super().__init__(list(events), every_s=0)
        self.batches = list(batches)

    def items(self):
        return self.batches.pop(0)


def test_first_poll_primes_then_only_new_items_fire():
    s = Fixed([{"a": {}}, {"a": {}, "b": {"x": 1}}, {"a": {}, "b": {"x": 1}}])
    assert s.poll(0) == []
    assert s.poll(1) == [{"id": "b", "x": 1}]
    assert s.poll(2) == []


def test_poll_respects_interval():
    s = Fixed([{"a": {}}, {"b": {}}])
    s.every_s = 60
    s.poll(0)
    assert s.poll(30) == [] and len(s.batches) == 1   # not due, nothing fetched


def test_page_text_strips_scripts_and_tags():
    assert page_text("<html><script>var x=1</script><b>Rank</b>\n 1  <style>a{}</style>Gemini</html>") == "Rank 1 Gemini"


def test_page_source_changes_fire(monkeypatch):
    pages = iter(["<p>#1 Gemini</p>", "<p>#1 Gemini</p>", "<p>#1 Claude</p>"])
    monkeypatch.setattr(watch, "fetch", lambda *a, **k: next(pages))
    s = PageSource("https://arena.ai/x", ["ev"], every_s=0)
    assert s.poll(0) == [] and s.poll(1) == []
    (sig,) = s.poll(2)
    assert sig["head"] == "#1 Claude"


def test_edgar_source_parses_hits_and_sends_entity_filter(monkeypatch):
    seen = {}

    def fake(url, params=None, **k):
        seen.update(params)
        return {"hits": {"hits": [{"_id": "0002130357-26-000014:f.htm", "_source": {
            "form": "DRS/A", "file_date": "2026-10-09", "display_names": ["Anthropic, PBC  (CIK 0002130357)"],
            "adsh": "0002130357-26-000014"}}]}}

    monkeypatch.setattr(watch, "fetch", fake)
    items = EdgarSource("Anthropic", ["anthropic"]).items()
    assert seen["entityName"] == "Anthropic" and "enddt" in seen and "EFFECT" in seen["forms"]
    assert items["0002130357-26-000014:f.htm"]["form"] == "DRS/A"


class FakeGateway:
    def market_slugs(self, event):
        return [f"{event}-yes"]

    def bbo(self, slug):
        return {"slug": slug, "bid": 0.86, "ask": 0.87}


def test_tick_logs_signal_with_books_and_prices(tmp_path):
    w = Watcher(FakeGateway(), [Fixed([{"a": {}}, {"a": {}, "b": {"form": "EFFECT"}}], ["anthropic"])], tmp_path)
    assert w.tick(0) == 0
    assert w.tick(1) == 1
    row = json.loads((tmp_path / "signals.jsonl").read_text())
    assert row["item"] == {"id": "b", "form": "EFFECT"}
    assert row["books"] == [{"slug": "anthropic-yes", "bid": 0.86, "ask": 0.87}]
    (prices,) = tmp_path.glob("prices-*.jsonl")
    assert json.loads(prices.read_text().splitlines()[0])["slug"] == "anthropic-yes"


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>OpenAI News</title>
<item><title>Introducing GPT Astra 6.1</title><link>https://openai.com/index/astra-6-1/</link>
<guid isPermaLink="false">astra-6-1</guid><pubDate>Thu, 29 Oct 2026 17:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:x,1</id><title>Claude 6</title>
<link href="https://www.anthropic.com/news/claude-6"/><updated>2026-10-29T17:00:00Z</updated></entry></feed>"""


def test_rss_and_atom_items(monkeypatch):
    from pmbot.watch import RSSSource
    monkeypatch.setattr(watch, "fetch", lambda *a, **k: RSS)
    assert RSSSource("u", []).items() == {"astra-6-1": {
        "title": "Introducing GPT Astra 6.1", "link": "https://openai.com/index/astra-6-1/",
        "published": "Thu, 29 Oct 2026 17:00:00 GMT"}}
    monkeypatch.setattr(watch, "fetch", lambda *a, **k: ATOM)
    assert RSSSource("u", []).items()["tag:x,1"]["link"] == "https://www.anthropic.com/news/claude-6"


def test_arena_fires_only_on_leader_change(monkeypatch):
    import pandas as pd
    from pmbot.watch import ArenaHFSource

    boards = iter([
        [("Google", "gemini-3.5-pro", 1, 1510.0), ("Anthropic", "claude-opus-5-5", 1, 1508.0)],
        [("Google", "gemini-3.5-pro", 1, 1511.0), ("Anthropic", "claude-opus-5-5", 2, 1500.0)],
        [("Anthropic", "claude-6", 1, 1530.0), ("Google", "gemini-3.5-pro", 2, 1511.0)],
    ])
    modified = iter(["d1", "d1", "d2", "d3"])
    monkeypatch.setattr(watch, "fetch", lambda *a, **k: {"lastModified": next(modified)})
    s = ArenaHFSource(["company1-2026-12-31"], every_s=0)
    s.board = lambda: pd.DataFrame(next(boards), columns=["organization", "model_name", "rank", "rating"]).assign(
        leaderboard_publish_date="2026-10-28").sort_values(["rank", "rating"], ascending=[True, False])
    assert s.poll(0) == []              # primes: Google leads (rank tie broken by score)
    assert s.poll(1) == []              # dataset unchanged, board not even fetched
    assert s.poll(2) == []              # same leader
    (sig,) = s.poll(3)
    assert sig["leader"] == "Anthropic|claude-6" and sig["gap_to_2nd"] == 19.0
