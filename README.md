# Killing Time · Raid Progress Tracker

A progress and reporting app for the guild **Killing Time** (Draenor, EU). It pulls your raid logs from
**Warcraft Logs**, realm standings from **Raider.IO**, stores everything in a local SQLite database, and gives you:

- **Dashboard** – current tier at a glance: bosses down, pulls, wipes, raid nights, best wipe on the next boss,
  progression timeline, pulls per boss, recent nights.
- **Tiers** – tier-over-tier comparison aligned by boss order and by days since first pull (cumulative pulls,
  days to each kill, pulls per boss, nights per tier).
- **Rivals & Realm** – head-to-head with configured rival guilds and the realm leaderboard: first-kill dates,
  pull counts, best percentages, a "progress race" chart, world/region/realm ranks.
- **Raid nights** and **Attendance** – per-night kills/wipes/hours and attendance % per raider.
- **Ask ✦** – type a question in plain English ("how many pulls did the last boss take compared to last tier?")
  and Claude answers from the database with tables and charts. Read-only by construction.
- A `kt` command line for syncing, checking configuration, printing text reports and asking questions.

Everything runs locally (or in one Docker container). No data leaves your machine except the API calls to
Warcraft Logs, Raider.IO and, for Ask, Anthropic.

## Quick start

```bash
git clone <this repo> && cd killingtime
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env                                     # then edit .env – see docs/SETUP.md
kt check                                                 # verifies WCL + Raider.IO + Anthropic settings
kt sync                                                  # first sync: a few minutes
kt serve                                                 # open http://127.0.0.1:8000
kt ask "where are we on the current tier and what took the most pulls?"
```

**The full step-by-step guide, including creating the Warcraft Logs API client, is in
[docs/SETUP.md](docs/SETUP.md).** Example questions and how Ask works: [docs/ASKING.md](docs/ASKING.md).
Hosting it for the whole guild: [docs/DEPLOY.md](docs/DEPLOY.md). How it fits together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

- Python 3.11+ (or Docker)
- A free Warcraft Logs API client (client id + secret) – for your own pull-level data
- Optional: an Anthropic API key – for the Ask page
- Raider.IO needs no key

## Commands

| Command | What it does |
|---|---|
| `kt check` | Validate `.env`, test the Warcraft Logs token, confirm the guild is found, ping Raider.IO |
| `kt sync` | Incremental sync (new reports since last time, refreshed realm standings) |
| `kt sync --full` | Re-list every report the guild has on Warcraft Logs |
| `kt sync --no-wcl` / `--no-rio` | Skip one source |
| `kt serve [--port 8000] [--sync-every 60]` | Run the web app, optionally syncing on a schedule |
| `kt report [--zone ID] [--difficulty 5]` | Text summary in the terminal |
| `kt ask "question" [--json] [-v]` | Ask Claude; `-v` prints the SQL it ran |

## Data sources and what they give you

| Source | Auth | Data |
|---|---|---|
| Warcraft Logs v2 API | client credentials | Every boss pull in your guild's reports: kill/wipe, %, duration, difficulty, item level; attendance; guild zone rankings |
| Raider.IO API | none | Per-boss first kills, pull counts and best % for **every guild on the realm** and configured rivals; world/region/realm ranks; raid/boss reference data |

Cross-guild comparisons come from Raider.IO on purpose: rivals don't need to publish their logs, and you don't
burn Warcraft Logs API points on other guilds.

## Development

```bash
pip install -e ".[dev]"
pytest -q          # 22 tests, no network needed
ruff check .
```

Tests run against fake API clients with a synthetic two-tier history (`tests/conftest.py`).
