"""No-key, rules-only daily briefing for Horizon.

This module deliberately lives beside, rather than inside, the AI pipeline.  It
reuses Horizon's source idea and publishing convention while guaranteeing that
the offline command neither reads nor calls an AI provider.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import html
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


CATEGORIES = {
    "Indie Business": ["indie", "bootstrap", "bootstrapp", "micro saas", "saas", "side project", "solopreneur", "revenue", "mrr", "profit", "customers", "conversion", "launch", "acquisition", "sell a site"],
    "SEO": ["seo", "google search", "search console", "indexing", "ranking", "organic traffic", "algorithm update", "site migration", "serp", "backlink"],
    "AI Tools": ["ai", "llm", "codex", "claude", "openai", "deepseek", "agent", "mcp", "machine learning", "github", "automation", "cloudflare", "developer tool"],
    "Technology": ["semiconductor", "chip", "gpu", "robot", "manufacturing", "space", "energy", "battery", "power grid", "materials", "industrial", "inference", "datacenter"],
    "Discoveries": ["open source", "github", "free tool", "productivity", "design", "resource", "tutorial", "database", "self hosted"],
}

SOURCE_WEIGHTS = {"Hacker News": 13, "Simon Willison": 14, "GitHub Blog": 12, "GitHub Trending": 10, "Lobsters": 10, "The Pragmatic Engineer": 10, "MIT Technology Review": 9, "IEEE Spectrum": 9, "TechCrunch": 6}
DEFAULT_SOURCES = [
    {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/"},
    {"name": "GitHub Blog", "url": "https://github.blog/feed/"},
    {"name": "GitHub Trending", "url": "https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml"},
    {"name": "The Pragmatic Engineer", "url": "https://newsletter.pragmaticengineer.com/feed"},
    {"name": "Lobsters", "url": "https://lobste.rs/rss"},
    {"name": "MIT Technology Review", "url": "https://www.technologyreview.com/feed/"},
    {"name": "IEEE Spectrum", "url": "https://spectrum.ieee.org/feeds/feed.rss"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
]


def canonical_url(url: str) -> str:
    """Remove fragments and tracking parameters without changing article identity."""
    parts = urlsplit(url.strip())
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in {"ref", "source", "fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(kept), ""))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value or ""))).strip()


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return result.replace(tzinfo=result.tzinfo or UTC).astimezone(UTC)


def parse_feed(payload: bytes, source: dict[str, str]) -> list[dict[str, Any]]:
    """Parse RSS or Atom using the standard library, retaining only supplied excerpts."""
    root = ET.fromstring(payload)
    rows = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    items = []
    for row in rows:
        get = lambda names: next((clean_text((node.text or "")) for name in names for node in row.findall(name) if node is not None and (node.text or "").strip()), "")
        atom = "{http://www.w3.org/2005/Atom}"
        title = get(["title", atom + "title"])
        link = get(["link"])
        if not link:
            link_node = row.find(atom + "link")
            link = (link_node.get("href", "") if link_node is not None else "")
        if not title or not link.startswith(("https://", "http://")):
            continue
        published = parse_date(get(["pubDate", "published", "updated", atom + "published", atom + "updated"]))
        excerpt = get(["description", "summary", "content", atom + "summary", atom + "content"])
        items.append({"title": title, "url": canonical_url(link), "source": source["name"], "published_at": published.isoformat() if published else None, "excerpt": excerpt[:500]})
    return items


def fetch_url(url: str, timeout: int = 12) -> bytes:
    request = Request(url, headers={"User-Agent": "Horizon-Offline-Radar/1.0 (+https://github.com/daligao/Horizon)"})
    with urlopen(request, timeout=timeout) as response:  # nosec B310: public, configured feeds only
        return response.read()


def fetch_hacker_news(fetcher: Callable[[str], bytes]) -> list[dict[str, Any]]:
    ids = json.loads(fetcher("https://hacker-news.firebaseio.com/v0/topstories.json").decode())[:40]
    output = []
    def get_story(story_id: int) -> dict[str, Any]:
        return json.loads(fetcher(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json").decode())
    # HN's public API is independent per item; bounded concurrency avoids a slow
    # single story turning a daily job into dozens of sequential timeouts.
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(get_story, story_id) for story_id in ids[:30]]
        for future in as_completed(futures):
            try:
                data = future.result()
            except Exception:
                continue
            if data.get("type") == "story" and data.get("url") and data.get("title"):
                output.append({"title": data["title"], "url": canonical_url(data["url"]), "source": "Hacker News", "published_at": datetime.fromtimestamp(data.get("time", 0), UTC).isoformat(), "excerpt": "", "popularity": int(data.get("score", 0))})
    return output


def score_item(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    haystack = f"{item['title']} {item.get('excerpt', '')}".lower()
    matches = {category: [word for word in words if word in haystack] for category, words in CATEGORIES.items()}
    category, words = max(matches.items(), key=lambda pair: len(pair[1]))
    score, reasons = min(42, len(words) * 11), []
    if words:
        reasons.append(f"Matches {category.lower()} terms: {', '.join(words[:3])}")
    else:
        score -= 10
    source_weight = SOURCE_WEIGHTS.get(item["source"], 4)
    score += source_weight
    if source_weight >= 10:
        reasons.append("Trusted specialist source")
    published = parse_date(item.get("published_at"))
    if published:
        age = now - published
        if age <= timedelta(hours=30):
            score += 18; reasons.append("Published in the last 30 hours")
        elif age <= timedelta(days=7):
            score += 8; reasons.append("Published this week")
    if re.search(r"\b(revenue|mrr|arr|profit|customers|traffic|conversion|users|%|\$[0-9])", haystack):
        score += 15; reasons.append("Includes concrete operating or outcome data")
    popularity = int(item.get("popularity", 0))
    if popularity:
        score += min(15, popularity // 20)
        reasons.append(f"Hacker News score: {popularity}")
    item.update(category=category, score=max(0, min(100, score)), reasons=reasons or ["Low-confidence rules-only match"])
    return item


def filter_and_select(items: list[dict[str, Any]], seen: set[str], now: datetime, seen_titles: set[str] | None = None) -> tuple[list[dict[str, Any]], int]:
    unique, titles = [], set()
    for item in items:
        title_key = re.sub(r"[^a-z0-9]+", "", item["title"].lower())
        if item["url"] in seen or title_key in (seen_titles or set()) or item["url"] in {x["url"] for x in unique} or title_key in titles or len(item["title"]) < 12:
            continue
        published = parse_date(item.get("published_at"))
        if published and published < now - timedelta(days=8):
            continue
        titles.add(title_key); unique.append(score_item(item, now))
    filtered = [item for item in unique if item["score"] >= 20]
    selected, source_counts = [], {}
    for item in sorted(filtered, key=lambda row: row["score"], reverse=True):
        if len(selected) == 20 or source_counts.get(item["source"], 0) >= 3:
            continue
        source_counts[item["source"]] = source_counts.get(item["source"], 0) + 1
        selected.append(item)
    return selected, len(filtered)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Horizon Daily Radar — {report['date']}", "", f"Collected: {report['total_collected']} · After filtering: {report['after_filtering']} · Selected: {len(report['items'])}", "", "Rules-only ranking; no AI summary or external AI service was used.", ""]
    for category in CATEGORIES:
        rows = [row for row in report["items"] if row["category"] == category]
        if rows:
            lines += [f"## {category}", ""]
            for row in rows:
                lines += [f"### [{row['title']}]({row['url']}) — {row['score']}/100", f"Source: {row['source']} · Published: {row.get('published_at') or 'Unknown'}", f"Reason: {'; '.join(row['reasons'])}", ""]
    return "\n".join(lines)


def render_html(report: dict[str, Any]) -> str:
    cards = "".join(f'''<article class="card" data-category="{html.escape(row['category'])}"><div class="meta">{html.escape(row['category'])} · {html.escape(row['source'])} · {html.escape((row.get('published_at') or 'Unknown')[:10])}</div><h2><a href="{html.escape(row['url'], quote=True)}" rel="noopener noreferrer" target="_blank">{html.escape(row['title'])}</a></h2><p class="score">{row['score']}<small>/100</small></p><p>{html.escape(row.get('excerpt') or '')}</p><p class="reason">{' · '.join(html.escape(reason) for reason in row['reasons'])}</p></article>''' for row in report["items"])
    buttons = "".join(f'<button data-filter="{name}">{name}</button>' for name in ["All", *CATEGORIES])
    statuses = ", ".join(f"{html.escape(name)}: {html.escape(status)}" for name, status in report["source_status"].items())
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Horizon Daily Radar</title><style>body{{font:16px system-ui,sans-serif;max-width:1100px;margin:auto;padding:24px;background:#f7f8fa;color:#18202b}}header{{border-bottom:1px solid #d9dee7}}h1{{margin-bottom:4px}}.summary,.meta,.reason{{color:#5c6878}}.filters{{display:flex;gap:8px;flex-wrap:wrap;margin:24px 0}}button{{border:1px solid #c8d0db;background:white;border-radius:20px;padding:7px 12px;cursor:pointer}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:16px}}.card{{background:#fff;padding:18px;border-radius:12px;box-shadow:0 1px 4px #0012}}h2{{font-size:1.05rem}}a{{color:#1459c7;text-decoration:none}}.score{{font-size:1.5rem;font-weight:bold;color:#087950}}small{{font-size:.8rem}}.reason{{font-size:.85rem}}footer{{margin:28px 0;color:#667}}@media(max-width:600px){{body{{padding:14px}}}}</style><header><h1>Horizon Daily Radar</h1><p class="summary">{report['date']} · {report['total_collected']} collected · {report['after_filtering']} filtered · {len(report['items'])} selected</p><p class="summary">Rules-only recommendation score; no AI semantic analysis.</p></header><nav class="filters">{buttons}</nav><main class="grid">{cards or '<p>No items met the rules today.</p>'}</main><footer>Source status: {statuses}<br><a href="archive/index.html">Archive</a> · <a href="daily.md">Markdown</a> · <a href="daily.json">JSON</a></footer><script>document.querySelectorAll('button').forEach(b=>b.onclick=()=>document.querySelectorAll('.card').forEach(c=>c.hidden=b.dataset.filter!=='All'&&c.dataset.category!==b.dataset.filter));</script></html>'''


def write_report(site_dir: Path, report: dict[str, Any]) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    day = report["date"]
    archive = site_dir / "archive" / day
    archive.mkdir(parents=True, exist_ok=True)
    for directory in (site_dir, archive):
        (directory / "daily.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (directory / "daily.md").write_text(render_markdown(report), encoding="utf-8")
        (directory / "index.html").write_text(render_html(report), encoding="utf-8")
    days = sorted([path.name for path in (site_dir / "archive").iterdir() if path.is_dir()], reverse=True)[:90]
    (site_dir / "archive" / "index.html").write_text("<!doctype html><title>Horizon archive</title><h1>Daily archive</h1><ul>" + "".join(f'<li><a href="{day}/">{day}</a></li>' for day in days) + "</ul>", encoding="utf-8")


def run(site_dir: Path, state_path: Path, hours: int = 36, fetcher: Callable[[str], bytes] = fetch_url, sources: list[dict[str, str]] | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    state = json.loads(state_path.read_text()) if state_path.exists() else {"seen": [], "seen_titles": []}
    source_status, raw = {}, []
    for source in sources or DEFAULT_SOURCES:
        try:
            rows = parse_feed(fetcher(source["url"]), source)
            raw.extend(rows); source_status[source["name"]] = f"ok ({len(rows)})"
        except Exception as error:  # source failures must never abort a daily report
            source_status[source["name"]] = f"failed: {type(error).__name__}"
    try:
        hn = fetch_hacker_news(fetcher); raw.extend(hn); source_status["Hacker News"] = f"ok ({len(hn)})"
    except Exception as error:
        source_status["Hacker News"] = f"failed: {type(error).__name__}"
    recent = [row for row in raw if not row.get("published_at") or parse_date(row["published_at"]) >= now - timedelta(hours=hours)]
    selected, after_filtering = filter_and_select(recent, set(state.get("seen", [])), now, set(state.get("seen_titles", [])))
    report = {"date": now.date().isoformat(), "generated_at": now.isoformat(), "total_collected": len(raw), "after_filtering": after_filtering, "source_status": source_status, "items": selected}
    write_report(site_dir, report)
    combined = [row["url"] for row in selected] + state.get("seen", [])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    title_history = [re.sub(r"[^a-z0-9]+", "", row["title"].lower()) for row in selected] + state.get("seen_titles", [])
    state_path.write_text(json.dumps({"seen": list(dict.fromkeys(combined))[:3000], "seen_titles": list(dict.fromkeys(title_history))[:3000], "updated_at": now.isoformat()}, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Horizon's no-key rules-only daily radar.")
    parser.add_argument("--site-dir", type=Path, default=Path("docs/radar"))
    parser.add_argument("--state", type=Path, default=Path("data/offline/history.json"))
    parser.add_argument("--hours", type=int, default=36)
    args = parser.parse_args()
    report = run(args.site_dir, args.state, args.hours)
    print(f"Generated {len(report['items'])} selected items from {report['total_collected']} collected candidates.")


if __name__ == "__main__":
    main()
