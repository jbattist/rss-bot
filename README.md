# rss-bot

A self-hosted RSS curation bot that sits on top of [FreshRSS](https://freshrss.org) and uses a local [Ollama](https://ollama.ai) LLM to filter, score, and learn from your reading behavior.

## What it does

Three loops run continuously:

- **Filter** — scores every new unread article 1-10 against your interest profile using Ollama. Articles below your threshold are marked read and disappear. Paywall domains and keyword blocklist are checked first (no LLM call wasted). Duplicate stories are detected by title similarity — the first source to publish wins, and each additional source that covers the same story increments a **heat counter** on the original (useful for future ranking). Articles that score in the **marginal zone** (threshold − 2 to threshold − 1) are saved to a `marginal.rss` feed so you can review near-misses and star anything you want more of.
- **Feedback** — starred articles boost topic weights; articles labeled `not-relevant` penalize them. The bot tunes itself over time.
- **Discovery** — periodically analyzes your recent kept articles and surfaces adjacent topics you might not be following yet, as a local RSS feed you can subscribe to in FreshRSS.

## Requirements

- FreshRSS instance with the GReader API enabled
- Ollama running locally or on your LAN
- Python 3.10+

## Setup

**1. Clone and install deps:**
```bash
git clone git@github.com:jbattist/rss-bot.git
cd rss-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**2. Create your config:**
```bash
cp config.example.yaml config.yaml
```
Edit `config.yaml` with your FreshRSS URL, credentials, and Ollama settings.

> **FreshRSS API password**: this is a *separate* password from your login. Set it in FreshRSS under User Profile → API password.

**3. Import feeds (optional):**
`feeds_validated.opml` contains a curated set of feeds across tech, infosec, homelab, science, and niche topics. Import it via FreshRSS → Subscription Management → Import.

**4. Create the `not-relevant` label in FreshRSS:**
Settings → Labels → add a label named exactly `not-relevant`. Apply it to articles you want the bot to learn from negatively.

**5. Subscribe to the bot feeds in FreshRSS:**
Once the bot is running, add these as subscriptions:
- `http://localhost:8765/suggestions.rss` — adjacent topic suggestions (star to add to profile)
- `http://localhost:8765/marginal.rss` — articles that almost made the cut (star to boost related topics)

## Running

```bash
# Run continuously (normal mode)
venv/bin/python bot.py

# Run all loops once and exit (good for testing)
venv/bin/python bot.py --run-once

# Print current interest profile weights
venv/bin/python bot.py --status
```

## Systemd service

```bash
# Edit rss-bot.service to match your path, then:
sudo cp rss-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rss-bot
journalctl -u rss-bot -f
```

## How the feedback loop works

| Action in FreshRSS | Effect |
|---|---|
| Star an article | Topics extracted from it get a weight boost |
| Apply `not-relevant` label | Topics extracted from it get a weight penalty |
| Star a `[SUGGESTION]` item | That topic is added to your profile |

Weights are stored in `profile.json` and evolve continuously. You can inspect them anytime with `--status`.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot — all logic |
| `config.yaml` | Your config (not committed — see `config.example.yaml`) |
| `config.example.yaml` | Template for config.yaml |
| `paywalls.txt` | Paywall domain blocklist |
| `feeds_validated.opml` | Curated, validated feed list |
| `validate_feeds.py` | Utility to check feed URLs and rewrite OPML |
| `rss-bot.service` | Systemd unit file |
| `profile.json` | Auto-generated evolving interest weights (not committed) |
| `rss-bot.db` | SQLite state (not committed) |
