#!/usr/bin/env python3
"""
Validate feed URLs in feeds.opml — check which ones actually respond with XML.
Prints a report and writes feeds_validated.opml with only working feeds.
Usage: venv/bin/python validate_feeds.py
"""

import xml.etree.ElementTree as ET
import concurrent.futures
import requests
import sys
from pathlib import Path

OPML_IN  = Path("feeds.opml")
OPML_OUT = Path("feeds_validated.opml")
TIMEOUT  = 10
WORKERS  = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; rss-bot feed validator)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}

def check_feed(outline: ET.Element) -> tuple[ET.Element, bool, str]:
    url = outline.get("xmlUrl", "")
    if not url:
        return outline, False, "no xmlUrl"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        ct = resp.headers.get("Content-Type", "")
        # Accept if HTTP 2xx and looks like XML or feed content
        if resp.status_code == 200 and (
            "xml" in ct or "rss" in ct or "atom" in ct
            or resp.text.lstrip().startswith("<")
        ):
            return outline, True, f"OK ({resp.status_code})"
        return outline, False, f"HTTP {resp.status_code} / Content-Type: {ct[:60]}"
    except requests.exceptions.Timeout:
        return outline, False, "timeout"
    except Exception as e:
        return outline, False, str(e)[:80]


def main():
    tree = ET.parse(OPML_IN)
    root = tree.getroot()

    # Collect all feed outlines and their parent category outlines
    tasks: list[tuple[ET.Element, ET.Element]] = []  # (feed_outline, category_outline)
    for category in root.find("body"):
        for feed in list(category):
            if feed.get("xmlUrl"):
                tasks.append((feed, category))

    print(f"Checking {len(tasks)} feeds with {WORKERS} workers…\n")

    ok_count = 0
    fail_count = 0
    failed: list[tuple[str, str, str]] = []

    # Check all feeds in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(check_feed, feed): (feed, cat) for feed, cat in tasks}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            outline, cat = futures[future]
            _, ok, msg = future.result()
            name = outline.get("title") or outline.get("text") or "?"
            url  = outline.get("xmlUrl", "")
            status = "OK  " if ok else "FAIL"
            print(f"[{i:3d}/{len(tasks)}] {status}  {name[:45]:<45}  {msg}")
            if ok:
                ok_count += 1
            else:
                fail_count += 1
                failed.append((cat.get("text", "?"), name, url))
                category = futures[future][1]
                category.remove(outline)

    # Write validated OPML (dead feeds removed)
    ET.indent(tree, space="  ")
    with open(OPML_OUT, "wb") as f:
        tree.write(f, xml_declaration=True, encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"  Working feeds:  {ok_count}")
    print(f"  Dead/failed:    {fail_count}")
    print(f"  Output:         {OPML_OUT}")
    print(f"{'='*60}")

    if failed:
        print("\nFailed feeds:")
        for cat, name, url in failed:
            print(f"  [{cat}] {name}  —  {url}")


if __name__ == "__main__":
    main()
