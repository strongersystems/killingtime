"""Derived progression metrics. Every function returns plain dicts/lists so the results can be
rendered by the web UI, printed by the CLI, or handed to Claude as tool output."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from .db import CODE_TO_RIO_DIFFICULTY, DIFFICULTIES


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def ms_to_date(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def home_guild(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM guilds WHERE is_home = 1 LIMIT 1").fetchone()
    return dict(row) if row else None


def tiers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Zones that have any of our pulls, newest first, with a per-difficulty kill count."""
    rows = _rows(
        conn,
        """
        SELECT z.id, z.name, z.frozen, z.rio_raid_slug, x.name AS expansion,
               (SELECT COUNT(*) FROM encounters e WHERE e.zone_id = z.id) AS bosses,
               (SELECT COUNT(*) FROM reports r WHERE r.zone_id = z.id) AS reports,
               (SELECT MIN(start_time) FROM reports r WHERE r.zone_id = z.id) AS first_report,
               (SELECT MAX(end_time) FROM reports r WHERE r.zone_id = z.id) AS last_report
        FROM zones z LEFT JOIN expansions x ON x.id = z.expansion_id
        WHERE EXISTS (SELECT 1 FROM reports r WHERE r.zone_id = z.id)
        ORDER BY z.id DESC
        """,
    )
    for r in rows:
        kills = _rows(
            conn,
            "SELECT difficulty, SUM(killed) AS killed FROM v_first_kills WHERE zone_id = ? GROUP BY difficulty",
            (r["id"],),
        )
        r["kills"] = {int(k["difficulty"]): int(k["killed"] or 0) for k in kills if k["difficulty"] is not None}
        r["first_report_date"] = ms_to_date(r["first_report"])
        r["last_report_date"] = ms_to_date(r["last_report"])
        r["summary"] = " / ".join(
            f"{r['kills'].get(d, 0)}/{r['bosses']} {DIFFICULTIES[d][0]}" for d in (5, 4, 3) if d in r["kills"]
        )
    return rows


def current_tier(conn: sqlite3.Connection) -> dict[str, Any] | None:
    t = tiers(conn)
    return t[0] if t else None


def best_difficulty(conn: sqlite3.Connection, zone_id: int) -> int:
    row = conn.execute(
        "SELECT MAX(difficulty) AS d FROM v_pulls WHERE zone_id = ? AND difficulty IN (3,4,5)", (zone_id,)
    ).fetchone()
    return int(row["d"]) if row and row["d"] else 5


def tier_summary(conn: sqlite3.Connection, zone_id: int, difficulty: int) -> dict[str, Any]:
    zone = conn.execute("SELECT * FROM zones WHERE id = ?", (zone_id,)).fetchone()
    bosses = _rows(
        conn,
        """
        SELECT e.id, e.name, e.ord, fk.killed, fk.first_kill_time, fk.first_kill_date, fk.pulls_to_kill,
               fk.wipes_before_kill, fk.best_wipe_pct, fk.nights_to_kill, fk.hours_to_kill, fk.first_pull_time,
               (SELECT COUNT(*) FROM v_pulls p WHERE p.encounter_id = e.id AND p.difficulty = ?) AS total_pulls,
               (SELECT SUM(kill) FROM v_pulls p WHERE p.encounter_id = e.id AND p.difficulty = ?) AS total_kills,
               (SELECT MIN(fight_pct) FROM v_pulls p WHERE p.encounter_id = e.id AND p.difficulty = ? AND kill = 0) AS best_pct
        FROM encounters e
        LEFT JOIN v_first_kills fk ON fk.encounter_id = e.id AND fk.difficulty = ?
        WHERE e.zone_id = ?
        ORDER BY e.ord
        """,
        (difficulty, difficulty, difficulty, difficulty, zone_id),
    )
    totals = conn.execute(
        """SELECT COUNT(*) AS pulls, SUM(kill) AS kills, COUNT(DISTINCT pull_date) AS nights,
                  SUM(duration_s)/3600.0 AS hours, MIN(start_time) AS first_pull, MAX(end_time) AS last_pull,
                  AVG(avg_ilvl) AS avg_ilvl
           FROM v_pulls WHERE zone_id = ? AND difficulty = ?""",
        (zone_id, difficulty),
    ).fetchone()
    killed = sum(1 for b in bosses if b["killed"])
    first_pull = totals["first_pull"]
    last_kill = max((b["first_kill_time"] for b in bosses if b["first_kill_time"]), default=None)
    days_to_current = (last_kill - first_pull) / 86400_000 if (first_pull and last_kill) else None
    return {
        "zone": dict(zone) if zone else None,
        "difficulty": difficulty,
        "difficulty_name": DIFFICULTIES.get(difficulty, str(difficulty)),
        "bosses": bosses,
        "killed": killed,
        "total_bosses": len(bosses),
        "cleared": killed == len(bosses) and killed > 0,
        "pulls": totals["pulls"] or 0,
        "kills": totals["kills"] or 0,
        "wipes": (totals["pulls"] or 0) - (totals["kills"] or 0),
        "nights": totals["nights"] or 0,
        "hours": round(totals["hours"] or 0, 1),
        "avg_ilvl": round(totals["avg_ilvl"], 1) if totals["avg_ilvl"] else None,
        "first_pull_date": ms_to_date(first_pull),
        "last_pull_date": ms_to_date(totals["last_pull"]),
        "days_to_latest_kill": round(days_to_current, 1) if days_to_current is not None else None,
        "next_boss": next((b for b in bosses if not b["killed"]), None),
    }


