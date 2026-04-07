#!/usr/bin/env python3
"""
rss-bot: FreshRSS curation bot

Three loops:
  FILTER    (every N min)  - paywall check, keyword blocklist, Ollama relevance score
  FEEDBACK  (every N min)  - starred articles boost topic weights; "not-relevant" label penalizes
  DISCOVERY (every N hrs)  - surfaces adjacent topics as a local RSS feed you subscribe to

Usage:
  python bot.py              # run continuously
  python bot.py --run-once   # run all loops once and exit (good for cron / testing)
  python bot.py --status     # print current interest profile weights and exit
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import requests
import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rss-bot")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
PROFILE_PATH = BASE_DIR / "profile.json"
PAYWALLS_PATH = BASE_DIR / "paywalls.txt"
DB_PATH = BASE_DIR / "rss-bot.db"
SUGGESTIONS_PATH = BASE_DIR / "suggestions.rss"

# ---------------------------------------------------------------------------
# Config & Profile
# ---------------------------------------------------------------------------


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_profile() -> dict:
    if PROFILE_PATH.exists():
        with open(PROFILE_PATH) as f:
            return json.load(f)
    # Bootstrap from config on first run
    cfg = load_config()
    profile = {
        "topic_weights": {
            t["name"]: float(t.get("weight", 1.0))
            for t in cfg["interests"]["topics"]
        },
        "suppressed_topics": [],
        "last_feedback_run": None,
        "last_discovery_run": None,
    }
    save_profile(profile)
    log.info("Bootstrapped profile.json from config.yaml")
    return profile


def save_profile(profile: dict):
    with open(PROFILE_PATH, "w") as f:
        json.dump(profile, f, indent=2)


def load_paywalls() -> set[str]:
    if not PAYWALLS_PATH.exists():
        return set()
    domains = set()
    with open(PAYWALLS_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                domains.add(line.lower())
    return domains


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS processed_items (
            item_id  TEXT NOT NULL,
            loop     TEXT NOT NULL,
            action   TEXT NOT NULL,
            score    INTEGER,
            ts       TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (item_id, loop)
        );

        CREATE TABLE IF NOT EXISTS kept_articles (
            item_id  TEXT PRIMARY KEY,
            title    TEXT,
            summary  TEXT,
            url      TEXT,
            score    INTEGER,
            ts       TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS feedback_items (
            item_id  TEXT PRIMARY KEY,
            signal   TEXT,
            topics   TEXT,
            ts       TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_processed(item_id: str, loop: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_items WHERE item_id = ? AND loop = ?",
            (item_id, loop),
        ).fetchone()
    return row is not None


def mark_processed_batch(records: list[tuple]):
    """records: list of (item_id, loop, action, score_or_None)"""
    with _db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO processed_items (item_id, loop, action, score) VALUES (?, ?, ?, ?)",
            records,
        )
        conn.commit()


def save_kept_article(item_id: str, title: str, summary: str, url: str, score: int):
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO kept_articles (item_id, title, summary, url, score) VALUES (?, ?, ?, ?, ?)",
            (item_id, title, summary, url, score),
        )
        conn.commit()


def get_recent_kept_articles(days: int = 7, limit: int = 50) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        rows = conn.execute(
            "SELECT title, summary, url FROM kept_articles WHERE ts > ? ORDER BY score DESC LIMIT ?",
            (since, limit),
        ).fetchall()
    return [{"title": r["title"], "summary": r["summary"], "url": r["url"]} for r in rows]


def is_feedback_processed(item_id: str) -> bool:
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM feedback_items WHERE item_id = ?", (item_id,)
        ).fetchone()
    return row is not None


def save_feedback_item(item_id: str, signal: str, topics: list):
    with _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO feedback_items (item_id, signal, topics) VALUES (?, ?, ?)",
            (item_id, signal, json.dumps(topics)),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# FreshRSS GReader API Client
# ---------------------------------------------------------------------------


class FreshRSSClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.api = base_url.rstrip("/") + "/api/greader.php"
        self.username = username
        self.password = password
        self._auth_token: Optional[str] = None
        self._post_token: Optional[str] = None
        self._session = requests.Session()

    # --- Auth ---

    def auth(self):
        resp = self._session.post(
            f"{self.api}/accounts/ClientLogin",
            data={"Email": self.username, "Passwd": self.password},
            timeout=15,
        )
        resp.raise_for_status()
        for line in resp.text.strip().splitlines():
            if line.startswith("Auth="):
                self._auth_token = line[5:]
        if not self._auth_token:
            raise RuntimeError("FreshRSS auth failed — check credentials in config.yaml")
        self._session.headers.update({"Authorization": f"GoogleLogin auth={self._auth_token}"})
        self._post_token = None  # invalidate; will fetch lazily
        log.info("Authenticated with FreshRSS")

    def _ensure_auth(self):
        if not self._auth_token:
            self.auth()

    def _post_token_header(self) -> str:
        if not self._post_token:
            self._ensure_auth()
            resp = self._session.get(f"{self.api}/reader/api/0/token", timeout=10)
            resp.raise_for_status()
            self._post_token = resp.text.strip()
        return self._post_token

    # --- HTTP helpers with auto-reauth ---

    def _get(self, path: str, params: dict = None) -> dict:
        self._ensure_auth()
        resp = self._session.get(f"{self.api}{path}", params=params, timeout=30)
        if resp.status_code == 401:
            self.auth()
            resp = self._session.get(f"{self.api}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict):
        self._ensure_auth()
        data["T"] = self._post_token_header()
        resp = self._session.post(f"{self.api}{path}", data=data, timeout=30)
        if resp.status_code == 401:
            self.auth()
            data["T"] = self._post_token_header()
            resp = self._session.post(f"{self.api}{path}", data=data, timeout=30)
        resp.raise_for_status()

    # --- API methods ---

    def get_unread_items(self, count: int = 100, continuation: str = None) -> dict:
        params = {"xt": "user/-/state/com.google/read", "n": count, "output": "json"}
        if continuation:
            params["c"] = continuation
        return self._get(
            "/reader/api/0/stream/contents/user/-/state/com.google/reading-list", params
        )

    def get_starred_items(self, count: int = 50) -> dict:
        return self._get(
            "/reader/api/0/stream/contents/user/-/state/com.google/starred",
            {"n": count, "output": "json"},
        )

    def get_labeled_items(self, label: str, count: int = 50) -> dict:
        return self._get(
            f"/reader/api/0/stream/contents/user/-/label/{label}",
            {"n": count, "output": "json"},
        )

    def mark_as_read(self, item_ids: list[str]):
        """Batch mark items as read. Sends in groups of 20."""
        if not item_ids:
            return
        for i in range(0, len(item_ids), 20):
            batch = item_ids[i : i + 20]
            self._post(
                "/reader/api/0/edit-tag",
                {"a": "user/-/state/com.google/read", "i": batch},
            )


# ---------------------------------------------------------------------------
# Ollama Client
# ---------------------------------------------------------------------------


class OllamaClient:
    def __init__(self, base_url: str, model: str):
        self.url = base_url.rstrip("/") + "/api/generate"
        self.model = model
        self._session = requests.Session()

    def _generate(self, prompt: str, temperature: float = 0.1) -> str:
        resp = self._session.post(
            self.url,
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temperature},
            },
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["response"].strip()

    def score_relevance(
        self,
        title: str,
        summary: str,
        profile_text: str,
        topic_weights: dict,
        threshold: int,
    ) -> int:
        weighted = "\n".join(
            f"  - {t} (importance: {w:.1f})"
            for t, w in sorted(topic_weights.items(), key=lambda x: -x[1])
        )
        prompt = (
            "You are a relevance scorer for a personal news reader.\n\n"
            f"User interest profile:\n{profile_text}\n\n"
            f"Weighted topics (higher = more important):\n{weighted}\n\n"
            f"Article title: {title}\n"
            f"Article summary: {summary[:600] if summary else '(none)'}\n\n"
            "Rate relevance 1-10:\n"
            "  1-3 = irrelevant, user would not want to see this\n"
            "  4-6 = marginal\n"
            "  7-10 = highly relevant, user would want to read this\n\n"
            "Reply with a SINGLE INTEGER from 1 to 10. No explanation."
        )
        try:
            result = self._generate(prompt)
            match = re.search(r"\b(10|[1-9])\b", result)
            return int(match.group(1)) if match else threshold + 1  # fail open
        except Exception as e:
            log.warning(f"Ollama scoring failed ({e}) — keeping article")
            return threshold + 1  # fail open: keep the article

    def extract_topics(self, title: str, content: str) -> list[str]:
        prompt = (
            "Extract 3-5 specific topics or themes from this article.\n"
            "Be specific (e.g. 'RISC-V firmware' not 'technology').\n\n"
            f"Title: {title}\n"
            f"Content: {content[:800] if content else '(none)'}\n\n"
            'Reply with ONLY a JSON array of strings. Example: ["ZFS snapshots", "NixOS"]\n'
            "JSON:"
        )
        try:
            result = self._generate(prompt, temperature=0.3)
            match = re.search(r"\[.*?\]", result, re.DOTALL)
            if match:
                return json.loads(match.group(0))
        except Exception as e:
            log.warning(f"Topic extraction failed: {e}")
        return []

    def find_adjacent_topics(
        self, current_topics: list[str], recent_articles: list[dict]
    ) -> list[dict]:
        articles_text = "\n".join(
            f"- {a['title']}: {(a['summary'] or '')[:150]}"
            for a in recent_articles[:30]
        )
        current_text = ", ".join(current_topics[:40])
        prompt = (
            "Analyze the articles a user found relevant and identify 3-5 adjacent or emerging\n"
            "topics they might enjoy that are NOT already in their interest list.\n\n"
            f"User's current topics: {current_text}\n\n"
            f"Recent articles the user found relevant:\n{articles_text}\n\n"
            "Find topics that naturally extend from what they're already reading. Be specific.\n\n"
            "Reply with ONLY a JSON array:\n"
            '[{"topic": "...", "reason": "...", "example": "article title that led here"}]\n'
            "JSON:"
        )
        try:
            result = self._generate(prompt, temperature=0.5)
            match = re.search(r"\[.*?\]", result, re.DOTALL)
            if match:
                return json.loads(match.group(0))
        except Exception as e:
            log.warning(f"Adjacent topic discovery failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Suggestions Feed Server
# ---------------------------------------------------------------------------


class SuggestionsFeedServer:
    def __init__(self, port: int):
        self.port = port
        self._server: Optional[HTTPServer] = None

    def write_feed(self, suggestions: list[dict]):
        rss = ET.Element("rss")
        rss.set("version", "2.0")
        ch = ET.SubElement(rss, "channel")
        ET.SubElement(ch, "title").text = "RSS Bot: Topic Suggestions"
        ET.SubElement(ch, "link").text = f"http://127.0.0.1:{self.port}/suggestions.rss"
        ET.SubElement(ch, "description").text = (
            "Adjacent topics found by rss-bot. "
            "Star an item to add that topic to your interest profile."
        )
        ET.SubElement(ch, "lastBuildDate").text = _rfc822_now()

        for s in suggestions:
            topic = s.get("topic", "Unknown topic")
            reason = s.get("reason", "")
            example = s.get("example", "")
            item = ET.SubElement(ch, "item")
            ET.SubElement(item, "title").text = f"[SUGGESTION] {topic}"
            desc = f"<b>Suggested topic:</b> {topic}<br/><br/>"
            if reason:
                desc += f"<b>Why:</b> {reason}<br/><br/>"
            if example:
                desc += f"<b>Spotted in:</b> {example}<br/><br/>"
            desc += "<i>Star this article in FreshRSS to add this topic to your profile.</i>"
            ET.SubElement(item, "description").text = desc
            slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
            ET.SubElement(item, "guid").text = (
                f"rss-bot-suggestion-{slug}-{datetime.now().strftime('%Y%m%d')}"
            )
            ET.SubElement(item, "pubDate").text = _rfc822_now()
            ET.SubElement(item, "category").text = "rss-bot-suggestion"

        tree = ET.ElementTree(rss)
        ET.indent(tree, space="  ")
        with open(SUGGESTIONS_PATH, "wb") as f:
            tree.write(f, xml_declaration=True, encoding="utf-8")
        log.info(f"Suggestions feed updated ({len(suggestions)} items) → {SUGGESTIONS_PATH}")

    def start(self):
        feed_path = SUGGESTIONS_PATH  # capture for closure

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/suggestions.rss":
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    data = feed_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except FileNotFoundError:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass  # suppress request logs

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        log.info(
            f"Suggestions feed server listening on "
            f"http://127.0.0.1:{self.port}/suggestions.rss"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rfc822_now() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _extract_domain(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"https?://(?:www\.)?([^/?#]+)", url)
    return m.group(1).lower() if m else None


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").strip()


def _item_url(item: dict) -> str:
    for key in ("canonical", "alternate"):
        refs = item.get(key) or []
        if refs:
            return refs[0].get("href", "")
    return ""


def _published_days_ago(item: dict) -> float:
    ts = item.get("published") or 0
    return (time.time() - ts) / 86400


# ---------------------------------------------------------------------------
# Filter Loop
# ---------------------------------------------------------------------------


def run_filter_loop(
    freshrss: FreshRSSClient,
    ollama: OllamaClient,
    config: dict,
    profile: dict,
    paywalls: set[str],
):
    log.info("--- Filter loop start ---")
    cfg_f = config.get("filter", {})
    blocklist = [kw.lower() for kw in cfg_f.get("keyword_blocklist", [])]
    threshold = config["ollama"]["relevance_threshold"]
    max_items = cfg_f.get("max_items_per_run", 50)
    max_age_days = cfg_f.get("skip_older_than_days", 7)
    profile_text = config["interests"]["profile"]

    to_mark_read: list[str] = []   # ids to mark read in FreshRSS
    db_records: list[tuple] = []   # (item_id, loop, action, score)
    kept_records: list[tuple] = [] # for kept_articles table
    processed = 0
    kept = 0
    filtered = 0

    try:
        continuation = None
        while processed < max_items:
            data = freshrss.get_unread_items(
                count=min(100, max_items - processed), continuation=continuation
            )
            items = data.get("items") or []
            if not items:
                break

            for item in items:
                if processed >= max_items:
                    break

                item_id = item.get("id", "")
                if not item_id or is_processed(item_id, "filter"):
                    continue

                # Skip stale articles on startup / large backlog
                if _published_days_ago(item) > max_age_days:
                    db_records.append((item_id, "filter", "too_old", None))
                    to_mark_read.append(item_id)
                    filtered += 1
                    processed += 1
                    continue

                title = item.get("title") or ""
                summary = _strip_html((item.get("summary") or {}).get("content") or "")
                url = _item_url(item)
                domain = _extract_domain(url)

                # 1. Paywall check
                if domain and domain in paywalls:
                    log.info(f"PAYWALL  {title[:70]}")
                    db_records.append((item_id, "filter", "paywall", None))
                    to_mark_read.append(item_id)
                    filtered += 1
                    processed += 1
                    continue

                # 2. Keyword blocklist
                text_lower = (title + " " + summary).lower()
                hit = next((kw for kw in blocklist if kw in text_lower), None)
                if hit:
                    log.info(f"BLOCKED  [{hit}] {title[:65]}")
                    db_records.append((item_id, "filter", "keyword", None))
                    to_mark_read.append(item_id)
                    filtered += 1
                    processed += 1
                    continue

                # 3. Ollama relevance score
                score = ollama.score_relevance(
                    title, summary, profile_text, profile["topic_weights"], threshold
                )
                processed += 1

                if score < threshold:
                    log.info(f"SCORE {score:2d}/10  SKIP  {title[:65]}")
                    db_records.append((item_id, "filter", "low_score", score))
                    to_mark_read.append(item_id)
                    filtered += 1
                else:
                    log.info(f"SCORE {score:2d}/10  KEEP  {title[:65]}")
                    db_records.append((item_id, "filter", "kept", score))
                    kept_records.append((item_id, title, summary[:500], url, score))
                    kept += 1

            continuation = data.get("continuation")
            if not continuation:
                break

        # Commit to FreshRSS first, then DB
        if to_mark_read:
            freshrss.mark_as_read(to_mark_read)

        if db_records:
            mark_processed_batch(db_records)

        for rec in kept_records:
            save_kept_article(*rec)

        log.info(
            f"--- Filter loop done: {kept} kept, {filtered} filtered "
            f"({len(to_mark_read)} marked read) ---"
        )

    except Exception:
        log.exception("Filter loop error")


# ---------------------------------------------------------------------------
# Feedback Loop
# ---------------------------------------------------------------------------


def run_feedback_loop(
    freshrss: FreshRSSClient,
    ollama: OllamaClient,
    config: dict,
    profile: dict,
):
    log.info("--- Feedback loop start ---")
    label = config["freshrss"].get("not_relevant_label", "not-relevant")
    fb = config.get("feedback", {})
    boost = float(fb.get("star_weight_boost", 0.2))
    penalty = float(fb.get("not_relevant_weight_penalty", 0.15))
    max_w = float(fb.get("max_topic_weight", 3.0))
    min_w = float(fb.get("min_topic_weight", 0.1))
    changed = False

    def process_items(items: list, signal: str, weight_delta: float):
        nonlocal changed
        for item in items:
            item_id = item.get("id", "")
            if not item_id or is_feedback_processed(item_id):
                continue
            title = item.get("title") or ""
            content = _strip_html((item.get("summary") or {}).get("content") or "")

            # Suggestions from the discovery feed — handle directly without Ollama
            if title.startswith("[SUGGESTION]"):
                topic = title.replace("[SUGGESTION]", "").strip()
                if signal == "star":
                    _adjust_weight(profile, topic, boost * 2, max_w, min_w, add_if_missing=True)
                    log.info(f"SUGGESTION ACCEPTED: {topic}")
                save_feedback_item(item_id, signal, [topic])
                changed = True
                continue

            topics = ollama.extract_topics(title, content)
            if topics:
                action = "STARRED" if signal == "star" else "NOT-RELEVANT"
                log.info(f"{action}: {title[:60]}  →  {topics}")
                for topic in topics:
                    _adjust_weight(
                        profile, topic, weight_delta, max_w, min_w,
                        add_if_missing=(signal == "star"),
                    )
                save_feedback_item(item_id, signal, topics)
                changed = True
            else:
                save_feedback_item(item_id, signal, [])

    try:
        starred = freshrss.get_starred_items(count=50)
        process_items(starred.get("items") or [], "star", boost)
    except Exception as e:
        log.warning(f"Starred items fetch failed: {e}")

    try:
        labeled = freshrss.get_labeled_items(label, count=50)
        process_items(labeled.get("items") or [], "not_relevant", -penalty)
    except Exception as e:
        log.warning(f"Labeled items fetch failed: {e} (label '{label}' may not exist yet)")

    if changed:
        profile["last_feedback_run"] = datetime.now().isoformat()
        save_profile(profile)
        log.info("--- Feedback loop done: profile updated ---")
    else:
        log.info("--- Feedback loop done: no new signals ---")


def _adjust_weight(
    profile: dict,
    topic: str,
    delta: float,
    max_w: float,
    min_w: float,
    add_if_missing: bool = False,
):
    weights = profile["topic_weights"]
    # Exact match
    if topic in weights:
        weights[topic] = max(min_w, min(max_w, weights[topic] + delta))
        return
    # Case-insensitive match
    lower_map = {k.lower(): k for k in weights}
    if topic.lower() in lower_map:
        key = lower_map[topic.lower()]
        weights[key] = max(min_w, min(max_w, weights[key] + delta))
        return
    # New topic via positive feedback
    if add_if_missing and delta > 0:
        weights[topic] = max(min_w, min(max_w, 1.0 + delta))
        log.info(f"New topic added to profile: {topic!r} (weight {weights[topic]:.2f})")


# ---------------------------------------------------------------------------
# Discovery Loop
# ---------------------------------------------------------------------------


def run_discovery_loop(
    ollama: OllamaClient,
    profile: dict,
    suggestions_server: SuggestionsFeedServer,
):
    log.info("--- Discovery loop start ---")
    recent = get_recent_kept_articles(days=7, limit=50)
    if len(recent) < 5:
        log.info("Not enough recent kept articles for discovery (need 5+), skipping")
        return

    current_topics = list(profile["topic_weights"].keys())
    suggestions = ollama.find_adjacent_topics(current_topics, recent)

    if suggestions:
        log.info(
            f"Adjacent topics found: {[s.get('topic') for s in suggestions]}"
        )
        suggestions_server.write_feed(suggestions)
    else:
        log.info("No adjacent topics found this run")

    profile["last_discovery_run"] = datetime.now().isoformat()
    save_profile(profile)
    log.info("--- Discovery loop done ---")


# ---------------------------------------------------------------------------
# Status / CLI
# ---------------------------------------------------------------------------


def show_status():
    profile = load_profile()
    weights = profile.get("topic_weights", {})
    suppressed = profile.get("suppressed_topics", [])

    print("\n=== rss-bot interest profile ===\n")
    print("Topic weights (sorted by weight):")
    for topic, w in sorted(weights.items(), key=lambda x: -x[1]):
        bar = "█" * int(w * 5)
        print(f"  {w:5.2f}  {bar:<20}  {topic}")

    if suppressed:
        print(f"\nSuppressed topics: {', '.join(suppressed)}")

    print(f"\nLast feedback run:  {profile.get('last_feedback_run') or 'never'}")
    print(f"Last discovery run: {profile.get('last_discovery_run') or 'never'}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="rss-bot: FreshRSS curation bot")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run all loops once and exit (useful for cron or testing)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current interest profile and exit",
    )
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    log.info("rss-bot starting")
    init_db()

    config = load_config()
    profile = load_profile()
    paywalls = load_paywalls()

    freshrss = FreshRSSClient(
        config["freshrss"]["url"],
        config["freshrss"]["username"],
        config["freshrss"]["password"],
    )
    freshrss.auth()

    ollama = OllamaClient(
        config["ollama"]["url"],
        config["ollama"]["model"],
    )

    port = config.get("suggestions_feed_port", 8765)
    suggestions_server = SuggestionsFeedServer(port)
    if not SUGGESTIONS_PATH.exists():
        suggestions_server.write_feed([])
    suggestions_server.start()

    if args.run_once:
        log.info("--run-once: running all loops once")
        profile = load_profile()
        run_filter_loop(freshrss, ollama, config, profile, paywalls)
        profile = load_profile()
        run_feedback_loop(freshrss, ollama, config, profile)
        profile = load_profile()
        run_discovery_loop(ollama, profile, suggestions_server)
        log.info("--run-once complete")
        return

    # Continuous mode
    intervals = config.get("intervals", {})
    filter_secs = intervals.get("filter_minutes", 5) * 60
    feedback_secs = intervals.get("feedback_minutes", 60) * 60
    discovery_secs = intervals.get("discovery_hours", 24) * 3600

    log.info(
        f"Intervals — filter: {filter_secs//60}m  "
        f"feedback: {feedback_secs//60}m  "
        f"discovery: {discovery_secs//3600}h"
    )

    last_feedback = 0.0
    last_discovery = 0.0

    while True:
        now = time.time()

        # Reload config + profile + paywalls each iteration so live edits take effect
        config = load_config()
        profile = load_profile()
        paywalls = load_paywalls()

        run_filter_loop(freshrss, ollama, config, profile, paywalls)

        if now - last_feedback >= feedback_secs:
            profile = load_profile()
            run_feedback_loop(freshrss, ollama, config, profile)
            last_feedback = time.time()

        if now - last_discovery >= discovery_secs:
            profile = load_profile()
            run_discovery_loop(ollama, profile, suggestions_server)
            last_discovery = time.time()

        log.info(f"Sleeping {filter_secs // 60}m …")
        time.sleep(filter_secs)


if __name__ == "__main__":
    main()
