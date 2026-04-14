#!/usr/bin/env python3
"""
Daily RSS Digest generator.
Fetches RSS feeds, scores articles by keyword relevance, writes JSON to data/.
No LLM required.
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from dateutil import parser as dateparser

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
FEEDS_FILE = ROOT / "feeds.txt"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
LOOKBACK_HOURS = 120  # 5 days
FETCH_TIMEOUT = 10    # seconds per feed
TOP_N = 5

# ---------------------------------------------------------------------------
# Interest keyword scoring
# Each topic has a weight and a list of keywords (matched case-insensitively).
# Score = sum of weights for each keyword found in title+snippet.
# ---------------------------------------------------------------------------

TOPICS = [
    {
        "name": "AI product & news",
        "weight": 4,
        "keywords": [
            "ai product", "ai feature", "ai launch", "ai release", "ai update",
            "chatgpt", "claude", "gemini", "gpt-4", "gpt-5", "copilot",
            "openai", "anthropic", "google deepmind", "mistral", "meta ai",
            "ai assistant", "ai tool", "ai app", "ai startup", "ai company",
            "ai funding", "ai acquisition", "ai model release",
        ],
    },
    {
        "name": "AI agent & application",
        "weight": 3,
        "keywords": [
            "ai agent", "agentic", "autonomous ai", "ai workflow",
            "multi-agent", "ai automation", "ai in practice", "ai use case",
            "vibe coding", "cursor", "devin", "ai coding",
        ],
    },
    {
        "name": "Product & UX",
        "weight": 3,
        "keywords": [
            "product management", "product strategy", "user experience", "ux research",
            "product-market fit", "product roadmap", "product design",
            "onboarding", "retention", "activation", "churn",
            "software product", "tech product", "app design",
        ],
    },
    {
        "name": "Startup & business",
        "weight": 3,
        "keywords": [
            "startup", "founder", "venture capital", "saas", "arr", "mrr",
            "business model", "monetiz", "fundrais", "bootstrap",
            "series a", "series b", "valuation", "ipo", "acquisition",
            "tech company", "silicon valley",
        ],
    },
    {
        "name": "Cognitive & learning",
        "weight": 2,
        "keywords": [
            "cognitive science", "mental model", "decision making", "psychology",
            "interdisciplin", "systems thinking", "second-order",
        ],
    },
    {
        "name": "LLM engineering",
        "weight": 1,
        "keywords": [
            "prompt engineering", "rag", "fine-tun", "eval", "embedding",
            "context window", "reasoning model", "vector database",
        ],
    },
]

# Sources that consistently block scraping or have no readable content
BLOCKED_SOURCES = [
    "red.anthropic.com",
    "openai.com/blog",
]

# Hard penalty — these topics are never relevant
PENALTY_KEYWORDS = [
    "recipe", "cooking", "sports", "nfl", "nba", "celebrity", "fashion",
    "horoscope", "lottery", "weather", "movie", "film", "tv show", "album",
    "james bond", "hollywood", "box office", "oscar",
    "security vulnerability", "cve", "exploit", "malware", "ransomware",
    "sql injection", "buffer overflow", "penetration test",
    "kernel", "syscall", "assembly language", "linker", "compiler internals",
    "network protocol", "tcp/ip", "ethernet", "openwrt",
    "sponsor", "advertisement", "sponsored",
]

# Minimum score to be included — articles scoring below this are dropped entirely
MIN_SCORE = 2


def score_article(title: str, snippet: str, url: str = "") -> float:
    if any(blocked in url for blocked in BLOCKED_SOURCES):
        return -99  # exclude unscrapable sources
    text = (title + " " + snippet).lower()
    score = 0.0
    for topic in TOPICS:
        for kw in topic["keywords"]:
            if kw.lower() in text:
                score += topic["weight"]
                break  # count each topic at most once
    for kw in PENALTY_KEYWORDS:
        if kw in text:
            score -= 2
    # Slight recency bonus is already handled by the cutoff filter
    return score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_feeds():
    return [line.strip() for line in FEEDS_FILE.read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def parse_date(entry):
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for attr in ("published", "updated"):
        s = getattr(entry, attr, None)
        if s:
            try:
                dt = dateparser.parse(s)
                if dt and dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                pass
    return None


def get_text_from_entry(entry, max_chars):
    import html as html_module
    text = getattr(entry, "summary", "") or ""
    if not text:
        content = getattr(entry, "content", [])
        if content:
            text = content[0].get("value", "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    last = max(chunk.rfind(". "), chunk.rfind("! "), chunk.rfind("? "))
    if last > 100:
        return chunk[:last + 1]
    return chunk


def get_summary(entry):
    return get_text_from_entry(entry, 500)


def extract_article_text(html):
    """Extract main article body from HTML, ignoring nav/sidebar/footer/metadata."""
    # Remove non-content blocks entirely
    for tag in ("script", "style", "nav", "header", "footer", "aside",
                "figure", "figcaption"):
        html = re.sub(
            rf"<{tag}[^>]*>.*?</{tag}>", " ", html,
            flags=re.DOTALL | re.IGNORECASE
        )

    # Try common article content selectors in priority order
    candidates = [
        r'<article[^>]*>(.*?)</article>',
        r'<main[^>]*>(.*?)</main>',
        r'<div[^>]+class="[^"]*\b(?:post-content|entry-content|article-body|article-content|post-body|post-text)\b[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]+class="[^"]*\b(?:post|entry|article|content|body)\b[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]+id="[^"]*\b(?:post|entry|article|content|main)\b[^"]*"[^>]*>(.*?)</div>',
    ]

    best = ""
    for pattern in candidates:
        match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if match:
            block = match.group(1) if match.lastindex else match.group(0)
            text = re.sub(r"<[^>]+>", " ", block)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > len(best):
                best = text

    if not best:
        # Fallback: strip all tags from full page
        best = re.sub(r"<[^>]+>", " ", html)
        best = re.sub(r"\s+", " ", best).strip()

    return clean_text(best)


def clean_text(text):
    """Decode HTML entities and restore paragraph breaks."""
    import html as html_module
    # Decode HTML entities (&#8220; → " etc.)
    text = html_module.unescape(text)
    # Collapse runs of whitespace back to single spaces
    text = re.sub(r" {2,}", " ", text)
    # Restore paragraph breaks: sentence-ending punctuation followed by space + capital
    text = re.sub(r'([.!?]) ([A-Z\u4e00-\u9fa5])', r'\1\n\n\2', text)
    return text.strip()


def fetch_full_text(url):
    import time
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RSS-Digest-Bot/1.0)"}
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=FETCH_TIMEOUT, headers=headers)
            if resp.status_code == 429:
                if attempt == 0:
                    time.sleep(5)
                    continue
                print(f"  [full_text skip] {url}: 429 rate limited", file=sys.stderr)
                return ""
            resp.raise_for_status()
            text = extract_article_text(resp.text)
            if len(text) < 100:
                print(f"  [full_text skip] {url}: extracted text too short ({len(text)} chars)", file=sys.stderr)
                return ""
            return text
        except Exception as e:
            print(f"  [full_text skip] {url}: {e}", file=sys.stderr)
            return ""
    return ""


def get_source_name(feed, entry):
    feed_title = getattr(feed.feed, "title", "") or ""
    author = getattr(entry, "author", "") or ""
    return feed_title or author or "Unknown"


def fetch_recent_articles(feeds, cutoff):
    articles = []
    for url in feeds:
        try:
            resp = requests.get(url, timeout=FETCH_TIMEOUT,
                                headers={"User-Agent": "RSS-Digest-Bot/1.0"})
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"  [skip] {url}: {e}", file=sys.stderr)
            continue

        for entry in feed.entries:
            pub = parse_date(entry)
            if pub is None or pub < cutoff:
                continue
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            if not title or not link:
                continue
            snippet = get_summary(entry)
            full_snippet = get_text_from_entry(entry, 3000)
            articles.append({
                "title": title,
                "url": link,
                "source": get_source_name(feed, entry),
                "published": pub.strftime("%Y-%m-%d %H:%M UTC"),
                "snippet": snippet,
                "_score": score_article(title, snippet, link),
                "_pub_dt": pub,
                "_snippet_short": len(snippet) < 120,
                "_full_snippet": full_snippet,
            })

    print(f"Fetched {len(articles)} articles from last {LOOKBACK_HOURS}h")
    return articles


def select_top_articles(articles):
    if not articles:
        return []

    # Sort by score desc, then by recency desc as tiebreaker
    ranked = sorted(articles, key=lambda a: (a["_score"], a["_pub_dt"]), reverse=True)

    # Log scores for visibility
    print("Top scored articles:")
    for a in ranked[:10]:
        print(f"  [{a['_score']:.0f}] {a['title'][:80]}")

    # Drop articles below minimum relevance threshold
    qualified = [a for a in ranked if a["_score"] >= MIN_SCORE]
    print(f"Qualified (score >= {MIN_SCORE}): {len(qualified)} articles")
    top = qualified[:TOP_N]

    # Build output — drop internal fields, add reason from matched topics
    selected = []
    for a in top:
        text = (a["title"] + " " + a["snippet"]).lower()
        matched = [t["name"] for t in TOPICS if any(kw in text for kw in t["keywords"])]
        reason = "涵盖 " + "、".join(matched[:2]) if matched else "综合推荐"
        selected.append({
            "title": a["title"],
            "url": a["url"],
            "source": a["source"],
            "published": a["published"],
            "reason": reason,
            "summary": a["snippet"],
        })

    return selected


def load_seen_urls() -> set:
    """Collect all article URLs already shown in previous digests."""
    seen = set()
    for f in DATA_DIR.glob("*.json"):
        if f.name == "index.json":
            continue
        try:
            data = json.loads(f.read_text())
            for a in data.get("articles", []):
                if a.get("url"):
                    seen.add(a["url"])
        except Exception:
            pass
    return seen


def update_index(date_str):
    index_path = DATA_DIR / "index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text())
    else:
        index = {"dates": []}
    if date_str not in index["dates"]:
        index["dates"].insert(0, date_str)
        index["dates"].sort(reverse=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    print(f"Updated index.json: {len(index['dates'])} dates")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"=== RSS Digest Generator — {TODAY} ===")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    print(f"Fetching articles since {cutoff.strftime('%Y-%m-%d %H:%M UTC')}")

    feeds = load_feeds()
    print(f"Loaded {len(feeds)} feeds")

    articles = fetch_recent_articles(feeds, cutoff)

    seen_urls = load_seen_urls()
    articles = [a for a in articles if a["url"] not in seen_urls]
    print(f"After dedup: {len(articles)} new articles (excluded {len(seen_urls)} seen URLs)")

    if articles:
        selected = select_top_articles(articles)
        print(f"Fetching full text for {len(selected)} selected articles...")
        for a in selected:
            full = fetch_full_text(a["url"])
            snippet_short = a.pop("_snippet_short", False)
            if full:
                a["full_text"] = full
                # If RSS snippet was too short, extract a better summary from full text
                if snippet_short:
                    chunk = full[:600]
                    last = max(chunk.rfind(". "), chunk.rfind("! "), chunk.rfind("? "))
                    a["summary"] = chunk[:last + 1].strip() if last > 80 else chunk[:500].strip()
            else:
                # JS-rendered or blocked page — use a longer snippet from the RSS feed
                # stored in _full_snippet if available, otherwise leave full_text empty
                a["full_text"] = a.pop("_full_snippet", "")
        for a in selected:
            a.pop("_full_snippet", None)
    else:
        selected = []

    output = {
        "date": TODAY,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total_fetched": len(articles),
        "articles": selected,
        "note": "" if selected else "今日暂无符合条件的新文章，请明天再来。",
    }

    out_path = DATA_DIR / f"{TODAY}.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"Written: {out_path}")

    update_index(TODAY)
    print("Done.")


if __name__ == "__main__":
    main()