def progress_timeline(conn: sqlite3.Connection, zone_id: int, difficulty: int) -> list[dict[str, Any]]:
    """Cumulative bosses killed by day, plus day index since our first pull in this zone/difficulty."""
    first = conn.execute(
        "SELECT MIN(start_time) AS t FROM v_pulls WHERE zone_id = ? AND difficulty = ?", (zone_id, difficulty)
    ).fetchone()["t"]
    kills = _rows(
        conn,
        """SELECT encounter_name, first_kill_time FROM v_first_kills
           WHERE zone_id = ? AND difficulty = ? AND killed = 1 ORDER BY first_kill_time""",
        (zone_id, difficulty),
    )
    out = []
    for i, k in enumerate(kills, start=1):
        out.append(
            {
                "boss": k["encounter_name"],
                "date": ms_to_date(k["first_kill_time"]),
                "day": round((k["first_kill_time"] - first) / 86400_000, 1) if first else None,
                "kills": i,
            }
        )
    return out


def tier_comparison(conn: sqlite3.Connection, difficulty: int) -> list[dict[str, Any]]:
    """Per zone: cumulative pulls and days to each boss kill in boss order, for tier-over-tier comparison."""
    out = []
    for t in tiers(conn):
        s = tier_summary(conn, t["id"], difficulty)
        if s["pulls"] == 0:
            continue
        first_pull = min((b["first_pull_time"] for b in s["bosses"] if b["first_pull_time"]), default=None)
        cum_pulls, points = 0, []
        for b in s["bosses"]:
            cum_pulls += b["pulls_to_kill"] or 0
            points.append(
                {
                    "ord": b["ord"],
                    "boss": b["name"],
                    "killed": bool(b["killed"]),
                    "pulls_to_kill": b["pulls_to_kill"],
                    "cum_pulls": cum_pulls,
                    "days_to_kill": round((b["first_kill_time"] - first_pull) / 86400_000, 1)
                    if (b["killed"] and first_pull)
                    else None,
                    "nights_to_kill": b["nights_to_kill"],
                }
            )
        out.append(
            {
                "zone_id": t["id"],
                "zone": t["name"],
                "expansion": t["expansion"],
                "killed": s["killed"],
                "total_bosses": s["total_bosses"],
                "pulls": s["pulls"],
                "nights": s["nights"],
                "hours": s["hours"],
                "days_to_latest_kill": s["days_to_latest_kill"],
                "first_pull_date": s["first_pull_date"],
                "bosses": points,
            }
        )
    return out


def raid_nights(conn: sqlite3.Connection, zone_id: int, limit: int = 60) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """SELECT pull_date, difficulty, pulls, kills, wipes, bosses_pulled, ROUND(hours_in_combat, 2) AS hours_in_combat,
                  ROUND(avg_ilvl, 1) AS avg_ilvl,
                  (SELECT GROUP_CONCAT(DISTINCT encounter_name) FROM v_pulls p
                    WHERE p.zone_id = n.zone_id AND p.difficulty = n.difficulty AND p.pull_date = n.pull_date AND p.kill = 1) AS killed_bosses
           FROM v_raid_nights n WHERE zone_id = ? ORDER BY pull_date DESC, difficulty DESC LIMIT ?""",
        (zone_id, limit),
    )


