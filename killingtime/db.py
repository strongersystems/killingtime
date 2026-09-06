"""SQLite storage: schema, connection helpers and derived views.

Conventions
-----------
* All timestamps are **milliseconds since the Unix epoch** (Warcraft Logs native format).
  Convert in SQL with ``datetime(ts/1000, 'unixepoch')``.
* Difficulty codes follow Warcraft Logs: 1 = LFR, 3 = Normal, 4 = Heroic, 5 = Mythic.
* ``fight_pct`` is the percentage of the *encounter* remaining on a wipe (lower = closer to a kill).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

DIFFICULTIES: dict[int, str] = {1: "LFR", 3: "Normal", 4: "Heroic", 5: "Mythic"}
RIO_DIFFICULTY_TO_CODE: dict[str, int] = {"normal": 3, "heroic": 4, "mythic": 5}
CODE_TO_RIO_DIFFICULTY: dict[int, str] = {v: k for k, v in RIO_DIFFICULTY_TO_CODE.items()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS expansions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

-- A "tier" = a Warcraft Logs raid zone.
CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    expansion_id INTEGER REFERENCES expansions(id),
    frozen INTEGER NOT NULL DEFAULT 0,
    rio_raid_slug TEXT
);

CREATE TABLE IF NOT EXISTS encounters (
    id INTEGER PRIMARY KEY,
    zone_id INTEGER NOT NULL REFERENCES zones(id),
    name TEXT NOT NULL,
    ord INTEGER NOT NULL,
    rio_encounter_slug TEXT
);

-- Any guild we know about (home guild, configured rivals, realm leaderboard entries).
CREATE TABLE IF NOT EXISTS guilds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    realm_slug TEXT NOT NULL,
    region TEXT NOT NULL,
    faction TEXT,
    wcl_id INTEGER,
    rio_id INTEGER,
    is_home INTEGER NOT NULL DEFAULT 0,
    is_rival INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name, realm_slug, region)
);

CREATE TABLE IF NOT EXISTS reports (
    code TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL REFERENCES guilds(id),
    zone_id INTEGER REFERENCES zones(id),
    title TEXT,
    owner TEXT,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    fights_synced_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reports_guild_zone ON reports(guild_id, zone_id, start_time);

-- One row per boss pull (trash fights are never stored).
CREATE TABLE IF NOT EXISTS fights (
    report_code TEXT NOT NULL REFERENCES reports(code) ON DELETE CASCADE,
    fight_id INTEGER NOT NULL,
    encounter_id INTEGER NOT NULL,
    name TEXT,
    difficulty INTEGER,
    kill INTEGER NOT NULL DEFAULT 0,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    boss_pct REAL,
    fight_pct REAL,
    last_phase INTEGER,
    size INTEGER,
    avg_ilvl REAL,
    PRIMARY KEY (report_code, fight_id)
);
CREATE INDEX IF NOT EXISTS idx_fights_encounter ON fights(encounter_id, difficulty, start_time);

-- Warcraft Logs guild zone rankings (progress / speed) for the home guild.
CREATE TABLE IF NOT EXISTS wcl_zone_rankings (
    guild_id INTEGER NOT NULL REFERENCES guilds(id),
    zone_id INTEGER NOT NULL REFERENCES zones(id),
    metric TEXT NOT NULL,            -- progress | speed | completeRaidSpeed
    difficulty INTEGER,
    world_rank INTEGER, world_pct REAL,
    region_rank INTEGER, region_pct REAL,
    server_rank INTEGER, server_pct REAL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, zone_id, metric, difficulty)
);

-- Attendance per report (from Warcraft Logs guild attendance).
CREATE TABLE IF NOT EXISTS attendance (
    report_code TEXT NOT NULL,
    player_name TEXT NOT NULL,
    player_class TEXT,
    presence INTEGER NOT NULL,        -- 1 = present, 2 = present as bench/partial
    PRIMARY KEY (report_code, player_name)
);

-- Raider.IO reference data.
CREATE TABLE IF NOT EXISTS rio_raids (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    expansion_id INTEGER,
    ord INTEGER
);
CREATE TABLE IF NOT EXISTS rio_encounters (
    raid_slug TEXT NOT NULL REFERENCES rio_raids(slug),
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    ord INTEGER NOT NULL,
    PRIMARY KEY (raid_slug, slug)
);

-- Raider.IO guild summary per raid ("8/8 H").
CREATE TABLE IF NOT EXISTS rio_summary (
    guild_id INTEGER NOT NULL REFERENCES guilds(id),
    raid_slug TEXT NOT NULL,
    summary TEXT,
    total_bosses INTEGER,
    normal_killed INTEGER,
    heroic_killed INTEGER,
    mythic_killed INTEGER,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, raid_slug)
);

-- Raider.IO ranks per raid/difficulty (world/region/realm; 0 = unranked).
CREATE TABLE IF NOT EXISTS rio_rankings (
    guild_id INTEGER NOT NULL REFERENCES guilds(id),
    raid_slug TEXT NOT NULL,
    difficulty INTEGER NOT NULL,
    world_rank INTEGER,
    region_rank INTEGER,
    realm_rank INTEGER,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, raid_slug, difficulty)
);

-- Raider.IO per-boss progress for every guild we track (first kill, pulls, best %).
CREATE TABLE IF NOT EXISTS rio_progress (
    guild_id INTEGER NOT NULL REFERENCES guilds(id),
    raid_slug TEXT NOT NULL,
    difficulty INTEGER NOT NULL,
    encounter_slug TEXT NOT NULL,
    first_defeated INTEGER,           -- ms epoch, NULL if not killed
    last_defeated INTEGER,
    num_pulls INTEGER,
    best_percent REAL,
    is_defeated INTEGER NOT NULL DEFAULT 0,
    pull_started_at INTEGER,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, raid_slug, difficulty, encounter_slug)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    status TEXT NOT NULL,
    detail TEXT
);
"""

