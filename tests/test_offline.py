from datetime import UTC, datetime
import json

from src.offline import canonical_url, fetch_hacker_news, filter_and_select, parse_feed, run, score_item


def test_canonical_url_strips_tracking_and_fragment():
    assert canonical_url("HTTPS://Example.com/a/?utm_source=x&ok=1#part") == "https://example.com/a?ok=1"


def test_parse_feed_and_score_category():
    feed = b"""<rss><channel><item><title>Bootstrapped SaaS reaches $10k MRR</title><link>https://example.com/a?utm_source=rss</link><pubDate>Wed, 15 Oct 2026 01:00:00 GMT</pubDate><description>Revenue and customers</description></item></channel></rss>"""
    item = parse_feed(feed, {"name": "Indie Hackers", "url": "https://feed"})[0]
    scored = score_item(item, datetime(2026, 10, 15, 2, tzinfo=UTC))
    assert scored["category"] == "Indie Business"
    assert scored["score"] >= 40


def test_dedup_history_title_and_url():
    now = datetime(2026, 10, 15, tzinfo=UTC)
    rows = [
        {"title": "Google Search indexing experiment", "url": "https://example.com/a", "source": "Google Search Central", "published_at": now.isoformat(), "excerpt": "ranking traffic"},
        {"title": "Google Search indexing experiment", "url": "https://example.com/b", "source": "Google Search Central", "published_at": now.isoformat(), "excerpt": "ranking traffic"},
    ]
    selected, _ = filter_and_select(rows, {"https://already.example/a"}, now)
    assert len(selected) == 1
    selected, _ = filter_and_select(rows, {"https://example.com/a"}, now, {"googlesearchindexingexperiment"})
    assert not selected


def test_hacker_news_uses_public_items_and_skips_failures():
    def fake_fetch(url):
        if url.endswith("topstories.json"):
            return b"[1, 2]"
        if url.endswith("/1.json"):
            return b'{"type":"story","title":"Open source coding agent","url":"https://example.com/a","time":1792026000,"score":240}'
        raise OSError("one item failed")
    rows = fetch_hacker_news(fake_fetch)
    assert len(rows) == 1
    assert rows[0]["popularity"] == 240


def test_run_handles_source_failure_and_writes_all_formats(tmp_path):
    now = datetime(2026, 10, 15, 2, tzinfo=UTC)
    feed = b"""<rss><channel><item><title>Open source MCP coding tool</title><link>https://example.com/tool</link><pubDate>Wed, 15 Oct 2026 01:00:00 GMT</pubDate><description>Free developer automation</description></item></channel></rss>"""
    def fake_fetch(url):
        if url == "https://feed": return feed
        raise OSError("network down")
    report = run(tmp_path / "site", tmp_path / "state.json", fetcher=fake_fetch, sources=[{"name": "GitHub Blog", "url": "https://feed"}, {"name": "Bad", "url": "https://bad"}], now=now)
    assert report["source_status"]["Bad"].startswith("failed:")
    assert (tmp_path / "site" / "index.html").exists()
    assert (tmp_path / "site" / "daily.md").exists()
    assert json.loads((tmp_path / "site" / "daily.json").read_text())["date"] == "2026-10-15"