def attendance_summary(conn: sqlite3.Connection, zone_id: int) -> dict[str, Any]:
    total_raids = conn.execute(
        "SELECT COUNT(DISTINCT report_code) AS c FROM v_attendance WHERE zone_id = ?", (zone_id,)
    ).fetchone()["c"]
    players = _rows(
        conn,
        """SELECT player_name, player_class, COUNT(DISTINCT report_code) AS raids,
                  ROUND(100.0 * COUNT(DISTINCT report_code) / ?, 1) AS pct
           FROM v_attendance WHERE zone_id = ? AND presence = 1
           GROUP BY player_name ORDER BY raids DESC, player_name""",
        (max(total_raids, 1), zone_id),
    )
    return {"total_raids": total_raids, "players": players}


def rio_raids_for_zone(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Raider.IO raids we have standings for, newest first."""
    return _rows(
        conn,
        """SELECT r.slug, r.name, r.ord, (SELECT COUNT(*) FROM rio_encounters e WHERE e.raid_slug = r.slug) AS bosses,
                  (SELECT id FROM zones z WHERE z.rio_raid_slug = r.slug) AS zone_id
           FROM rio_raids r
           WHERE EXISTS (SELECT 1 FROM rio_progress p WHERE p.raid_slug = r.slug)
           ORDER BY r.ord DESC""",
    )


def realm_standings(conn: sqlite3.Connection, raid_slug: str, difficulty: int, limit: int = 100) -> list[dict[str, Any]]:
    """Guilds on the home realm ranked by Raider.IO realm rank for a raid/difficulty."""
    home = home_guild(conn)
    realm, region = (home["realm_slug"], home["region"]) if home else ("", "")
    return _rows(
        conn,
        """SELECT g.id AS guild_id, g.name, g.faction, g.is_home, g.is_rival, rk.realm_rank, rk.region_rank, rk.world_rank,
                  (SELECT COUNT(*) FROM rio_progress p WHERE p.guild_id = g.id AND p.raid_slug = rk.raid_slug
                      AND p.difficulty = rk.difficulty AND p.is_defeated = 1) AS killed,
                  (SELECT SUM(num_pulls) FROM rio_progress p WHERE p.guild_id = g.id AND p.raid_slug = rk.raid_slug
                      AND p.difficulty = rk.difficulty) AS pulls,
                  (SELECT MAX(first_defeated) FROM rio_progress p WHERE p.guild_id = g.id AND p.raid_slug = rk.raid_slug
                      AND p.difficulty = rk.difficulty) AS latest_kill,
                  (SELECT p.encounter_slug || ' ' || COALESCE(p.best_percent, '') || '%' FROM rio_progress p
                      WHERE p.guild_id = g.id AND p.raid_slug = rk.raid_slug AND p.difficulty = rk.difficulty AND p.is_defeated = 0
                      ORDER BY p.best_percent LIMIT 1) AS current_prog
           FROM rio_rankings rk JOIN guilds g ON g.id = rk.guild_id
           WHERE rk.raid_slug = ? AND rk.difficulty = ? AND g.realm_slug = ? AND g.region = ? AND rk.realm_rank > 0
           ORDER BY rk.realm_rank LIMIT ?""",
        (raid_slug, difficulty, realm, region, limit),
    )


def rival_comparison(conn: sqlite3.Connection, raid_slug: str, difficulty: int, extra_top: int = 3) -> dict[str, Any]:
    """Home guild vs configured rivals (plus the realm's top guilds): per-boss first kill dates and pulls."""
    bosses = _rows(conn, "SELECT slug, name, ord FROM rio_encounters WHERE raid_slug = ? ORDER BY ord", (raid_slug,))
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM guilds WHERE is_home = 1 OR is_rival = 1 ORDER BY is_home DESC, name"
        )
    ]
    for r in realm_standings(conn, raid_slug, difficulty, limit=extra_top):
        if r["guild_id"] not in ids and len(ids) < 8:
            ids.append(r["guild_id"])
    guilds = []
    for gid in ids[:8]:
        g = dict(conn.execute("SELECT * FROM guilds WHERE id = ?", (gid,)).fetchone())
        prog = {
            p["encounter_slug"]: p
            for p in _rows(
                conn,
                "SELECT * FROM rio_progress WHERE guild_id = ? AND raid_slug = ? AND difficulty = ?",
                (gid, raid_slug, difficulty),
            )
        }
        rank = conn.execute(
            "SELECT * FROM rio_rankings WHERE guild_id = ? AND raid_slug = ? AND difficulty = ?", (gid, raid_slug, difficulty)
        ).fetchone()
        per_boss = []
        for b in bosses:
            p = prog.get(b["slug"], {})
            per_boss.append(
                {
                    "boss": b["name"],
                    "killed": bool(p.get("is_defeated")),
                    "first_kill": ms_to_date(p.get("first_defeated")),
                    "first_kill_ms": p.get("first_defeated"),
                    "pulls": p.get("num_pulls"),
                    "best_pct": p.get("best_percent"),
                }
            )
        killed = sum(1 for b in per_boss if b["killed"])
        guilds.append(
            {
                "guild_id": gid,
                "name": g["name"],
                "realm": g["realm_slug"],
                "is_home": bool(g["is_home"]),
                "is_rival": bool(g["is_rival"]),
                "killed": killed,
                "total": len(bosses),
                "pulls": sum(b["pulls"] or 0 for b in per_boss),
                "realm_rank": rank["realm_rank"] if rank else None,
                "region_rank": rank["region_rank"] if rank else None,
                "world_rank": rank["world_rank"] if rank else None,
                "bosses": per_boss,
            }
        )
    return {
        "raid_slug": raid_slug,
        "difficulty": difficulty,
        "difficulty_name": DIFFICULTIES.get(difficulty),
        "rio_difficulty": CODE_TO_RIO_DIFFICULTY.get(difficulty),
        "bosses": bosses,
        "guilds": guilds,
    }


