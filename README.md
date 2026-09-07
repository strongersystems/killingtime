# Killing Time · Raid Progress Tracker

A progress and reporting app for the guild **Killing Time** (Draenor, EU). It pulls your raid logs from
**Warcraft Logs**, realm standings from **Raider.IO**, stores everything in a local SQLite database, and gives you:

- **Team first** – the app opens with "which team?" (`RAID_TEAMS`, e.g. a CE team and a 6-hour team; reports are
  attributed from attendance) and remembers the answer. Everything under a team - Progress, Roster, Nights, History,
  Peers, Realm - shows that team's logs only; "whole guild" is one more option.
- **Progress** – the current tier by default with a tier and difficulty switcher: bosses down, next boss and best pull,
  pulls, nights, median parse, and a boss-by-boss table with the peer numbers inline.
- **Season-accurate progress** – a tier counts as it stood at the season cut-off (when Cutting Edge / Ahead of the
  Curve stop and the Mythic+ season ends). Kills after it are marked post-season and excluded from the tier's
  progress, and each closed tier shows whether CE / AOTC was earned.
- **History** – tier-over-tier comparison aligned by boss order and by days since first pull (cumulative pulls,
  days to each kill, pulls per boss, nights per tier).
- **Peers** – "guilds around our level" for this and last tier: our pulls per boss vs the average, median and
  quartiles of guilds at a similar kill count (not the top of the server), the percentile of peers we out-pulled,
  and our days-into-tier at each kill vs the typical pace.
- **Rivals & Realm** – head-to-head with configured rival guilds and the realm leaderboard: first-kill dates,
  pull counts, best percentages, a "progress race" chart, world/region/realm ranks.
- **Roster** – attendance % and Warcraft Logs parses (average/median/best percentile) per raider in one table, plus
  parses per boss and per raid night; other-realm pugs hidden by default.
- **Boss** – the pull-by-pull view for the boss you are actually working on: every pull with how much of the boss
  was left, the running best percentage, phase, duration and a link to that pull on Warcraft Logs, plus per-night
  totals and the progression chart.
- **Meet the team** – a card per raider built from their own numbers: a mildly comical bio, attendance, parses,
  Mythic+, raid history and alts, their live transmog and weapons, and five image prompts (hero shot first) whose
  generated frames animate on hover.
- **Nights** – per-night kills, wipes and hours in combat.
- **Public site** – a self-contained landing page (`/public`, served at `killingtime.fyi` on Cloudflare) with links
  to apply / Discord / Raider.IO / Warcraft Logs and the latest progress, per-team standings and recent kills.
  It is re-rendered and published after every sync.
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
| Warcraft Logs v2 API | client credentials | Every boss pull in your guild's reports: kill/wipe, %, duration, difficulty, item level; attendance; guild zone rankings; per-player parses on kills |
| Raider.IO API | none | Per-boss first kills, pull counts and best % for **every guild on the realm** and configured rivals; world/region/realm ranks; raid/boss reference data |

Cross-guild comparisons come from Raider.IO on purpose: rivals don't need to publish their logs, and you don't
burn Warcraft Logs API points on other guilds. Raider.IO is also the record of *what the guild killed*: a boss counts
as killed if either source says so, because a raid night logged personally (or not uploaded at all) never reaches the
guild's Warcraft Logs reports. Such kills are shown as "no log" and have no pull count.

## Development

```bash
pip install -e ".[dev]"
pytest -q          # 48 tests, no network needed
ruff check .
```

Tests run against fake API clients with a synthetic two-tier history (`tests/conftest.py`).
