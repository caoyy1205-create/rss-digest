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
LOOKBACK_HOURS = 72   # 3 days to handle weekends / slow feeds
FETCH_TIMEOUT = 10    # seconds per feed
TOP_N = 5

# ---------------------------------------------------------------------------
# Interest keyword scoring
# Each topic has a weight and a list of keywords (matched case-insensitively).
# Score = sum of weights for each keyword found in title+snippet.
# ---------------------------------------------------------------------------

TOPICS = [
    {
        "name": "AI agent",
        "weight": 3,
        "keywords": [
            "agent", "agentic", "autonomous", "tool use", "tool-use",
            "multi-agent", "orchestration", "workflow automation",
        ],
    },
    {
        "name": "LLM engineering",
        "weight": 3,
        "keywords": [
            "llm", "large language model", "prompt", "rag", "retrieval",
            "fine-tun", "finetun", "eval", "embedding", "context window",
            "chain-of-thought", "reasoning model", "inference",
        ],
    },
    {
        "name": "AI / ML general",
        "weight": 2,
        "keywords": [
            "gpt", "claude", "gemini", "mistral", "openai", "anthropic",
            "machine learning", "deep learning", "neural", "transformer",
            "diffusion", "multimodal", "foundation model",
        ],
    },
    {
        "name": "Product & UX",
        "weight": 2,
        "keywords": [
            "product", "ux", "user experience", "design", "interface",
            "onboarding", "retention", "metrics", "a/b test", "roadmap",
        ],
    },
    {
        "name": "Startup & business",
        "weight": 2,
        "keywords": [
            "startup", "founder", "venture", "saas", "revenue", "growth",
            "business model", "monetiz", "fundrais", "bootstrap",
        ],
    },
    {
        "name": "Cognitive & learning",
        "weight": 1,
        "keywords": [
            "cognitive", "mental model", "learning", "memory", "psychology",
            "decision", "bias", "thinking", "interdisciplin",
        ],
    },
    {
        "name": "Engineering quality",
        "weight": 1,
        "keywords": [
            "software engineering", "architecture", "refactor", "testing",
            "observability", "reliability", "scalab", "distributed",
        ],
    },
]

# Penalty keywords — lower score for off-topic content
PENALTY_KEYWORDS = [
    "recipe", "cooking", "sports", "celebrity", "fashion",
    "horoscope", "lottery", "weather forecast",
]


def score_article(title: str, snippet: str) -> float:
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


def get_summary(entry):
    text = getattr(entry, "summary", "") or ""
    if not text:
        content = getattr(entry, "content", [])
        if content:
            text = content[0].get("value", "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


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
            articles.append({
                "title": title,
                "url": link,
                "source": get_source_name(feed, entry),
                "published": pub.strftime("%Y-%m-%d %H:%M UTC"),
                "snippet": snippet,
                "_score": score_article(title, snippet),
                "_pub_dt": pub,
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

    top = ranked[:TOP_N]

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

    if articles:
        selected = select_top_articles(articles)
        print(f"Fetching full text for {len(selected)} selected articles...")
        for a in selected:
            a["full_text"] = fetch_full_text(a["url"])
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
