#!/usr/bin/env python3
"""
collector.py — overnight newswire collector for the MYST Engine.

Runs on a schedule in GitHub Actions (see .github/workflows/collect.yml). Each run:
  1. fetches the four wire-wide RSS feeds (PR Newswire, GlobeNewswire, Business Wire,
     ACCESS Newswire),
  2. merges every item into items.json, de-duplicated by guid/link,
  3. drops anything older than RETAIN_HOURS,
  4. writes items.json + status.json (the workflow commits them).

It stores EVERYTHING the wires publish — no ticker list lives here. The Engine on
John's PC downloads items.json at startup / before the 8:00 brief and filters it
against the active universe locally.

No third-party dependencies: urllib + xml.etree only.
"""
import json
import os
import re
import sys
import time
import html as htmlmod
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# PR Newswire feeds return only ~20 items each, so its 16 industry feeds are
# polled alongside the all-releases feed (mostly disjoint → ~17x the coverage).
PRN_CATEGORIES = [
    "automotive-transportation", "business-technology", "consumer-products-retail", "consumer-technology",
    "energy", "entertainment-media", "environment", "financial-services", "general-business",
    "heavy-industry-manufacturing", "health", "multicultural", "policy-public-interest", "sports",
    "telecommunications", "travel",
]
FEEDS = [
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
] + [
    ("PR Newswire", f"https://www.prnewswire.com/rss/{c}-latest-news/{c}-latest-news-list.rss") for c in PRN_CATEGORIES
] + [
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/country/United%20States/feedTitle/GlobeNewswire%20-%20News%20from%20United%20States"),
    ("Business Wire", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRXg=="),
    ("ACCESS Newswire", "https://www.accesswire.com/rssfeed.aspx"),
]
# If a feed URL above fails, these are tried in order (status.json shows which one worked).
ALTERNATES = {
    "ACCESS Newswire": ["https://www.accessnewswire.com/rssfeed.aspx", "https://www.accessnewswire.com/newsroom/rss",
                        "https://www.accesswire.com/newsroom/rss", "https://www.accessnewswire.com/rss/latest"],
}
RETAIN_HOURS = int(os.environ.get("RETAIN_HOURS", "36"))
SUMMARY_CHARS = 600
ITEMS_FILE = "items.json"
STATUS_FILE = "status.json"
# Some wires (GlobeNewswire, ACCESS) answer 403 to anything that doesn't look
# like a browser, so send browser-like headers.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = htmlmod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def to_iso(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def local(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def first(el, names):
    for child in el:
        if local(child.tag) in names:
            return child
    return None


def text(el):
    return (el.text or "").strip() if el is not None else ""


def parse_feed(xml_bytes, source):
    root = ET.fromstring(xml_bytes)
    out = []
    items = [e for e in root.iter() if local(e.tag) == "item"]
    entries = [] if items else [e for e in root.iter() if local(e.tag) == "entry"]
    for it in items:
        title = text(first(it, {"title"}))
        link = text(first(it, {"link"}))
        guid = text(first(it, {"guid"})) or link or title
        pub = text(first(it, {"pubDate", "date", "published", "updated"}))
        desc = text(first(it, {"description", "encoded", "summary"}))
        out.append({"guid": guid, "title": title, "link": link, "publishedAt": to_iso(pub),
                    "summary": strip_html(desc)[:SUMMARY_CHARS], "source": source})
    for en in entries:
        title = text(first(en, {"title"}))
        link = ""
        for child in en:
            if local(child.tag) == "link":
                link = child.get("href") or text(child)
                if child.get("rel") in (None, "alternate"):
                    break
        guid = text(first(en, {"id"})) or link or title
        pub = text(first(en, {"published", "updated", "issued"}))
        desc = text(first(en, {"summary", "content"}))
        out.append({"guid": guid, "title": title, "link": link, "publishedAt": to_iso(pub),
                    "summary": strip_html(desc)[:SUMMARY_CHARS], "source": source})
    return [x for x in out if x["title"] or x["link"]]


def load_items():
    try:
        with open(ITEMS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("items", []) if isinstance(d, dict) else d
    except Exception:
        return []


def main():
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = (now - timedelta(hours=RETAIN_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = load_items()
    by_key = {}
    for it in existing:
        k = it.get("guid") or it.get("link") or it.get("title")
        if k:
            by_key[k] = it

    status = {"ranAt": now_iso, "feeds": [], "retainHours": RETAIN_HOURS}
    new_total = 0
    for name, url in FEEDS:
        row = {"name": name, "url": url, "status": "ok", "items": 0, "new": 0, "error": None}
        urls = [url] + ALTERNATES.get(name, [])
        parsed = None
        for u in urls:
            try:
                parsed = parse_feed(fetch(u), name)
                row["url"] = u
                break
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
        if parsed is None:
            row["status"] = "error"
            status["feeds"].append(row)
            print(f"[{name}] ERROR {row['error']}", file=sys.stderr)
            continue
        row["error"] = None
        row["items"] = len(parsed)
        for it in parsed:
            k = it["guid"] or it["link"] or it["title"]
            if not k:
                continue
            if k not in by_key:
                it["seenAt"] = now_iso
                if not it["publishedAt"]:
                    it["publishedAt"] = now_iso
                by_key[k] = it
                row["new"] += 1
        new_total += row["new"]
        status["feeds"].append(row)
        print(f"[{name}] {row['items']} items, {row['new']} new")

    # Roll the 17 PR Newswire rows up to one line per wire in the status summary.
    rollup = {}
    for r in status["feeds"]:
        g = rollup.setdefault(r["name"], {"name": r["name"], "feeds": 0, "ok": 0, "items": 0, "new": 0, "lastError": None})
        g["feeds"] += 1
        if r["status"] == "ok":
            g["ok"] += 1
        else:
            g["lastError"] = r["error"]
        g["items"] += r["items"]
        g["new"] += r["new"]
    status["byWire"] = list(rollup.values())

    kept = [it for it in by_key.values() if (it.get("publishedAt") or it.get("seenAt") or now_iso) >= cutoff]
    kept.sort(key=lambda x: x.get("publishedAt") or "", reverse=True)
    status["itemsStored"] = len(kept)
    status["newThisRun"] = new_total
    status["newest"] = kept[0]["publishedAt"] if kept else None
    status["oldest"] = kept[-1]["publishedAt"] if kept else None

    with open(ITEMS_FILE, "w", encoding="utf-8") as f:
        json.dump({"updatedAt": now_iso, "retainHours": RETAIN_HOURS, "count": len(kept), "items": kept}, f, ensure_ascii=False, separators=(",", ":"))
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    print(f"stored {len(kept)} items ({new_total} new this run); newest {status['newest']}")
    # Non-zero exit only if EVERY feed failed, so one broken wire never stops the others.
    if all(r["status"] == "error" for r in status["feeds"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
