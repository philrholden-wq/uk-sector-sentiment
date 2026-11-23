#!/usr/bin/env python3
"""
collector.py - UK consumer sentiment collector (no Reddit API required).

Sources (each is optional and runs only when its key is set):

  1. Bluesky        (atproto)                   BLUESKY_HANDLE, BLUESKY_APP_PASSWORD
  2. YouTube        (google-api-python-client)  YOUTUBE_API_KEY
  3. Reddit threads via Brave Search API         SEARCH_API_KEY  (sent as X-Subscription-Token)

Sectors:
  Each market sector is a JSON file in sectors/ (brands, aliases, phrases).
  uk-high-street-banking.json is created automatically if the folder is empty.
  New sectors can be added from GitHub's "Run workflow" form (see README).

Every run, for each sector:
  * collects recent UK-relevant mentions of the sector's brands and phrases
  * removes duplicates (same item, same Reddit thread, or identical text)
  * scores each mention with VADER
  * writes data/<sector>/sentiment_YYYY-MM-DD.csv (same-day reruns overwrite)
  * updates data/<sector>/history_index.json (one summary row per run, read by app.py)
  * counts Brave requests in data/api_usage.json and stops at BRAVE_MONTHLY_LIMIT

Usage:
  python collector.py                                 # every weekly sector, last 7 days
  python collector.py --sector uk-high-street-banking # one sector
  python collector.py --new-sector-name "UK supermarkets" \\
      --new-brands "Tesco*, Sainsbury's*, Asda*, Aldi, Lidl" --new-terms "supermarket, food shop"
  python collector.py --demo --data-dir demo_data     # synthetic data, no keys needed
  python collector.py --rebuild-index                 # rebuild the indexes from the CSVs
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# =============================================================================
# SECTOR CONFIGURATION
# Each sector is a JSON file in sectors/ (for example sectors/uk-supermarkets.json)
# and its data goes to data/<file name>/. Add a sector without editing code:
# GitHub > Actions > "Weekly sentiment collection" > Run workflow, then fill in
# "New sector name", "Brands" and "Sector phrases". See README, "Adding sectors".
# =============================================================================
SECTORS_DIR = Path(os.getenv("SECTORS_DIR", "sectors"))

# UK filters shared by every sector. A sector file can add its own with
# "extra_uk_signals" and "extra_uk_subreddits".
# A mention from Bluesky, YouTube or a non-UK subreddit is kept only if it
# contains at least one UK signal (or names a brand marked uk_unique).
GENERIC_UK_SIGNALS = [
    "£", "UK", "U.K.", "Britain", "British", "England", "English", "Scotland",
    "Scottish", "Wales", "Welsh", "Northern Ireland", "London", "Manchester",
    "Birmingham", "Glasgow", "Edinburgh", "Cardiff", "Belfast", "Leeds",
    "Liverpool", "Bristol", "high street", "HMRC", "quid", "council tax",
    "Post Office", "NHS",
]
# Reddit results from these subreddits count as UK without an extra signal.
GENERIC_UK_SUBREDDITS = [
    "AskUK", "UKPersonalFinance", "unitedkingdom", "CasualUK", "britishproblems",
    "LegalAdviceUK", "london", "Scotland", "Wales", "northernireland", "AskBrits",
]
# Brave runs one targeted query per search term for each of these subreddits,
# plus one general "site:reddit.com + UK" query per term.
DEFAULT_TARGET_SUBREDDITS = ["AskUK", "CasualUK"]
MAX_TARGET_SUBREDDITS = 3

# Written to sectors/uk-high-street-banking.json if the sectors folder is empty.
DEFAULT_SECTOR: dict = {
    "sector_name": "UK high street banking",
    "include_in_schedule": True,

    # Brands to track. Aliases are matched as whole words, case-insensitive.
    # The first alias is used as the search query. Set "uk_unique" to true only
    # when the name is unambiguous outside the UK: a uk_unique brand counts as
    # a UK signal by itself.
    "brands": {
        "Barclays":         {"aliases": ["Barclays", "Barclaycard"], "uk_unique": True},
        "HSBC":             {"aliases": ["HSBC UK", "HSBC"], "uk_unique": False},
        "Lloyds":           {"aliases": ["Lloyds Bank", "Lloyds"], "uk_unique": True},
        "NatWest":          {"aliases": ["NatWest", "Nat West"], "uk_unique": True},
        "RBS":              {"aliases": ["Royal Bank of Scotland", "RBS"], "uk_unique": True},
        "Santander":        {"aliases": ["Santander UK", "Santander"], "uk_unique": False},
        "Halifax":          {"aliases": ["Halifax Bank", "Halifax"], "uk_unique": False},
        "Nationwide":       {"aliases": ["Nationwide Building Society", "Nationwide"], "uk_unique": False},
        "TSB":              {"aliases": ["TSB Bank", "TSB"], "uk_unique": True},
        "Monzo":            {"aliases": ["Monzo"], "uk_unique": True},
        "Starling":         {"aliases": ["Starling Bank"], "uk_unique": True},
        "Revolut":          {"aliases": ["Revolut"], "uk_unique": False},
        "Virgin Money":     {"aliases": ["Virgin Money"], "uk_unique": True},
        "Metro Bank":       {"aliases": ["Metro Bank"], "uk_unique": True},
        "First Direct":     {"aliases": ["First Direct"], "uk_unique": True},
        "Co-op Bank":       {"aliases": ["Co-op Bank", "Co-operative Bank"], "uk_unique": True},
        "Bank of Scotland": {"aliases": ["Bank of Scotland"], "uk_unique": True},
        "Chase UK":         {"aliases": ["Chase UK"], "uk_unique": True},
    },

    # Generic sector phrases. They count as a match without a brand.
    "sector_terms": [
        "high street bank", "bank branch", "branch closure", "current account",
        "switching bank", "banking app", "overdraft", "sort code", "faster payments",
    ],
    "extra_uk_signals": ["sort code", "FCA", "Financial Ombudsman", "ISA", "Faster Payments"],
    "extra_uk_subreddits": [],
    "brave_target_subreddits": ["UKPersonalFinance", "AskUK"],
}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", " and ")).strip("-") or "sector"


def expand_config(raw: dict, slug: str) -> dict:
    """Adds the shared UK filters to a sector file and checks it is usable."""
    if not str(raw.get("sector_name", "")).strip():
        raise ValueError("missing sector_name")
    brands = raw.get("brands") or {}
    for brand, spec in brands.items():
        if not spec.get("aliases"):
            raise ValueError(f"brand '{brand}' has no aliases")
    if not brands and not raw.get("sector_terms"):
        raise ValueError("needs at least one brand or sector term")
    targets = (raw.get("brave_target_subreddits") or DEFAULT_TARGET_SUBREDDITS)[:MAX_TARGET_SUBREDDITS]
    return {
        **raw, "slug": slug, "brands": brands,
        "sector_terms": list(raw.get("sector_terms") or []),
        "brave_target_subreddits": targets,
        "uk_signals": list(dict.fromkeys(GENERIC_UK_SIGNALS + list(raw.get("extra_uk_signals") or []))),
        "uk_subreddits": list(dict.fromkeys(
            GENERIC_UK_SUBREDDITS + list(raw.get("extra_uk_subreddits") or []) + targets)),
    }


def format_sector_json(raw: dict) -> str:
    """Readable JSON: one line per brand and per list, so the file is easy to edit on GitHub."""
    def one(v):
        return json.dumps(v, ensure_ascii=False)
    lines = []
    for key, value in raw.items():
        if key == "brands":
            inner = [f"    {one(b)}: {one(spec)}" for b, spec in value.items()]
            lines.append('  "brands": {\n' + ",\n".join(inner) + "\n  }" if inner else '  "brands": {}')
        else:
            lines.append(f"  {one(key)}: {one(value)}")
    return "{\n" + ",\n".join(lines) + "\n}\n"


def save_sector(raw: dict) -> str:
    SECTORS_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(raw["sector_name"])
    path = SECTORS_DIR / f"{slug}.json"
    path.write_text(format_sector_json(raw), encoding="utf-8")
    log.info("Saved sector settings to %s", path)
    return slug


def load_sectors() -> dict[str, dict]:
    """All sector files, keyed by file name without .json."""
    if not list(SECTORS_DIR.glob("*.json")):
        save_sector(DEFAULT_SECTOR)
    sectors = {}
    for path in sorted(SECTORS_DIR.glob("*.json")):
        try:
            sectors[path.stem] = expand_config(json.loads(path.read_text(encoding="utf-8")), path.stem)
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
            log.error("Skipping %s: %s", path.name, exc)
    return sectors


def _split_list(text: str | None) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]", text or "") if x.strip()]


def sector_from_form(name: str, brands_text: str, terms_text: str, subreddits_text: str,
                     schedule: bool) -> dict:
    """Builds a sector from the GitHub "Run workflow" form fields.

    Brands are comma-separated. "|" adds aliases and "*" marks a UK-only name,
    e.g. "Tesco*, Sainsbury's*|Sainsburys, Marks & Spencer*|M&S, Aldi, Lidl".
    """
    brands: dict = {}
    for item in _split_list(brands_text):
        uk_unique = "*" in item
        names = [n.strip() for n in item.replace("*", "").split("|") if n.strip()]
        if not names:
            continue
        aliases = []
        for n in names:   # match straight and curly apostrophes, and none at all
            aliases.append(n)
            if "'" in n or "\u2019" in n:
                plain = n.replace("\u2019", "'")
                aliases += [plain, plain.replace("'", "\u2019"), plain.replace("'", "")]
        brands[names[0]] = {"aliases": list(dict.fromkeys(aliases)), "uk_unique": uk_unique}
    subs = [s.strip().removeprefix("r/").strip("/") for s in _split_list(subreddits_text)]
    return {
        "sector_name": name.strip(),
        "include_in_schedule": schedule,
        "brands": brands,
        "sector_terms": _split_list(terms_text),
        "extra_uk_signals": [],
        "extra_uk_subreddits": [],
        "brave_target_subreddits": [s for s in subs if s][:MAX_TARGET_SUBREDDITS] or DEFAULT_TARGET_SUBREDDITS,
    }


# ---- Collection limits (kept well inside each free tier) --------------------
BLUESKY_PAGE_SIZE = 100            # API maximum per page
BLUESKY_MAX_PAGES_PER_QUERY = 3    # up to 300 posts per query term

YOUTUBE_MAX_SEARCHES = 20          # search.list: 100 calls/day free bucket
YOUTUBE_VIDEOS_PER_SEARCH = 10
YOUTUBE_COMMENTS_PER_VIDEO = 50    # commentThreads.list: 1 unit per call

BRAVE_MAX_REQUESTS = 90            # per sector per run (banking: 27 terms x 3 = 81)
# Brave's $5 monthly credit covers about 1,000 requests. The collector counts its
# requests in data/api_usage.json and stops at this monthly ceiling.
BRAVE_MONTHLY_LIMIT = int(os.getenv("BRAVE_MONTHLY_LIMIT", "900"))
SEARCH_RESULTS_PER_QUERY = 20      # Brave maximum per request
BRAVE_PAUSE_SECONDS = 1.1          # stay well under the rate limit

TEXT_MAX_CHARS = 1500              # text stored per row (scoring uses full text)

# YouTube's Developer Policies (III.E.4) say API data such as comment text must be
# refreshed or deleted within 30 days. Runs are weekly, so YouTube rows are removed
# from CSVs once they are 21+ days old, which keeps every copy under 30 days.
# The run's summary in history_index.json is kept. See README, "Compliance".
YOUTUBE_RETENTION_DAYS = int(os.getenv("YOUTUBE_RETENTION_DAYS", "21"))
HTTP_TIMEOUT = 30

POSITIVE_THRESHOLD = 0.05          # VADER's standard thresholds
NEGATIVE_THRESHOLD = -0.05

UK_TZ = ZoneInfo("Europe/London")
SOURCE_LABELS = {"bluesky": "Bluesky", "youtube": "YouTube", "reddit_brave": "Reddit (via Brave)"}
CSV_COLUMNS = [
    "run_date", "collected_at_utc", "source", "item_type", "item_id", "community",
    "context", "brands", "matched_terms", "text", "created_utc", "engagement", "url",
    "author_hash", "compound", "pos", "neu", "neg", "sentiment",
]

log = logging.getLogger("collector")


# =============================================================================
# Matching helpers
# =============================================================================
def _term_pattern(term: str) -> re.Pattern:
    """Whole-word, case-insensitive pattern. Symbol terms such as '£' match as substrings."""
    escaped = re.escape(term).replace(r"\ ", r"[\s\-]?")
    if term[:1].isalnum() and term[-1:].isalnum():
        return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


@dataclass
class Matcher:
    config: dict
    brand_patterns: dict = field(default_factory=dict)
    sector_patterns: list = field(default_factory=list)
    uk_patterns: list = field(default_factory=list)
    uk_unique_brands: set = field(default_factory=set)
    uk_subreddits: set = field(default_factory=set)

    def __post_init__(self) -> None:
        for brand, spec in self.config["brands"].items():
            self.brand_patterns[brand] = [_term_pattern(a) for a in spec["aliases"]]
            if spec.get("uk_unique"):
                self.uk_unique_brands.add(brand)
        self.sector_patterns = [(t, _term_pattern(t)) for t in self.config["sector_terms"]]
        # "UK" is matched case-sensitively so it cannot hit words like "Ukraine".
        self.uk_patterns = [
            re.compile(r"(?<![A-Za-z])UK(?![A-Za-z])") if s == "UK" else _term_pattern(s)
            for s in self.config["uk_signals"]
        ]
        self.uk_subreddits = {s.lower() for s in self.config["uk_subreddits"]}

    def brands_in(self, text: str) -> list[str]:
        return [b for b, pats in self.brand_patterns.items() if any(p.search(text) for p in pats)]

    def sector_terms_in(self, text: str) -> list[str]:
        return [t for t, p in self.sector_patterns if p.search(text)]

    def has_uk_signal(self, text: str, brands: list[str]) -> bool:
        return any(b in self.uk_unique_brands for b in brands) or any(p.search(text) for p in self.uk_patterns)

    def search_queries(self) -> list[str]:
        """Primary alias of each brand, then each sector term."""
        qs = [spec["aliases"][0] for spec in self.config["brands"].values()]
        return list(dict.fromkeys(qs + self.config["sector_terms"]))


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)                 # HTML tags (Brave/YouTube)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_>#`~|]+", " ", text)              # light markdown clean-up
    return re.sub(r"\s+", " ", text).strip()


def author_hash(source: str, author: str | None) -> str:
    """Pseudonymise authors so names and handles are not committed to the repository."""
    return hashlib.sha256(f"{source}:{author}".encode()).hexdigest()[:12] if author else ""


def iso_utc(ts) -> str:
    if ts in (None, ""):
        return ""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = pd.to_datetime(str(ts), errors="coerce", utc=True)
    return "" if pd.isna(parsed) else parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_row(*, source, item_type, item_id, community, text, created, engagement,
             url, author, brands, matched_terms, context="") -> dict:
    return {
        "source": source, "item_type": item_type, "item_id": str(item_id),
        "community": community, "context": context[:300], "brands": "; ".join(brands),
        "matched_terms": "; ".join(matched_terms), "text": text,
        "created_utc": iso_utc(created), "engagement": int(engagement or 0),
        "url": url, "author_hash": author_hash(source, author),
    }


@dataclass
class SourceResult:
    rows: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    enabled: bool = True
    requests: int = 0


# =============================================================================
# 1. Bluesky (atproto)
# =============================================================================
def _get(obj, name, default=None):
    if obj is None:
        return default
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def collect_bluesky(matcher: Matcher, cutoff: datetime) -> SourceResult:
    handle, app_password = os.getenv("BLUESKY_HANDLE"), os.getenv("BLUESKY_APP_PASSWORD")
    if not (handle and app_password):
        log.warning("Bluesky skipped: BLUESKY_HANDLE / BLUESKY_APP_PASSWORD not set.")
        return SourceResult(enabled=False)

    from atproto import Client
    from atproto_client.exceptions import AtProtocolError

    res = SourceResult()
    client = Client()
    try:
        client.login(handle, app_password)
    except AtProtocolError as exc:
        res.warnings.append(f"Bluesky login failed ({exc}). Use an app password, not your main password.")
        log.error(res.warnings[-1])
        return res

    since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    seen: set[str] = set()
    for q in matcher.search_queries():
        cursor = None
        for _ in range(BLUESKY_MAX_PAGES_PER_QUERY):
            params = {"q": q, "sort": "latest", "since": since, "lang": "en", "limit": BLUESKY_PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = client.app.bsky.feed.search_posts(params=params)
            except AtProtocolError as exc:
                res.warnings.append(f"Bluesky search error for '{q}': {exc}")
                log.warning(res.warnings[-1])
                break
            for post in resp.posts or []:
                if post.uri in seen:
                    continue
                seen.add(post.uri)
                text = clean_text(_get(post.record, "text", ""))
                brands, terms = matcher.brands_in(text), matcher.sector_terms_in(text)
                if not (brands or terms) or not matcher.has_uk_signal(text, brands):
                    continue
                author = _get(post.author, "handle", "")
                engagement = (post.like_count or 0) + (post.repost_count or 0) + (post.reply_count or 0)
                res.rows.append(make_row(
                    source="bluesky", item_type="post", item_id=post.uri, community="Bluesky",
                    text=text, created=_get(post.record, "created_at") or post.indexed_at,
                    engagement=engagement,
                    url=f"https://bsky.app/profile/{author}/post/{post.uri.rsplit('/', 1)[-1]}",
                    author=author, brands=brands, matched_terms=terms))
            cursor = resp.cursor
            if not cursor or not resp.posts:
                break
            time.sleep(0.3)
    log.info("Bluesky: %d UK-relevant posts", len(res.rows))
    return res


# =============================================================================
# 2. YouTube Data API v3
# =============================================================================
def collect_youtube(matcher: Matcher, cutoff: datetime) -> SourceResult:
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        log.warning("YouTube skipped: YOUTUBE_API_KEY not set.")
        return SourceResult(enabled=False)

    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    res = SourceResult()
    yt = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    published_after = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    videos: dict[str, dict] = {}

    # Step 1: find recent videos. regionCode="GB" returns videos viewable in the UK,
    # not only UK-made ones, so each video also needs a UK signal (checked below).
    for q in matcher.search_queries()[:YOUTUBE_MAX_SEARCHES]:
        try:
            resp = yt.search().list(
                part="snippet", q=q, type="video", regionCode="GB", relevanceLanguage="en",
                publishedAfter=published_after, order="relevance",
                maxResults=YOUTUBE_VIDEOS_PER_SEARCH).execute()
        except HttpError as exc:
            res.warnings.append(f"YouTube search error for '{q}': {exc.reason if hasattr(exc, 'reason') else exc}")
            log.warning(res.warnings[-1])
            if exc.resp.status == 403:   # quota exhausted or key not enabled: stop searching
                break
            continue
        for item in resp.get("items", []):
            vid = item["id"].get("videoId")
            if not vid or vid in videos:
                continue
            sn = item["snippet"]
            meta = clean_text(f"{sn.get('title', '')}. {sn.get('description', '')} {sn.get('channelTitle', '')}")
            brands, terms = matcher.brands_in(meta), matcher.sector_terms_in(meta)
            if not (brands or terms) or not matcher.has_uk_signal(meta, brands):
                continue
            videos[vid] = {"title": clean_text(sn.get("title", "")), "channel": sn.get("channelTitle", ""),
                           "brands": brands, "terms": terms}

    # Step 2: top comments on each matching video. Comments inherit the video's
    # brands when they do not name one themselves.
    for vid, v in videos.items():
        try:
            resp = yt.commentThreads().list(
                part="snippet", videoId=vid, order="relevance", textFormat="plainText",
                maxResults=YOUTUBE_COMMENTS_PER_VIDEO).execute()
        except HttpError as exc:
            if "commentsDisabled" not in str(exc):
                res.warnings.append(f"YouTube comments error for video {vid}: {exc}")
            continue
        for thread in resp.get("items", []):
            c = thread["snippet"]["topLevelComment"]["snippet"]
            if pd.to_datetime(c.get("publishedAt"), utc=True) < cutoff:
                continue
            text = clean_text(c.get("textDisplay") or c.get("textOriginal", ""))
            if len(text) < 15:
                continue
            res.rows.append(make_row(
                source="youtube", item_type="comment", item_id=thread["id"],
                community=f"YouTube: {v['channel']}"[:80], context=v["title"], text=text,
                created=c.get("publishedAt"), engagement=c.get("likeCount", 0),
                url=f"https://www.youtube.com/watch?v={vid}&lc={thread['id']}",
                author=c.get("authorChannelId", {}).get("value") or c.get("authorDisplayName"),
                brands=matcher.brands_in(text) or v["brands"],
                matched_terms=matcher.sector_terms_in(text) or ["(video)"]))
    log.info("YouTube: %d comments from %d UK-relevant videos", len(res.rows), len(videos))
    return res


# =============================================================================
# 3. Reddit threads via the Brave Search API (no Reddit API key needed)
# =============================================================================
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
REDDIT_URL = re.compile(r"reddit\.com/r/([^/]+)/comments/([a-z0-9]+)", re.IGNORECASE)


def brave_queries(matcher: Matcher) -> list[str]:
    """For each search term: one query per targeted UK subreddit, plus one general
    site:reddit.com query with a UK term. Results are always re-checked locally
    against the URL and UK filters, so an imperfect query never leaks non-UK data."""
    queries = []
    for term in matcher.search_queries():
        for sub in matcher.config["brave_target_subreddits"]:
            queries.append(f'"{term}" site:reddit.com/r/{sub}')
        queries.append(f'"{term}" UK site:reddit.com')
    return queries[:BRAVE_MAX_REQUESTS]


def _brave_search(query: str, freshness: str, key: str) -> list[dict]:
    params = {"q": query, "country": "GB", "search_lang": "en", "count": SEARCH_RESULTS_PER_QUERY,
              "freshness": freshness, "extra_snippets": "true"}
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}
    for attempt in range(3):
        r = requests.get(BRAVE_ENDPOINT, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code == 429 and attempt < 2:          # rate limited: back off and retry
            time.sleep(2 ** attempt * 2)
            continue
        r.raise_for_status()
        break
    out = []
    for x in r.json().get("web", {}).get("results", []):
        snippet = " ".join([x.get("description", "")] + (x.get("extra_snippets") or []))
        out.append({"title": x.get("title", ""), "url": x.get("url", ""), "snippet": snippet,
                    "published": x.get("page_age")})
    return out


def collect_reddit_brave(matcher: Matcher, cutoff: datetime, budget: int = BRAVE_MAX_REQUESTS) -> SourceResult:
    key = os.getenv("SEARCH_API_KEY")
    if not key:
        log.warning("Reddit via Brave skipped: SEARCH_API_KEY not set.")
        return SourceResult(enabled=False)

    res = SourceResult()
    # Custom date range keeps the window identical to the other sources.
    freshness = f"{cutoff.date().isoformat()}to{datetime.now(timezone.utc).date().isoformat()}"
    seen_threads: set[str] = set()
    queries = brave_queries(matcher)
    if budget < len(queries):
        res.warnings.append(f"Brave monthly limit ({BRAVE_MONTHLY_LIMIT} requests) reached: ran "
                            f"{max(budget, 0)} of {len(queries)} Reddit searches for this sector.")
        log.warning(res.warnings[-1])
        queries = queries[:max(budget, 0)]
    for q in queries:
        res.requests += 1
        try:
            results = _brave_search(q, freshness, key)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            res.warnings.append(f"Brave search error {code} for '{q}'")
            log.warning(res.warnings[-1])
            if code in (401, 402, 403, 422, 429):          # bad key, no credit or still rate-limited
                break
            continue
        except requests.RequestException as exc:
            res.warnings.append(f"Brave network error for '{q}': {exc}")
            continue
        for x in results:
            m = REDDIT_URL.search(x["url"])
            if not m:                                      # skip subreddit index pages, user pages etc.
                continue
            subreddit, thread_id = m.group(1), m.group(2).lower()
            if thread_id in seen_threads:                  # same thread from another query or URL form
                continue
            title = re.sub(r"\s*:\s*r/\w+\s*$", "", clean_text(x["title"]))   # drop " : r/AskUK" suffix
            text = clean_text(f"{title}. {x['snippet']}")
            brands, terms = matcher.brands_in(text), matcher.sector_terms_in(text)
            if not (brands or terms):
                continue
            if subreddit.lower() not in matcher.uk_subreddits and not matcher.has_uk_signal(text, brands):
                continue
            seen_threads.add(thread_id)
            res.rows.append(make_row(
                source="reddit_brave", item_type="thread_snippet", item_id=thread_id,
                community=f"r/{subreddit}", context=title, text=text, created=x.get("published"),
                engagement=0, url=f"https://www.reddit.com/r/{subreddit}/comments/{thread_id}/",
                author=None, brands=brands, matched_terms=terms))
        time.sleep(BRAVE_PAUSE_SECONDS)
    log.info("Reddit via Brave: %d UK-relevant threads from %d queries", len(res.rows), len(queries))
    return res


# =============================================================================
# Demo data (preview the dashboard without any keys)
# =============================================================================
DEMO_SNIPPETS = {
    "positive": [
        "Switched my current account to {b} last month and the app is brilliant, sorted in minutes.",
        "Have to say {b} customer service were great today, refunded the £40 fee without any fuss.",
        "{b} staff in town were really helpful setting up my mum's account. Lovely people.",
        "Got my £175 switching bonus from {b}, easiest money I've made all year.",
    ],
    "negative": [
        "{b} have closed yet another branch on our high street. Nearest one is now 20 miles away.",
        "Absolutely furious with {b}, card blocked abroad again and an hour on hold.",
        "{b} app has been down all morning and I can't pay my council tax. Useless.",
        "Overdraft charges from {b} are a scandal, £30 for going 50p over.",
    ],
    "neutral": [
        "Does anyone know if {b} let you open a joint account online in the UK?",
        "Moving house, do I need to tell {b} or does it update automatically?",
        "What's the sort code format for {b} business accounts?",
    ],
}


def collect_demo(matcher: Matcher, run_date: str) -> list[dict]:
    rnd = random.Random(run_date)                          # reproducible per date
    brands = list(matcher.config["brands"])
    bias = rnd.uniform(-0.15, 0.15)
    run_ts = datetime.fromisoformat(run_date).replace(tzinfo=timezone.utc).timestamp()
    rows = []
    for i in range(rnd.randint(180, 320)):
        mood = rnd.choices(list(DEMO_SNIPPETS), weights=[0.35 + bias, 0.40 - bias, 0.25])[0]
        b = rnd.choice(brands)
        text = rnd.choice(DEMO_SNIPPETS[mood]).format(b=b)
        source = rnd.choice(["bluesky", "youtube", "youtube", "reddit_brave"])
        community = {"bluesky": "Bluesky", "youtube": "YouTube: Demo Money Channel",
                     "reddit_brave": f"r/{rnd.choice(matcher.config['uk_subreddits'])}"}[source]
        rows.append(make_row(
            source=source, item_type={"bluesky": "post", "youtube": "comment"}.get(source, "thread_snippet"),
            item_id=f"demo-{run_date}-{i}", community=community, text=text,
            context="Demo video or thread title" if source != "bluesky" else "",
            created=run_ts - rnd.randint(0, 6 * 86400), engagement=rnd.randint(0, 250),
            url="https://example.com/demo", author=f"demo{i}", brands=[b],
            matched_terms=matcher.sector_terms_in(text)))
    return rows


# =============================================================================
# Deduplication, scoring, summary and storage
# =============================================================================
def deduplicate(rows: list[dict]) -> list[dict]:
    """Drop repeats of the same item, then identical text across sources
    (cross-posts and copy-pasted complaints). The first copy is kept."""
    out, seen_ids, seen_text = [], set(), set()
    for r in rows:
        key_id = (r["source"], r["item_id"])
        key_text = hashlib.md5(re.sub(r"\W+", "", r["text"].lower()).encode()).hexdigest()
        if key_id in seen_ids or key_text in seen_text:
            continue
        seen_ids.add(key_id)
        seen_text.add(key_text)
        out.append(r)
    return out


def score(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=CSV_COLUMNS)
    analyser = SentimentIntensityAnalyzer()
    df = pd.DataFrame(rows)
    scores = pd.DataFrame(df["text"].map(analyser.polarity_scores).tolist())
    df = pd.concat([df, scores[["compound", "pos", "neu", "neg"]]], axis=1)
    # VADER convention: compound >= 0.05 positive, <= -0.05 negative, otherwise neutral.
    df["sentiment"] = "neutral"
    df.loc[df["compound"] >= POSITIVE_THRESHOLD, "sentiment"] = "positive"
    df.loc[df["compound"] <= NEGATIVE_THRESHOLD, "sentiment"] = "negative"
    df["text"] = df["text"].str.slice(0, TEXT_MAX_CHARS)
    return df


def _net(counts: pd.Series, n: int) -> float | None:
    return round(100 * (counts.get("positive", 0) - counts.get("negative", 0)) / n, 1) if n else None


def summarise(df: pd.DataFrame) -> dict:
    total = int(len(df))
    if total == 0:
        return {"total_mentions": 0, "positive": 0, "negative": 0, "neutral": 0,
                "positive_pct": None, "negative_pct": None, "neutral_pct": None,
                "net_sentiment": None, "mean_compound": None, "by_source": {}}
    c = df["sentiment"].value_counts()
    pos, neg, neu = (int(c.get(k, 0)) for k in ("positive", "negative", "neutral"))
    by_source = {}
    for src, g in df.groupby("source"):
        gc, n = g["sentiment"].value_counts(), len(g)
        by_source[src] = {"mentions": int(n),
                          "positive_pct": round(100 * gc.get("positive", 0) / n, 1),
                          "negative_pct": round(100 * gc.get("negative", 0) / n, 1),
                          "net_sentiment": _net(gc, n)}
    return {
        "total_mentions": total, "positive": pos, "negative": neg, "neutral": neu,
        "positive_pct": round(100 * pos / total, 1),
        "negative_pct": round(100 * neg / total, 1),
        "neutral_pct": round(100 * neu / total, 1),
        "net_sentiment": _net(c, total),          # Positive % minus Negative %, -100 to +100
        "mean_compound": round(float(df["compound"].mean()), 4),
        "by_source": by_source,
    }


def load_index(data_dir: Path, sector_name: str) -> list[dict]:
    path = data_dir / "history_index.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("runs", [])
    except (json.JSONDecodeError, AttributeError):
        log.warning("history_index.json was unreadable; rebuilding from CSVs.")
        return rebuild_index(data_dir, sector_name, write=False)


def write_index(data_dir: Path, runs: list[dict], sector_name: str) -> None:
    payload = {"sector": sector_name,
               "updated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "runs": sorted(runs, key=lambda r: r["run_date"])}
    tmp = data_dir / "history_index.json.tmp"
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(data_dir / "history_index.json")      # atomic replace


def rebuild_index(data_dir: Path, sector_name: str, write: bool = True) -> list[dict]:
    runs = []
    for csv in sorted(data_dir.glob("sentiment_*.csv")):
        m = re.match(r"sentiment_(\d{4}-\d{2}-\d{2})\.csv$", csv.name)
        if m:
            df = pd.read_csv(csv)
            runs.append({"run_date": m.group(1), "file": csv.name,
                         "generated_at_utc": df["collected_at_utc"].iloc[0] if len(df) else "",
                         "sector": sector_name,
                         "sources": sorted(df["source"].unique().tolist()) if len(df) else [],
                         **summarise(df)})
    if write:
        write_index(data_dir, runs, sector_name)
        log.info("Rebuilt history_index.json from %d CSV files", len(runs))
    return runs


def prune_youtube(data_dir: Path, today: str, retention_days: int, sector_name: str) -> int:
    """Remove YouTube rows from CSVs older than retention_days. Returns files changed."""
    limit = datetime.fromisoformat(today).date() - timedelta(days=retention_days)
    runs = load_index(data_dir, sector_name)
    changed = 0
    for csv in sorted(data_dir.glob("sentiment_*.csv")):
        m = re.match(r"sentiment_(\d{4}-\d{2}-\d{2})\.csv$", csv.name)
        if not m or datetime.fromisoformat(m.group(1)).date() > limit:
            continue
        df = pd.read_csv(csv, dtype={"item_id": str})
        if "source" not in df or not (df["source"] == "youtube").any():
            continue
        df[df["source"] != "youtube"].to_csv(csv, index=False, encoding="utf-8")
        for r in runs:
            if r.get("file") == csv.name:
                r["youtube_pruned"] = True
        changed += 1
    if changed:
        write_index(data_dir, runs, sector_name)
        log.info("Removed YouTube rows from %d CSV(s) older than %d days", changed, retention_days)
    return changed


def write_github_summary(entry: dict) -> None:
    """Adds a short table to the GitHub Actions run page when running in Actions."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### {entry['sector']}: run {entry['run_date']}", "",
             f"Total mentions: **{entry['total_mentions']}**, net sentiment: **{entry['net_sentiment']}**", "",
             "| Source | Mentions | Net |", "|---|---|---|"]
    for src, s in entry["by_source"].items():
        lines.append(f"| {SOURCE_LABELS.get(src, src)} | {s['mentions']} | {s['net_sentiment']} |")
    if entry["warnings"]:
        lines += ["", "**Warnings**", *[f"- {w}" for w in entry["warnings"]]]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def migrate_flat_layout(data_root: Path) -> None:
    """Moves runs saved by the single-sector version (data/sentiment_*.csv) into
    data/<sector>/, so earlier history keeps appearing in the dashboard."""
    index = data_root / "history_index.json"
    flat = sorted(data_root.glob("sentiment_*.csv")) + ([index] if index.exists() else [])
    if not flat:
        return
    name = DEFAULT_SECTOR["sector_name"]
    if index.exists():
        try:
            name = json.loads(index.read_text(encoding="utf-8")).get("sector", name)
        except json.JSONDecodeError:
            pass
    target = data_root / slugify(name)
    target.mkdir(parents=True, exist_ok=True)
    for f in flat:
        if (target / f.name).exists():
            log.warning("Not moving %s: %s already exists", f, target / f.name)
        else:
            f.replace(target / f.name)
    log.info("Moved %d earlier file(s) into %s/", len(flat), target)


