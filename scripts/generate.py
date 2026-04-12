#!/usr/bin/env python3
"""
Daily RSS Digest generator.
Fetches RSS feeds, filters with Qwen LLM, writes JSON to data/.
"""

import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
from dateutil import parser as dateparser
from openai import OpenAI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
FEEDS_FILE = ROOT / "feeds.txt"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
LOOKBACK_HOURS = 24
FETCH_TIMEOUT = 10  # seconds per feed
TOP_N = 5

INTERESTS = """
- AI agent 落地与应用（实际产品、工程实践、案例）
- 产品方法论与用户体验设计
- LLM 工程化（prompt engineering、RAG、eval、fine-tuning）
- 创业、商业模式与增长策略
- 跨学科思维、认知科学、心理学与学习方法
""".strip()

# Qwen via DashScope OpenAI-compatible endpoint
client = OpenAI(
    api_key=os.environ["DASHSCOPE_API_KEY"],
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
MODEL = "qwen-plus-latest"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_feeds():
    return [line.strip() for line in FEEDS_FILE.read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def parse_date(entry):
    """Return a timezone-aware datetime from a feedparser entry, or None."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    # Fallback: try string fields
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
    """Extract a plain-text summary from an entry (max 300 chars)."""
    # Prefer summary field
    text = getattr(entry, "summary", "") or ""
    if not text:
        # Fall back to content
        content = getattr(entry, "content", [])
        if content:
            text = content[0].get("value", "")
    # Strip HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300]


def fetch_full_text(url):
    """Fetch a URL and return plain text of the article body (best-effort)."""
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT,
                            headers={"User-Agent": "RSS-Digest-Bot/1.0"})
        resp.raise_for_status()
        html = resp.text
        # Remove script/style blocks
        html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", html,
                      flags=re.DOTALL | re.IGNORECASE)
        # Strip all tags
        text = re.sub(r"<[^>]+>", " ", html)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception as e:
        print(f"  [full_text skip] {url}: {e}", file=sys.stderr)
        return ""


def get_source_name(feed, entry):
    """Best-effort blog/source name."""
    feed_title = getattr(feed.feed, "title", "") or ""
    author = getattr(entry, "author", "") or ""
    return feed_title or author or "Unknown"


def fetch_recent_articles(feeds, cutoff: datetime):
    articles = []
    for url in feeds:
        try:
            # feedparser doesn't support timeout natively; use requests + parse
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
            articles.append({
                "title": title,
                "url": link,
                "source": get_source_name(feed, entry),
                "published": pub.strftime("%Y-%m-%d %H:%M UTC"),
                "snippet": get_summary(entry),
            })

    print(f"Fetched {len(articles)} articles from last {LOOKBACK_HOURS}h")
    return articles


def select_top_articles(articles):
    if not articles:
        return []

    # Build article list for prompt
    lines = []
    for i, a in enumerate(articles, 1):
        lines.append(
            f"[{i}] 标题: {a['title']}\n"
            f"    来源: {a['source']}\n"
            f"    链接: {a['url']}\n"
            f"    摘要: {a['snippet']}\n"
        )
    article_text = "\n".join(lines)

    prompt = f"""你是一位技术博客精选编辑。以下是今天从 RSS 订阅中抓取的文章列表。

我的兴趣方向：
{INTERESTS}

请从中挑选最值得读的 {TOP_N} 篇文章（优先匹配我的兴趣方向，其次考虑内容质量和独特性）。

对每篇文章，用以下 JSON 格式输出（数组，共 {TOP_N} 个元素）：
{{
  "title": "文章标题（保持原文）",
  "url": "原文链接",
  "source": "博客/作者名",
  "reason": "一句话中文推荐理由（20字以内，说明为什么值得读）",
  "summary": "3-5句话中文摘要，概括文章核心观点"
}}

只输出 JSON 数组，不要有任何其他文字。

文章列表：
{article_text}
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    raw = response.choices[0].message.content.strip()

    # Extract JSON array from response
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        print(f"LLM response did not contain JSON array:\n{raw}", file=sys.stderr)
        return []

    # Replace Chinese/typographic curly quotes inside JSON strings with ASCII quotes
    json_str = match.group()
    json_str = json_str.replace('\u201c', '\\"').replace('\u201d', '\\"')
    json_str = json_str.replace('\u2018', "\\'").replace('\u2019', "\\'")

    try:
        selected = json.loads(json_str)
        return selected[:TOP_N]
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}\nRaw: {raw}", file=sys.stderr)
        return []


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
        print(f"Calling Qwen to select top {TOP_N}...")
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