VIEWS = """
DROP VIEW IF EXISTS v_pulls;
CREATE VIEW v_pulls AS
SELECT
    f.report_code, f.fight_id, r.guild_id, r.zone_id, z.name AS zone_name,
    f.encounter_id, e.name AS encounter_name, e.ord AS encounter_ord,
    f.difficulty, f.kill, f.start_time, f.end_time,
    (f.end_time - f.start_time) / 1000.0 AS duration_s,
    f.boss_pct, f.fight_pct, f.last_phase, f.size, f.avg_ilvl,
    date(f.start_time / 1000, 'unixepoch') AS pull_date
FROM fights f
JOIN reports r ON r.code = f.report_code
JOIN encounters e ON e.id = f.encounter_id
JOIN zones z ON z.id = r.zone_id;

DROP VIEW IF EXISTS v_first_kills;
CREATE VIEW v_first_kills AS
WITH fk AS (
    SELECT guild_id, zone_id, encounter_id, difficulty, MIN(start_time) AS first_kill_time
    FROM v_pulls WHERE kill = 1
    GROUP BY guild_id, zone_id, encounter_id, difficulty
)
SELECT
    p.guild_id, p.zone_id, p.zone_name, p.encounter_id, p.encounter_name, p.encounter_ord, p.difficulty,
    fk.first_kill_time,
    date(fk.first_kill_time / 1000, 'unixepoch') AS first_kill_date,
    MIN(p.start_time) AS first_pull_time,
    COUNT(*) AS pulls_to_kill,
    SUM(CASE WHEN p.kill = 0 THEN 1 ELSE 0 END) AS wipes_before_kill,
    MIN(CASE WHEN p.kill = 0 THEN p.fight_pct END) AS best_wipe_pct,
    COUNT(DISTINCT p.pull_date) AS nights_to_kill,
    SUM(p.duration_s) / 3600.0 AS hours_to_kill,
    CASE WHEN fk.first_kill_time IS NULL THEN 0 ELSE 1 END AS killed
FROM v_pulls p
LEFT JOIN fk ON fk.guild_id = p.guild_id AND fk.zone_id = p.zone_id
            AND fk.encounter_id = p.encounter_id AND fk.difficulty = p.difficulty
WHERE fk.first_kill_time IS NULL OR p.start_time <= fk.first_kill_time
GROUP BY p.guild_id, p.zone_id, p.encounter_id, p.difficulty;

DROP VIEW IF EXISTS v_raid_nights;
CREATE VIEW v_raid_nights AS
SELECT
    guild_id, zone_id, zone_name, difficulty, pull_date,
    COUNT(*) AS pulls,
    SUM(kill) AS kills,
    COUNT(*) - SUM(kill) AS wipes,
    COUNT(DISTINCT encounter_id) AS bosses_pulled,
    SUM(duration_s) / 3600.0 AS hours_in_combat,
    MIN(start_time) AS first_pull_time,
    MAX(end_time) AS last_pull_time,
    AVG(avg_ilvl) AS avg_ilvl
FROM v_pulls
GROUP BY guild_id, zone_id, difficulty, pull_date;

DROP VIEW IF EXISTS v_attendance;
CREATE VIEW v_attendance AS
SELECT a.player_name, a.player_class, a.presence, a.report_code,
       r.guild_id, r.zone_id, r.start_time, date(r.start_time / 1000, 'unixepoch') AS raid_date
FROM attendance a JOIN reports r ON r.code = a.report_code;

DROP VIEW IF EXISTS v_rio_progress;
CREATE VIEW v_rio_progress AS
SELECT g.id AS guild_id, g.name AS guild_name, g.realm_slug, g.region, g.faction, g.is_home, g.is_rival,
       p.raid_slug, rr.name AS raid_name, p.difficulty, p.encounter_slug, re.name AS encounter_name, re.ord AS encounter_ord,
       p.first_defeated, date(p.first_defeated / 1000, 'unixepoch') AS first_defeated_date,
       p.num_pulls, p.best_percent, p.is_defeated, p.pull_started_at
FROM rio_progress p
JOIN guilds g ON g.id = p.guild_id
LEFT JOIN rio_raids rr ON rr.slug = p.raid_slug
LEFT JOIN rio_encounters re ON re.raid_slug = p.raid_slug AND re.slug = p.encounter_slug;
"""

