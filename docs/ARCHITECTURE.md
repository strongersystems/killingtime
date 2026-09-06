# Architecture

```
Warcraft Logs v2 (GraphQL) ─┐                 ┌─ FastAPI + Jinja2 + Chart.js  (killingtime/web)
                            ├─ kt sync ──► SQLite ─┤
Raider.IO REST ─────────────┘  (sync.py)   (db.py) └─ Ask: Claude + read-only SQL tools (ask.py)
```

| Module | Role |
|---|---|
| `config.py` | Settings from `.env` / environment (pydantic-settings). Parses `RIVAL_GUILDS`, `TIER_MAP`. |
| `wcl.py` | Warcraft Logs client: OAuth2 client-credentials token (cached on disk), GraphQL queries, batching of report fights via aliases, rate-limit awareness. |
| `raiderio.py` | Raider.IO client with polite pacing. |
| `db.py` | Schema, views (`v_pulls`, `v_first_kills`, `v_raid_nights`, `v_attendance`, `v_rio_progress`) and the schema description used by Ask. |
| `sync.py` | Orchestrates a sync: zones → home guild → reports (incremental) → fights → zone rankings → attendance → Raider.IO profiles, static data, zone↔raid mapping, realm leaderboard scan. |
| `metrics.py` | Derived numbers for pages and tools: tier summaries, timelines, tier comparison, rival comparison, realm standings, attendance. |
| `ask.py` | Claude tool loop (`get_overview`, `run_sql`, `render_chart`), SQL guard, chart spec validation. |
| `web/app.py` | Routes, background sync manager, JSON API. |
| `cli.py` | `kt check / sync / serve / ask / report`. |

## Data model highlights

- **Timestamps** are milliseconds since epoch everywhere (Warcraft Logs' native unit). Fight times are absolute
  (report start + fight offset).
- **Difficulty** uses Warcraft Logs codes: 3 Normal, 4 Heroic, 5 Mythic (1 LFR). Raider.IO strings are mapped.
- **Tiers** are Warcraft Logs zones. Each zone is linked to a Raider.IO raid slug by name matching (override with
  `TIER_MAP`), and each encounter to a Raider.IO boss slug, so our pull data and realm data line up.
- `v_first_kills.pulls_to_kill` counts pulls up to and including the first kill on that difficulty; for unkilled
  bosses it counts all pulls so far. Re-kills are never counted as progression.
- `guilds` holds every guild we have seen: `is_home` for Killing Time, `is_rival` for configured rivals, others
  from the realm leaderboard.

## Incremental sync

Reports are re-listed from three days before the newest known report so late uploads are caught. A report's fights
are re-fetched only when its `end_time` moved (still-uploading logs) or it has never been fetched. Realm standings are
replaced on every sync (they are cheap and change nightly).

## Warcraft Logs API points

Rough costs per sync: 1 query for expansions, 1 per expansion for zones, 1 per 100 reports listed, 1 per 8 reports for
fights, 1 per zone for rankings, 1 per 25 attendance rows. A guild with 300 reports and 2 expansions: ~50 requests /
roughly 300–600 points on first sync, single digits afterwards. Budget is 3,600 points per hour.

## Ask safety

The SQL tool opens the database with `mode=ro` and installs an authorizer that permits only `SQLITE_SELECT`,
`SQLITE_READ` and `SQLITE_FUNCTION`; anything else (writes, `PRAGMA`, `ATTACH`) is denied by SQLite itself. On top of
that: one statement per call, must start with `SELECT`/`WITH`, 200-row cap, 8-second progress-handler timeout, and a
per-question tool-call budget. Chart specs are validated with pydantic (type whitelist, ≤ 8 series, equal lengths).