def race_timeline(conn: sqlite3.Connection, raid_slug: str, difficulty: int) -> list[dict[str, Any]]:
    """For the rival chart: each guild's cumulative kills by date (step series)."""
    cmp = rival_comparison(conn, raid_slug, difficulty)
    series = []
    for g in cmp["guilds"]:
        kills = sorted((b["first_kill_ms"] for b in g["bosses"] if b["first_kill_ms"]), key=int)
        series.append(
            {
                "name": g["name"],
                "is_home": g["is_home"],
                "points": [{"date": ms_to_date(t), "kills": i} for i, t in enumerate(kills, start=1)],
            }
        )
    return series


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compact snapshot used by the dashboard header and the Ask tool."""
    home = home_guild(conn)
    cur = current_tier(conn)
    last_sync = conn.execute("SELECT value FROM meta WHERE key = 'last_sync'").fetchone()
    rio_summary = _rows(
        conn,
        """SELECT s.raid_slug, r.name, s.summary, s.total_bosses, s.mythic_killed, s.heroic_killed, s.normal_killed,
                  (SELECT world_rank FROM rio_rankings k WHERE k.guild_id = s.guild_id AND k.raid_slug = s.raid_slug AND k.difficulty = 5) AS mythic_world,
                  (SELECT realm_rank FROM rio_rankings k WHERE k.guild_id = s.guild_id AND k.raid_slug = s.raid_slug AND k.difficulty = 5) AS mythic_realm,
                  (SELECT world_rank FROM rio_rankings k WHERE k.guild_id = s.guild_id AND k.raid_slug = s.raid_slug AND k.difficulty = 4) AS heroic_world,
                  (SELECT realm_rank FROM rio_rankings k WHERE k.guild_id = s.guild_id AND k.raid_slug = s.raid_slug AND k.difficulty = 4) AS heroic_realm
           FROM rio_summary s JOIN guilds g ON g.id = s.guild_id LEFT JOIN rio_raids r ON r.slug = s.raid_slug
           WHERE g.is_home = 1 ORDER BY r.ord DESC""",
    )
    return {
        "guild": home,
        "current_tier": cur,
        "tiers": tiers(conn),
        "raiderio": rio_summary,
        "last_sync": ms_to_date(int(last_sync["value"])) if last_sync else None,
        "last_sync_ms": int(last_sync["value"]) if last_sync else None,
    }