# Human-readable notes for the Ask feature's schema description.
TABLE_DOCS: dict[str, str] = {
    "zones": "Raid tiers from Warcraft Logs. rio_raid_slug links to Raider.IO data.",
    "encounters": "Bosses in each zone; ord is the boss order within the raid.",
    "guilds": "Every guild we know. is_home=1 is Killing Time; is_rival=1 are configured rivals; others come from the realm leaderboard.",
    "reports": "Warcraft Logs reports (one per raid night, usually) for the home guild.",
    "fights": "One row per boss pull from our logs. kill=1 for kills. fight_pct is % of the encounter remaining on a wipe.",
    "v_pulls": "fights joined to encounter/zone names, with pull_date and duration_s. Prefer this over fights.",
    "v_first_kills": "Per guild/zone/boss/difficulty: first kill time, pulls_to_kill (pulls up to and incl. the first kill; all pulls if not killed), wipes_before_kill, nights_to_kill, hours_to_kill, killed flag.",
    "v_raid_nights": "Per raid night (pull_date) and difficulty: pulls, kills, wipes, bosses_pulled, hours_in_combat.",
    "attendance / v_attendance": "Who attended each report (presence 1 = present).",
    "wcl_zone_rankings": "Warcraft Logs world/region/server rank for the home guild per zone (metric progress/speed/completeRaidSpeed).",
    "rio_raids / rio_encounters": "Raider.IO raid and boss reference data (slugs).",
    "rio_summary": "Raider.IO 'X/Y M' summary per guild per raid.",
    "rio_rankings": "Raider.IO world/region/realm rank per guild, raid and difficulty (0 = unranked).",
    "rio_progress / v_rio_progress": "Raider.IO per-boss progress for ALL tracked guilds (home, rivals, realm leaderboard): first_defeated, num_pulls, best_percent. Use this to compare guilds.",
    "sync_log / meta": "Sync bookkeeping.",
}


def connect(path: str) -> sqlite3.Connection:
    """Open (and create) the database, applying schema and views."""
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.executescript(VIEWS)
    conn.commit()
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def schema_description(conn: sqlite3.Connection) -> str:
    """Compact schema listing (tables, views, columns) with the notes above - used by Ask."""
    lines: list[str] = []
    rows = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
    ).fetchall()
    for r in rows:
        cols = conn.execute(f"PRAGMA table_info('{r['name']}')").fetchall()
        col_str = ", ".join(f"{c['name']} {c['type'] or ''}".strip() for c in cols)
        lines.append(f"{r['type'].upper()} {r['name']}({col_str})")
    lines.append("")
    lines.append("Notes:")
    for k, v in TABLE_DOCS.items():
        lines.append(f"- {k}: {v}")
    lines.append("- Difficulty codes: 1=LFR, 3=Normal, 4=Heroic, 5=Mythic.")
    lines.append("- Timestamps are ms since epoch: use datetime(col/1000,'unixepoch') or date(col/1000,'unixepoch').")
    return "\n".join(lines)