def load_usage(data_root: Path) -> dict:
    path = data_root / "api_usage.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError:
        return {}


def save_usage(data_root: Path, usage: dict) -> None:
    (data_root / "api_usage.json").write_text(json.dumps(usage, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_sector(config: dict, data_root: Path, run_date: str, now_utc: datetime, lookback_days: int,
               demo: bool, usage: dict) -> int:
    """Collects, scores and saves one sector. Returns 0 on success, 2 if no source is set up."""
    sector_name = config["sector_name"]
    data_dir = data_root / config["slug"]
    data_dir.mkdir(parents=True, exist_ok=True)
    cutoff = now_utc - timedelta(days=lookback_days)
    matcher = Matcher(config)
    log.info("=== %s (%s) ===", sector_name, config["slug"])

    if demo:
        rows, warnings, sources = collect_demo(matcher, run_date), [], ["demo"]
    else:
        month = now_utc.astimezone(UK_TZ).strftime("%Y-%m")
        brave_used = usage.setdefault("brave_requests_by_month", {}).get(month, 0)
        results = {
            "bluesky": collect_bluesky(matcher, cutoff),
            "youtube": collect_youtube(matcher, cutoff),
            "reddit_brave": collect_reddit_brave(matcher, cutoff, budget=BRAVE_MONTHLY_LIMIT - brave_used),
        }
        usage["brave_requests_by_month"][month] = brave_used + results["reddit_brave"].requests
        log.info("Brave requests this month: %d of %d", usage["brave_requests_by_month"][month],
                 BRAVE_MONTHLY_LIMIT)
        sources = [name for name, r in results.items() if r.enabled]
        if not sources:
            log.error("No sources configured. Set at least one set of keys (see README).")
            return 2
        rows = [row for r in results.values() for row in r.rows]
        warnings = [w for r in results.values() for w in r.warnings]

    before = len(rows)
    rows = deduplicate(rows)
    log.info("Deduplication removed %d of %d rows", before - len(rows), before)

    df = score(rows)
    df["run_date"] = run_date                 # assignment also works for an empty run
    df["collected_at_utc"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    df = df.reindex(columns=CSV_COLUMNS).sort_values("compound", ascending=False)
    csv_name = f"sentiment_{run_date}.csv"
    df.to_csv(data_dir / csv_name, index=False, encoding="utf-8")

    entry = {"run_date": run_date, "file": csv_name,
             "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "sector": sector_name, "lookback_days": lookback_days,
             "sources": sources, "warnings": warnings[:50], **summarise(df)}
    runs = [r for r in load_index(data_dir, sector_name) if r.get("run_date") != run_date] + [entry]
    write_index(data_dir, runs, sector_name)
    if not demo and YOUTUBE_RETENTION_DAYS > 0:
        prune_youtube(data_dir, run_date, YOUTUBE_RETENTION_DAYS, sector_name)
    write_github_summary(entry)

    log.info("Saved %s/%s: %d mentions, net sentiment %s", data_dir, csv_name,
             entry["total_mentions"], entry["net_sentiment"])
    if entry["total_mentions"] == 0:
        log.warning("No matching mentions this run. The empty run is still logged.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=os.getenv("DATA_DIR", "data"))
    parser.add_argument("--sector", default=os.getenv("SECTOR", "all"),
                        help="'all' (every sector with include_in_schedule) or a file name from sectors/")
    parser.add_argument("--new-sector-name", default=os.getenv("NEW_SECTOR_NAME", ""))
    parser.add_argument("--new-brands", default=os.getenv("NEW_BRANDS", ""),
                        help="Comma-separated; '|' adds aliases, '*' marks UK-only names")
    parser.add_argument("--new-terms", default=os.getenv("NEW_TERMS", ""))
    parser.add_argument("--new-subreddits", default=os.getenv("NEW_SUBREDDITS", ""))
    parser.add_argument("--not-weekly", action="store_true",
                        default=os.getenv("ADD_TO_WEEKLY", "true").strip().lower() == "false",
                        help="Save the new sector without adding it to the Monday run")
    parser.add_argument("--lookback-days", type=int, default=int(os.getenv("LOOKBACK_DAYS", "7")))
    parser.add_argument("--run-date", help="Override the run date (YYYY-MM-DD), e.g. to back-fill demo data.")
    parser.add_argument("--demo", action="store_true", help="Generate synthetic data; no API calls.")
    parser.add_argument("--rebuild-index", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_root = Path(args.data_dir)
    data_root.mkdir(parents=True, exist_ok=True)
    migrate_flat_layout(data_root)
    sectors = load_sectors()

    requested = (args.sector or "all").strip()
    if args.new_sector_name.strip():
        raw = sector_from_form(args.new_sector_name, args.new_brands, args.new_terms,
                               args.new_subreddits, schedule=not args.not_weekly)
        try:
            slug = slugify(raw["sector_name"])
            sectors[slug] = expand_config(raw, slug)
        except ValueError as exc:
            log.error("New sector not saved: %s. Fill in Brands or Sector phrases.", exc)
            return 2
        save_sector(raw)
        log.info("New sector '%s': %d brands (%d UK-only), %d phrases, Reddit targets %s",
                 raw["sector_name"], len(raw["brands"]),
                 sum(b["uk_unique"] for b in raw["brands"].values()),
                 len(raw["sector_terms"]), ", ".join(raw["brave_target_subreddits"]))
        targets = [slug]
    elif requested.lower() == "all":
        targets = [k for k, c in sectors.items() if c.get("include_in_schedule", True)]
    else:
        slug = requested.removesuffix(".json")
        slug = slug if slug in sectors else slugify(slug)
        if slug not in sectors:
            log.error("Unknown sector '%s'. Available: %s", requested, ", ".join(sectors) or "none")
            return 2
        targets = [slug]
    if not targets:
        log.error("No sectors to run. Check include_in_schedule in your sectors/*.json files.")
        return 2

    if args.rebuild_index:
        for slug in targets:
            (data_root / slug).mkdir(parents=True, exist_ok=True)
            rebuild_index(data_root / slug, sectors[slug]["sector_name"])
        return 0

    now_utc = datetime.now(timezone.utc)
    run_date = args.run_date or now_utc.astimezone(UK_TZ).date().isoformat()
    usage = load_usage(data_root)
    status = 0
    for slug in targets:
        try:
            code = run_sector(sectors[slug], data_root, run_date, now_utc, args.lookback_days, args.demo, usage)
        except Exception:                       # one failing sector should not stop the others
            log.exception("Sector %s failed", slug)
            code = 1
        if code == 2:                           # no keys at all: no point trying other sectors
            return 2
        status = status or code
        if not args.demo:
            save_usage(data_root, usage)
    return status


if __name__ == "__main__":
    sys.exit(main())
